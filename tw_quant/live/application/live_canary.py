from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Lock
from time import perf_counter
from typing import Protocol
from zoneinfo import ZoneInfo

from ...broker import (
    BrokerAccountRef,
    BrokerCapabilities,
    BrokerOrder,
    CanaryArmSession,
    LIVE_CANARY_CONFIRMATION,
    LiveCanaryConfig,
    SQLiteCanaryArmRepository,
)
from ...execution.canary import LiveExecutionSink
from ...execution.live_models import InstrumentSpec, LiveExecutionCandidate
from ...execution.live_policy import ExecutionPolicy
from ...market import ExecutionQuoteView
from ...risk import LiveRiskService
from ...risk.live import LiveKillSwitchAction, LiveRiskContext


LIVE_ORDER_CONFIRMATION = "REAL ORDER"


class ManualCanaryContext(Protocol):
    """Public-process read boundary. Implementations must use cached/durable truth."""

    def target(self, owner_id: str) -> BrokerAccountRef: ...
    def instrument(self, symbol: str, contract: str) -> InstrumentSpec: ...
    def risk_context(self, candidate: LiveExecutionCandidate) -> LiveRiskContext: ...
    def capabilities(self, target: BrokerAccountRef) -> BrokerCapabilities: ...
    def broker_position(self, target: BrokerAccountRef, contract: str) -> int | None: ...
    def assert_arm_ready(self, target: BrokerAccountRef) -> None: ...
    def public_status(self, owner_id: str) -> dict[str, object]: ...
    def preflight(
        self, owner_id: str, canary_enabled: bool, allowed_contracts: frozenset[str]
    ) -> dict[str, object]: ...
    def activate_kill_switch(
        self, owner_id: str, target: BrokerAccountRef,
        action: LiveKillSwitchAction, reason: str, now: datetime,
    ) -> None: ...


class ManualLiveCanaryService:
    """Human-only canary orchestration; never accepts or owns a BrokerPort."""

    def __init__(
        self,
        *,
        config: LiveCanaryConfig,
        arms: SQLiteCanaryArmRepository,
        sink: LiveExecutionSink,
        quotes: ExecutionQuoteView,
        risk: LiveRiskService,
        policy: ExecutionPolicy,
        context: ManualCanaryContext,
        now=None,
    ) -> None:
        self.config = config
        self.arms = arms
        self.sink = sink
        self.quotes = quotes
        self.risk = risk
        self.policy = policy
        self.context = context
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._admission_lock = Lock()
        self.live_order_requests_total = 0
        self.live_order_reserved_total = 0
        self.total_reserve_ms = 0.0
        self.max_reserve_ms = 0.0

    def arm(self, owner_id: str, created_by: str, confirmation: str) -> CanaryArmSession:
        if confirmation != LIVE_CANARY_CONFIRMATION:
            raise RuntimeError("live_canary_confirmation_invalid")
        target = self.context.target(owner_id)
        self.config.assert_request_allowed(
            owner_id,
            target,
            next(iter(self.config.allowed_symbols)),
            next(iter(self.config.allowed_contracts)),
            1,
        )
        self.context.assert_arm_ready(target)
        readiness = self._preflight(owner_id)
        if readiness.get("ready") is not True:
            blockers = readiness.get("blockers", [])
            code = str(blockers[0]) if isinstance(blockers, list) and blockers else "unknown"
            raise RuntimeError(f"live_canary_preflight_failed:{code}")
        armed_at = self.now()
        identity = f"{owner_id}|{target.broker_name}|{target.account_id}|{armed_at.isoformat()}"
        session = CanaryArmSession(
            arm_id="arm:" + sha256(identity.encode()).hexdigest()[:32],
            owner_id=owner_id,
            account_ref=target,
            armed_at=armed_at,
            expires_at=armed_at + timedelta(seconds=self.config.arm_ttl_seconds),
            created_by=created_by,
            target_id=getattr(self.context, "target_id", None),
        )
        self.arms.arm(session)
        return session

    def disarm(self, owner_id: str) -> bool:
        target = self.context.target(owner_id)
        return self.arms.disarm(owner_id, target, reason="operator_disarm")

    def _session(self, owner_id: str, target: BrokerAccountRef) -> CanaryArmSession:
        target_id = getattr(self.context, "target_id", None)
        session = self.arms.active(owner_id, target, self.now(), target_id)
        if session is None:
            raise RuntimeError("live_canary_arm_inactive")
        return session

    @staticmethod
    def _session_name(now: datetime) -> str:
        local = now.astimezone(ZoneInfo("Asia/Taipei")).time()
        return "day" if 8 <= local.hour < 15 else "night"

    def reserve_order(
        self,
        *,
        owner_id: str,
        side: str,
        quantity: int,
        idempotency_key: str,
        confirmation: str,
    ) -> tuple[BrokerOrder, bool]:
        started = perf_counter()
        with self._admission_lock:
            self.live_order_requests_total += 1
            try:
                result = self._reserve_order_locked(
                    owner_id=owner_id,
                    side=side,
                    quantity=quantity,
                    idempotency_key=idempotency_key,
                    confirmation=confirmation,
                )
                self.live_order_reserved_total += int(result[1])
                return result
            finally:
                elapsed = (perf_counter() - started) * 1_000
                self.total_reserve_ms += elapsed
                self.max_reserve_ms = max(self.max_reserve_ms, elapsed)

    def _reserve_order_locked(
        self,
        *,
        owner_id: str,
        side: str,
        quantity: int,
        idempotency_key: str,
        confirmation: str,
    ) -> tuple[BrokerOrder, bool]:
        if not idempotency_key.strip() or len(idempotency_key) > 128:
            raise RuntimeError("idempotency_key_invalid")
        symbol = next(iter(self.config.allowed_symbols))
        contract = next(iter(self.config.allowed_contracts))
        expected = f"{side.upper()} {quantity} {contract} {LIVE_ORDER_CONFIRMATION}"
        if confirmation != expected:
            raise RuntimeError("live_order_confirmation_invalid")
        target = self.context.target(owner_id)
        self.config.assert_request_allowed(owner_id, target, symbol, contract, quantity)
        arm = self._session(owner_id, target)
        now = self.now()
        quote = self.quotes.get(symbol, contract)
        if quote is None or not quote.complete:
            raise RuntimeError("execution_quote_incomplete")
        instrument = self.context.instrument(symbol, contract)
        reference = quote.best_ask if side == "buy" else quote.best_bid
        assert reference is not None
        stop_delta = self.config.protective_stop_ticks * instrument.tick_size
        stop = reference - stop_delta if side == "buy" else reference + stop_delta
        decision_id = "manual:" + sha256(
            f"{owner_id}|{target.broker_name}|{target.account_id}|{idempotency_key}".encode()
        ).hexdigest()
        candidate = LiveExecutionCandidate(
            owner_id=owner_id,
            runtime_id="manual-live-canary",
            decision_id=decision_id,
            target=target,
            strategy_id="manual-live-canary",
            strategy_version=1,
            symbol=symbol,
            contract=contract,
            trigger_time=now,
            action="entry",
            direction="long" if side == "buy" else "short",
            quantity=quantity,
            reason="manual_live_canary",
            planned_stop_price=stop,
            session=self._session_name(now),  # type: ignore[arg-type]
        )
        approval = self.risk.evaluate(
            candidate, self.context.risk_context(candidate), quote, instrument
        )
        result = self.policy.evaluate(
            candidate,
            quote,
            instrument,
            self.context.capabilities(target),
            approval,
        )
        if result.request is None:
            raise RuntimeError(result.reason)
        client_order_id = "canary:" + sha256(
            f"{owner_id}|{target.broker_name}|{target.account_id}|{idempotency_key}".encode()
        ).hexdigest()
        request = replace(
            result.request,
            client_order_id=client_order_id,
            correlation_id=decision_id,
            causation_id=idempotency_key,
            source="manual_live_canary",
            arm_id=arm.arm_id,
        )
        order, created = self.sink.reserve(target, request)
        self.arms.audit(
            "order.reserved" if created else "order.idempotent_replay",
            owner_id,
            target,
            arm_id=arm.arm_id,
            request_id=idempotency_key,
            detail={
                "client_order_id": order.request.client_order_id,
                "side": side,
                "quantity": quantity,
                "policy": self.policy.version,
                "risk_policy": approval.policy_version,
            },
            occurred_at=now,
        )
        return order, created

    def reserve_cancel(self, owner_id: str, client_order_id: str) -> tuple[BrokerOrder, bool]:
        target = self.context.target(owner_id)
        arm = self._session(owner_id, target)
        result = self.sink.reserve_cancel(target, owner_id, client_order_id)
        self.arms.audit(
            "cancel.reserved" if result[1] else "cancel.noop",
            owner_id,
            target,
            arm_id=arm.arm_id,
            request_id=client_order_id,
            occurred_at=self.now(),
        )
        return result

    def reserve_close(
        self, owner_id: str, idempotency_key: str
    ) -> tuple[BrokerOrder, bool]:
        with self._admission_lock:
            target = self.context.target(owner_id)
            arm = self._session(owner_id, target)
            symbol = next(iter(self.config.allowed_symbols))
            contract = next(iter(self.config.allowed_contracts))
            position = self.context.broker_position(target, contract)
            if position is None or position == 0:
                raise RuntimeError("reduce_only_position_unavailable")
            if abs(position) > self.config.max_quantity:
                raise RuntimeError("live_canary_close_quantity_exceeded")
            now = self.now()
            quote = self.quotes.get(symbol, contract)
            if quote is None:
                raise RuntimeError("execution_quote_unavailable")
            instrument = self.context.instrument(symbol, contract)
            decision_id = "manual-close:" + sha256(
                f"{owner_id}|{target.public_id}|{idempotency_key}".encode()
            ).hexdigest()
            candidate = LiveExecutionCandidate(
                owner_id=owner_id,
                runtime_id="manual-live-canary",
                decision_id=decision_id,
                target=target,
                strategy_id="manual-live-canary",
                strategy_version=1,
                symbol=symbol,
                contract=contract,
                trigger_time=now,
                action="exit",
                direction="long" if position > 0 else "short",
                quantity=abs(position),
                reason="manual_live_canary_close",
                planned_stop_price=None,
                session=self._session_name(now),  # type: ignore[arg-type]
            )
            approval = self.risk.evaluate(
                candidate, self.context.risk_context(candidate), quote, instrument
            )
            result = self.policy.evaluate(
                candidate, quote, instrument, self.context.capabilities(target), approval
            )
            if result.request is None:
                raise RuntimeError(result.reason)
            request = replace(
                result.request,
                client_order_id="canary-close:" + sha256(
                    f"{owner_id}|{target.public_id}|{idempotency_key}".encode()
                ).hexdigest(),
                correlation_id=decision_id,
                causation_id=idempotency_key,
                source="manual_live_canary",
                arm_id=arm.arm_id,
            )
            order, created = self.sink.reserve(target, request)
            self.arms.audit(
                "position_close.reserved" if created else "position_close.idempotent_replay",
                owner_id,
                target,
                arm_id=arm.arm_id,
                request_id=idempotency_key,
                detail={"quantity": abs(position), "side": request.side},
                occurred_at=now,
            )
            return order, created

    def activate_kill_switch(
        self, owner_id: str, action: str, reason: str
    ) -> dict[str, object]:
        try:
            parsed = LiveKillSwitchAction(action)
        except ValueError as exc:
            raise RuntimeError("live_canary_kill_switch_invalid") from exc
        target = self.context.target(owner_id)
        if parsed is LiveKillSwitchAction.FLATTEN:
            guardian = self.context.public_status(owner_id).get("position_guardian", {})
            if not isinstance(guardian, dict) or guardian.get("enabled") is not True:
                raise RuntimeError("live_position_guardian_disabled")
        now = self.now()
        self.context.activate_kill_switch(owner_id, target, parsed, reason, now)
        cancelled = 0
        if parsed is LiveKillSwitchAction.CANCEL_WORKING:
            for order in self.sink.manager.repository.orders(owner_id, target=target):
                if (
                    order.request.source == "manual_live_canary"
                    and not order.request.reduce_only
                    and not order.status.terminal
                    and order.broker_order_id
                ):
                    _pending, created = self.sink.reserve_cancel(
                        target, owner_id, order.request.client_order_id
                    )
                    cancelled += int(created)
        self.arms.audit(
            f"kill_switch.{parsed.value}", owner_id, target,
            arm_id=None, detail={"reason": reason, "cancel_reserved": cancelled},
            occurred_at=now,
        )
        return {
            "action": parsed.value,
            "cancel_reserved": cancelled,
            "flatten_enabled": parsed is LiveKillSwitchAction.FLATTEN,
            "guardian_managed": parsed is LiveKillSwitchAction.FLATTEN,
        }

    def status(self, owner_id: str) -> dict[str, object]:
        target = self.context.target(owner_id)
        arm = self.arms.active(owner_id, target, self.now())
        operational = self.context.public_status(owner_id)
        readiness = self._preflight(owner_id)
        ordering_enabled = bool(
            arm
            and readiness.get("ready") is True
        )
        orders = [
            item for item in self.sink.manager.repository.orders(owner_id, target=target)
            if item.request.source == "manual_live_canary"
        ]
        contract = next(iter(self.config.allowed_contracts))
        position_quantity = self.context.broker_position(target, contract)
        return {
            **operational,
            "mode": "live_canary",
            "real_money": True,
            "arm": "active" if arm else "off",
            "arm_expires_at": arm.expires_at.isoformat() if arm else None,
            "ordering_enabled": ordering_enabled,
            "readiness": readiness,
            "max_quantity": self.config.max_quantity,
            "allowed_symbol": next(iter(self.config.allowed_symbols)),
            "allowed_contract": contract,
            "position_quantity": position_quantity,
            "position": (
                "unknown" if position_quantity is None
                else "flat" if position_quantity == 0
                else "long" if position_quantity > 0 else "short"
            ),
            "metrics": {
                "live_order_requests_total": self.live_order_requests_total,
                "live_order_reserved_total": self.live_order_reserved_total,
                "average_reserve_ms": (
                    self.total_reserve_ms / self.live_order_requests_total
                    if self.live_order_requests_total else None
                ),
                "max_reserve_ms": self.max_reserve_ms,
                "working_orders": sum(not item.status.terminal for item in orders),
                "unknown_orders": sum(
                    item.status.value == "unknown" for item in orders
                ),
                "last_live_order_time": (
                    max(item.updated_at for item in orders).isoformat()
                    if orders else None
                ),
            },
        }

    def _preflight(self, owner_id: str) -> dict[str, object]:
        evaluator = getattr(self.context, "preflight", None)
        if callable(evaluator):
            return evaluator(
                owner_id, self.config.enabled, self.config.allowed_contracts
            )
        operational = self.context.public_status(owner_id)
        checks = {
            "canary_config_enabled": self.config.enabled,
            # Compatibility for narrow test/adapter contexts that already enforce
            # these two gates in assert_arm_ready but predate structured preflight.
            "broker_connected": operational.get("broker_connected", True) is True,
            "ca_ready": operational.get("ca_ready", True) is True,
            "recovery_ready": operational.get("recovery_status") == "ready",
            "guardian_healthy": isinstance(operational.get("position_guardian"), dict)
            and operational["position_guardian"].get("enabled") is True,
        }
        blockers = [name for name, passed in checks.items() if not passed]
        return {"ready": not blockers, "checks": checks, "blockers": blockers}

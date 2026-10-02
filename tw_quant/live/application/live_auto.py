from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import RLock
from typing import Callable, Mapping, Protocol

from ...broker import BrokerAccountRef, BrokerOrderStatus, CanaryArmSession, CanaryArmStore
from ...execution.canary import StrategyLiveExecutionSink
from ...execution.live_models import InstrumentSpec, LiveExecutionCandidate
from ...execution.live_policy import ExecutionPolicy
from ...market import ExecutionQuoteView, KBar
from ...risk.live import LiveRiskService
from ..storage import TradingRuntimeRepository
from .errors import InvalidInputError
from .live_shadow import _risk_stop


LIVE_AUTO_CONFIRMATION = "ARM LIVE AUTO - REAL MONEY"


@dataclass(frozen=True)
class LiveAutoArm:
    arm_id: str
    runtime_id: str
    owner_id: str
    target: BrokerAccountRef
    armed_at: datetime
    expires_at: datetime

    def active(self, now: datetime) -> bool:
        return self.armed_at <= now < self.expires_at


class LiveAutoContext(Protocol):
    def resolve_target(self, runtime: Mapping[str, object]) -> BrokerAccountRef: ...
    def preflight(self, runtime: Mapping[str, object]) -> Mapping[str, object]: ...
    def risk_context(self, candidate: LiveExecutionCandidate): ...
    def instrument(self, symbol: str, contract: str) -> InstrumentSpec: ...
    def capabilities(self, target: BrokerAccountRef): ...
    def active_position(self, owner_id: str, target: BrokerAccountRef, contract: str) -> int | None: ...
    def active_entry_orders(self, owner_id: str, target: BrokerAccountRef, contract: str) -> int: ...
    def request_strategy_exit(self, runtime: Mapping[str, object]) -> str: ...
    def audit(self, event: str, runtime: Mapping[str, object], detail: Mapping[str, object]) -> None: ...


class LiveAutoService:
    """Ephemeral ARM and fail-closed decision router over the existing live core."""

    def __init__(self, *, enabled: bool, runtimes: TradingRuntimeRepository,
                 context: LiveAutoContext, quotes: ExecutionQuoteView,
                 risk: LiveRiskService, policy: ExecutionPolicy,
                 sink: StrategyLiveExecutionSink, arm_ttl_seconds: int = 600,
                 shared_arms: CanaryArmStore | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        if not 60 <= arm_ttl_seconds <= 900:
            raise ValueError("live auto ARM TTL must be between 1 and 15 minutes")
        self.enabled = enabled
        self.runtimes = runtimes
        self.context = context
        self.quotes = quotes
        self.risk = risk
        self.policy = policy
        self.sink = sink
        self.arm_ttl_seconds = arm_ttl_seconds
        self.shared_arms = shared_arms
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._arms: dict[str, LiveAutoArm] = {}
        self._lock = RLock()

    def arm(self, runtime_id: str, owner_id: str, confirmation: str) -> dict[str, object]:
        if not self.enabled:
            raise InvalidInputError("live_auto is disabled")
        if confirmation != LIVE_AUTO_CONFIRMATION:
            raise InvalidInputError("explicit REAL MONEY confirmation is required")
        runtime = self.runtimes.trading_runtime(runtime_id, owner_id)
        if runtime is None or runtime.get("mode") != "live_auto":
            raise InvalidInputError("live_auto runtime not found")
        if runtime.get("status") == "stopped":
            raise InvalidInputError("a stopped runtime cannot be armed")
        checks = dict(self.context.preflight(runtime))
        failed = sorted(key for key, value in checks.items() if value is not True)
        if failed:
            raise InvalidInputError("live_auto preflight failed: " + ",".join(failed))
        target = self.context.resolve_target(runtime)
        now = self.now()
        with self._lock:
            active = [arm for arm in self._arms.values() if arm.active(now)]
            if active and active[0].runtime_id != runtime_id:
                raise InvalidInputError("another live_auto runtime is already armed")
            arm = LiveAutoArm(
                arm_id="live-auto-arm:" + sha256(f"{runtime_id}|{now.isoformat()}".encode()).hexdigest(),
                runtime_id=runtime_id, owner_id=owner_id, target=target,
                armed_at=now, expires_at=now + timedelta(seconds=self.arm_ttl_seconds),
            )
            self._arms[runtime_id] = arm
            if self.shared_arms is not None:
                self.shared_arms.arm(CanaryArmSession(
                    arm.arm_id, owner_id, target, arm.armed_at, arm.expires_at,
                    f"live_auto:{runtime_id}",
                    str(runtime.get("execution_target_id") or "") or None,
                ))
        updated = self.runtimes.set_trading_runtime_status(runtime_id, owner_id, "armed")
        assert updated is not None
        self.context.audit("live_auto.armed", updated, {"arm_id": arm.arm_id, "expires_at": arm.expires_at.isoformat()})
        return self.status(updated)

    def disarm(self, runtime_id: str, owner_id: str, reason: str = "operator") -> dict[str, object]:
        runtime = self._runtime(runtime_id, owner_id)
        with self._lock:
            self._arms.pop(runtime_id, None)
            if self.shared_arms is not None:
                self.shared_arms.disarm(owner_id, BrokerAccountRef(str(runtime["broker_name"]), str(runtime["account_id"])), reason=reason)
        updated = self.runtimes.set_trading_runtime_status(runtime_id, owner_id, "paused")
        assert updated is not None
        self.context.audit("live_auto.disarmed", updated, {"reason": reason})
        return self.status(updated)

    def stop(self, runtime_id: str, owner_id: str) -> dict[str, object]:
        self.disarm(runtime_id, owner_id, "runtime_stopped")
        updated = self.runtimes.stop_trading_runtime(runtime_id, owner_id)
        assert updated is not None
        self.context.audit("live_auto.stopped", updated, {"flatten": False})
        return self.status(updated)

    def status(self, runtime: Mapping[str, object]) -> dict[str, object]:
        arm = self._active_arm(str(runtime["runtime_id"]))
        preflight = dict(self.context.preflight(runtime)) if self.enabled else {}
        if arm is not None and (
            preflight.get("no_unknown_orders") is False
            or preflight.get("position_reconciled") is False
        ):
            with self._lock:
                self._arms.pop(str(runtime["runtime_id"]), None)
                if self.shared_arms is not None:
                    self.shared_arms.disarm(arm.owner_id, arm.target, reason="operational_lock")
            updated = self.runtimes.set_trading_runtime_status(
                str(runtime["runtime_id"]), str(runtime["owner_user_id"]), "recovery_locked"
            )
            if updated is not None:
                runtime = updated
            arm = None
        result = dict(runtime)
        result["arm_active"] = arm is not None
        result["arm_expires_at"] = arm.expires_at.isoformat() if arm else None
        result["live_auto_enabled"] = self.enabled
        result["preflight"] = preflight
        return result

    def on_decision(self, runtime: Mapping[str, object], decision: Mapping[str, object], bar: KBar) -> None:
        if runtime.get("mode") != "live_auto" or decision.get("action") not in {"entry", "exit"}:
            return
        owner = str(runtime["owner_user_id"])
        current = self.runtimes.trading_runtime(str(runtime["runtime_id"]), owner)
        if current is None:
            return
        if decision["action"] == "exit":
            try:
                order_id = self.context.request_strategy_exit(current)
            except Exception as exc:
                self._record(decision, owner, "live_rejected", str(exc))
            else:
                self._record(decision, owner, "guardian_exit_requested", "strategy_exit", order_id)
            return
        try:
            order, created = self._entry(current, decision, bar)
        except Exception as exc:
            self._record(decision, owner, "live_rejected", str(exc))
        else:
            self._record(decision, owner, "live_order_reserved" if created else "live_order_idempotent", "approved", order.request.client_order_id)

    def _entry(self, runtime: Mapping[str, object], decision: Mapping[str, object], bar: KBar):
        if not self.enabled or runtime.get("status") != "armed":
            raise RuntimeError("live_auto_not_armed")
        arm = self._active_arm(str(runtime["runtime_id"]))
        if arm is None:
            raise RuntimeError("live_auto_arm_inactive")
        if int(runtime["quantity"]) != 1:
            raise RuntimeError("live_auto_quantity_must_equal_one")
        target = self.context.resolve_target(runtime)
        if target != arm.target:
            raise RuntimeError("live_auto_target_mismatch")
        contract = str(decision["contract"])
        snapshot = runtime.get("strategy_snapshot")
        if not isinstance(snapshot, Mapping) or contract != snapshot.get("execution_contract"):
            raise RuntimeError("live_auto_contract_mismatch")
        position = self.context.active_position(arm.owner_id, target, contract)
        if position is None:
            raise RuntimeError("live_auto_position_unreconciled")
        if position != 0:
            raise RuntimeError("live_auto_pyramiding_blocked")
        if self.context.active_entry_orders(arm.owner_id, target, contract):
            raise RuntimeError("live_auto_active_entry_exists")
        checks = self.context.preflight(runtime)  # dispatch-time recheck
        failed = sorted(key for key, value in checks.items() if value is not True)
        if failed:
            raise RuntimeError("live_auto_dispatch_blocked:" + ",".join(failed))
        direction = str(decision["direction"])
        reference = float(decision.get("reference_price") or bar.close)
        candidate = LiveExecutionCandidate(
            owner_id=arm.owner_id, runtime_id=arm.runtime_id,
            decision_id=str(decision["decision_id"]), target=target,
            strategy_id=str(runtime["strategy_id"]),
            strategy_version=int(runtime.get("strategy_version") or 1),
            symbol=str(runtime["symbol"]), contract=contract,
            trigger_time=datetime.fromisoformat(str(decision["trigger_time"])),
            action="entry", direction=direction, quantity=1,
            reason=str(decision["reason"]),
            planned_stop_price=_risk_stop(runtime, direction, reference),
            session=bar.session,
        )
        quote = self.quotes.get(candidate.symbol, candidate.contract)
        instrument = self.context.instrument(candidate.symbol, candidate.contract)
        approval = self.risk.evaluate(candidate, self.context.risk_context(candidate), quote, instrument)
        result = self.policy.evaluate(candidate, quote, instrument, self.context.capabilities(target), approval)
        if result.request is None:
            raise RuntimeError(result.reason)
        # A second contextual check closes the approval-to-reservation race.
        checks = self.context.preflight(runtime)
        failed = sorted(key for key, value in checks.items() if value is not True)
        if failed or self._active_arm(arm.runtime_id) is None:
            raise RuntimeError("live_auto_dispatch_recheck_failed")
        request = replace(
            result.request,
            client_order_id="live-auto:" + sha256(f"{candidate.decision_id}|{target.public_id}|{result.policy_version}".encode()).hexdigest(),
            source="strategy_live_auto", arm_id=arm.arm_id,
            causation_id=str(decision["decision_id"]),
        )
        return self.sink.reserve(target, request)

    def lock_unknown(self, runtime_id: str, owner_id: str) -> dict[str, object]:
        runtime = self._runtime(runtime_id, owner_id)
        with self._lock:
            self._arms.pop(runtime_id, None)
        updated = self.runtimes.set_trading_runtime_status(runtime_id, owner_id, "recovery_locked")
        assert updated is not None
        self.context.audit("live_auto.unknown_locked", updated, {})
        return self.status(updated)

    def _runtime(self, runtime_id: str, owner_id: str):
        runtime = self.runtimes.trading_runtime(runtime_id, owner_id)
        if runtime is None or runtime.get("mode") != "live_auto":
            raise InvalidInputError("live_auto runtime not found")
        return runtime

    def _active_arm(self, runtime_id: str) -> LiveAutoArm | None:
        with self._lock:
            arm = self._arms.get(runtime_id)
            if arm is not None and not arm.active(self.now()):
                self._arms.pop(runtime_id, None)
                arm = None
            if arm is not None and self.shared_arms is not None:
                runtime = self.runtimes.trading_runtime(arm.runtime_id, arm.owner_id)
                target_id = str(runtime.get("execution_target_id") or "") if runtime else None
                shared = self.shared_arms.active(arm.owner_id, arm.target, self.now(), target_id)
                if shared is None or shared.arm_id != arm.arm_id or shared.created_by != f"live_auto:{runtime_id}":
                    self._arms.pop(runtime_id, None)
                    arm = None
            return arm

    def _record(self, decision, owner, status, reason, order_id=None):
        self.runtimes.update_decision_execution(str(decision["decision_id"]), owner, {
            "execution_status": status, "execution_reason": reason, "order_id": order_id,
        })

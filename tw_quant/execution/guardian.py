from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from typing import Callable, Mapping, Protocol

from .live_models import InstrumentSpec, LiveExecutionCandidate
from .live_policy import EmergencyExitPolicy, ExecutionPolicy, MarketableLimitIOCPolicy
from ..market import ExecutionQuote, ExecutionQuoteView
from ..risk.live import LiveKillSwitchAction, LiveRiskApproval
from ..broker.identity import BrokerAccountRef
from ..broker.guardian_models import (
    GuardianExitReason,
    ManagedLivePosition,
    ManagedPositionState,
    PositionGuardianStore,
)
from ..broker.manager import LiveOrderManager
from ..broker.models import BrokerOrder, BrokerOrderStatus
from ..broker.ports import LiveOrderStore
from tw_quant_core.broker.registry import BrokerRegistry
from ..broker.recovery import RecoveryLockStore
from ..broker.routing import RoutedBrokerOrderRequest
from ..broker.truth import BrokerTruthStore


@dataclass(frozen=True)
class LivePositionGuardianConfig:
    enabled: bool = False
    stop_loss_ticks: int = 20
    take_profit_ticks: int = 40
    quote_stale_seconds: float = 2.0
    poll_seconds: float = 0.25

    def __post_init__(self) -> None:
        if self.stop_loss_ticks < 1 or self.take_profit_ticks < 1:
            raise ValueError("guardian protection ticks must be positive")
        if not 0.1 <= self.quote_stale_seconds <= 30:
            raise ValueError("guardian quote stale threshold is invalid")
        if not 0.05 <= self.poll_seconds <= 5:
            raise ValueError("guardian poll interval is invalid")


class GuardianKillSwitchView(Protocol):
    def actions(self, owner_id: str, target: BrokerAccountRef) -> tuple[LiveKillSwitchAction, ...]: ...


def _position_id(owner_id: str, target: BrokerAccountRef, contract: str) -> str:
    raw = f"{owner_id}|{target.broker_name}|{target.account_id}|{contract}"
    return "live-position:" + sha256(raw.encode()).hexdigest()


def _exit_id(position: ManagedLivePosition, reason: GuardianExitReason) -> str:
    raw = f"{position.position_id}|{position.generation}|{reason.value}"
    return "guardian:" + sha256(raw.encode()).hexdigest()


def _open_position(fills: list[tuple[int, float]]) -> tuple[int, float | None]:
    """Average-cost open position; a fill that reverses direction is rejected."""

    quantity = 0
    average: float | None = None
    for signed, price in fills:
        if signed == 0 or price <= 0:
            raise ValueError("invalid broker fill")
        if quantity == 0 or (quantity > 0) == (signed > 0):
            previous = abs(quantity)
            total = previous + abs(signed)
            average = ((average or 0.0) * previous + price * abs(signed)) / total
            quantity += signed
            continue
        if abs(signed) > abs(quantity):
            raise RuntimeError("guardian_position_reversal_detected")
        quantity += signed
        if quantity == 0:
            average = None
    return quantity, average


class GuardianExecutionSink:
    """Source-restricted durable sink; it never calls a broker directly."""

    def __init__(self, manager: LiveOrderManager) -> None:
        self.manager = manager

    def reserve(self, target: BrokerAccountRef, request) -> tuple[BrokerOrder, bool]:
        if (
            request.source != "live_position_guardian"
            or not request.reduce_only
            or request.purpose not in {"exit", "liquidation"}
        ):
            raise RuntimeError("guardian_reduce_only_required")
        return self.manager.create(RoutedBrokerOrderRequest(target, request))


class LivePositionGuardian:
    """Account-scoped protection orchestrator over durable broker truth.

    The service never polls a broker and never mutates positions. Reconciliation
    supplies truth, quote persistence supplies market events, and every external
    action goes through ``LiveOrderManager`` and its durable outbox.
    """

    def __init__(
        self,
        *,
        account_ref: BrokerAccountRef,
        config: LivePositionGuardianConfig,
        store: PositionGuardianStore,
        order_store: LiveOrderStore,
        truth_store: BrokerTruthStore,
        recovery: RecoveryLockStore,
        registry: BrokerRegistry,
        sink: GuardianExecutionSink,
        quotes: ExecutionQuoteView,
        instrument: InstrumentSpec,
        normal_policy: ExecutionPolicy,
        emergency_policy: ExecutionPolicy,
        readiness: Callable[[], Mapping[str, object]],
        kill_switches: GuardianKillSwitchView | None = None,
        owner_ids: frozenset[str] = frozenset(),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.account_ref = account_ref
        self.config = config
        self.store = store
        self.order_store = order_store
        self.truth_store = truth_store
        self.recovery = recovery
        self.registry = registry
        self.sink = sink
        self.quotes = quotes
        self.instrument = instrument
        self.normal_policy = normal_policy
        self.emergency_policy = emergency_policy
        self.readiness = readiness
        self.kill_switches = kill_switches
        self.owner_ids = owner_ids
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _assert_ready(self) -> None:
        if not self.config.enabled:
            raise RuntimeError("live_position_guardian_disabled")
        self.recovery.assert_ready(
            self.account_ref.broker_name, self.account_ref.account_id
        )
        health = self.readiness()
        if health.get("connected") is not True or health.get("ca_ready") is not True:
            raise RuntimeError("guardian_broker_unavailable")

    def synchronize(self) -> list[ManagedLivePosition]:
        """Rebuild protection only after canonical reconciliation is READY."""

        self._assert_ready()
        snapshot = self.truth_store.get(self.account_ref)
        if snapshot is None or snapshot.account_ref != self.account_ref:
            raise RuntimeError("guardian_broker_truth_unavailable")
        grouped: dict[tuple[str, str], list[tuple[datetime, str, int, float, BrokerOrder]]] = {}
        for fill in snapshot.fills:
            order = self.order_store.get_by_broker_order_id(
                self.account_ref, fill.broker_order_id
            )
            if order is None:
                raise RuntimeError("guardian_orphan_broker_fill")
            signed = fill.quantity if fill.side == "buy" else -fill.quantity
            grouped.setdefault((order.request.owner_id, fill.contract), []).append(
                (fill.occurred_at, fill.fill_id, signed, fill.price, order)
            )

        broker_positions = {item.contract: item.quantity for item in snapshot.positions}
        calculated: dict[str, int] = {}
        synchronized: list[ManagedLivePosition] = []
        now = self.now()
        for (owner_id, contract), values in grouped.items():
            values.sort(key=lambda item: (item[0], item[1]))
            quantity, average = _open_position([(v[2], v[3]) for v in values])
            calculated[contract] = calculated.get(contract, 0) + quantity
            position_id = _position_id(owner_id, self.account_ref, contract)
            if quantity == 0:
                existing = self.store.get(position_id)
                if existing is not None:
                    synchronized.append(self.store.mark_flat(position_id, now))
                continue
            if average is None or contract != self.instrument.contract:
                raise RuntimeError("guardian_instrument_mismatch")
            last_order = values[-1][4]
            movement_stop = self.config.stop_loss_ticks * self.instrument.tick_size
            movement_take = self.config.take_profit_ticks * self.instrument.tick_size
            stop = average - movement_stop if quantity > 0 else average + movement_stop
            take = average + movement_take if quantity > 0 else average - movement_take
            existing = self.store.get(position_id)
            generation = (
                existing.generation
                if existing is not None
                and existing.quantity == quantity
                and abs(existing.average_fill_price - average) < 1e-9
                else (existing.generation + 1 if existing else 1)
            )
            state = ManagedPositionState.ACTIVE
            active_id = active_reason = pending_reason = issue = None
            if existing and existing.active_exit_order_id:
                active_id = existing.active_exit_order_id
                active_reason = existing.active_exit_reason
                pending_reason = existing.pending_exit_reason
                exit_order = self.order_store.get(owner_id, active_id)
                if exit_order is None:
                    state = ManagedPositionState.LOCKED
                    issue = "guardian_exit_reservation_without_order"
                elif exit_order.status is BrokerOrderStatus.UNKNOWN:
                    state = ManagedPositionState.UNKNOWN
                    issue = "guardian_exit_unknown"
                elif exit_order.status.terminal:
                    active_id = None
                    active_reason = None
                    state = ManagedPositionState.ACTIVE
                else:
                    state = ManagedPositionState.EXIT_PENDING
            position = ManagedLivePosition(
                position_id=position_id,
                owner_id=owner_id,
                account_ref=self.account_ref,
                symbol=last_order.request.symbol,
                contract=contract,
                quantity=quantity,
                average_fill_price=average,
                protection_quantity=abs(quantity),
                stop_loss_price=stop,
                take_profit_price=take,
                strategy_id=last_order.request.strategy_id,
                strategy_version=last_order.request.strategy_version,
                state=state,
                generation=generation,
                active_exit_order_id=active_id,
                active_exit_reason=active_reason,
                pending_exit_reason=pending_reason,
                updated_at=now,
                issue_code=issue,
            )
            synchronized.append(self.store.synchronize(position))
        calculated = {key: value for key, value in calculated.items() if value}
        broker_positions = {key: value for key, value in broker_positions.items() if value}
        if calculated != broker_positions:
            raise RuntimeError("guardian_broker_position_mismatch")
        for existing in self.store.positions(self.account_ref):
            if existing.contract not in broker_positions and existing.state is not ManagedPositionState.FLAT:
                synchronized.append(self.store.mark_flat(existing.position_id, now))
        return synchronized

    def evaluate_latest_quotes(self) -> list[BrokerOrder]:
        quote = self.quotes.get(self.instrument.symbol, self.instrument.contract)
        return self.on_quote(quote) if quote is not None else []

    def request_strategy_exit(self, position_id: str) -> tuple[BrokerOrder, bool]:
        position = self.store.get(position_id)
        quote = (
            self.quotes.get(position.symbol, position.contract)
            if position is not None else None
        )
        return self.request_exit(position_id, GuardianExitReason.STRATEGY_EXIT, quote)

    def mark_order_unknown(self, client_order_id: str) -> bool:
        return self.store.mark_exit_unknown(client_order_id, self.now())

    def request_session_end(self, position_id: str) -> tuple[BrokerOrder, bool]:
        position = self.store.get(position_id)
        quote = self.quotes.get(position.symbol, position.contract) if position else None
        return self.request_exit(position_id, GuardianExitReason.SESSION_END, quote)

    def request_contract_roll(self, position_id: str) -> tuple[BrokerOrder, bool]:
        position = self.store.get(position_id)
        quote = self.quotes.get(position.symbol, position.contract) if position else None
        return self.request_exit(position_id, GuardianExitReason.CONTRACT_ROLL, quote)

    def _cancel_working_entries(self, owner_id: str) -> int:
        created = 0
        for order in self.order_store.orders(owner_id, target=self.account_ref):
            if (
                order.request.source in {"manual_live_canary", "strategy_live_auto"}
                and not order.request.reduce_only
                and not order.status.terminal
                and order.broker_order_id
            ):
                _order, reserved = self.sink.manager.request_cancel(
                    self.account_ref, owner_id, order.request.client_order_id
                )
                created += int(reserved)
        return created

    def _has_working_entry(self, owner_id: str) -> bool:
        return any(
            not order.request.reduce_only and not order.status.terminal
            for order in self.order_store.orders(owner_id, target=self.account_ref)
        )

    def on_quote(self, quote: ExecutionQuote) -> list[BrokerOrder]:
        if quote.symbol != self.instrument.symbol or quote.contract != self.instrument.contract:
            return []
        now = self.now()
        if (now - quote.received_at).total_seconds() > self.config.quote_stale_seconds:
            return []
        created: list[BrokerOrder] = []
        for position in self.store.positions(self.account_ref):
            if position.state is not ManagedPositionState.ACTIVE or not quote.complete:
                continue
            executable = quote.best_bid if position.quantity > 0 else quote.best_ask
            assert executable is not None
            reason = None
            if (position.quantity > 0 and executable <= position.stop_loss_price) or (
                position.quantity < 0 and executable >= position.stop_loss_price
            ):
                reason = GuardianExitReason.STOP_LOSS
            elif (position.quantity > 0 and executable >= position.take_profit_price) or (
                position.quantity < 0 and executable <= position.take_profit_price
            ):
                reason = GuardianExitReason.TAKE_PROFIT
            if reason is not None:
                order, was_created = self.request_exit(position.position_id, reason, quote)
                if was_created:
                    created.append(order)
        return created

    def request_exit(
        self,
        position_id: str,
        reason: GuardianExitReason,
        quote: ExecutionQuote | None = None,
    ) -> tuple[BrokerOrder, bool]:
        self._assert_ready()
        position = self.store.get(position_id)
        if position is None or position.account_ref != self.account_ref:
            raise KeyError("unknown managed live position")
        if position.state in {ManagedPositionState.FLAT, ManagedPositionState.UNKNOWN, ManagedPositionState.LOCKED}:
            raise RuntimeError("guardian_position_not_exitable")
        client_order_id = _exit_id(position, reason)
        reserved, created = self.store.reserve_exit(
            position_id, reason, client_order_id, self.now()
        )
        if not created:
            existing = self.order_store.get(position.owner_id, reserved.active_exit_order_id or "")
            if existing is None:
                raise RuntimeError("guardian_exit_already_reserved")
            return existing, False
        try:
            candidate = LiveExecutionCandidate(
                owner_id=position.owner_id,
                runtime_id="live-position-guardian",
                decision_id=client_order_id,
                target=self.account_ref,
                strategy_id=position.strategy_id,
                strategy_version=position.strategy_version,
                symbol=position.symbol,
                contract=position.contract,
                trigger_time=self.now(),
                action="exit",
                direction="long" if position.quantity > 0 else "short",
                quantity=position.protection_quantity,
                reason=f"guardian_{reason.value}",
                planned_stop_price=None,
                session="day",
            )
            approval = LiveRiskApproval(
                True, "guardian_reduce_only_approved", "live-guardian:v1"
            )
            policy = (
                self.emergency_policy
                if reason is GuardianExitReason.EMERGENCY_FLATTEN
                else self.normal_policy
            )
            result = policy.evaluate(
                candidate,
                quote,
                self.instrument,
                self.registry.registration(self.account_ref).capabilities,
                approval,
            )
            if result.request is None:
                raise RuntimeError(result.reason)
            request = replace(
                result.request,
                client_order_id=client_order_id,
                source="live_position_guardian",
                arm_id=None,
                reduce_only=True,
                purpose=(
                    "liquidation"
                    if reason is GuardianExitReason.EMERGENCY_FLATTEN
                    else "exit"
                ),
                reason=f"guardian_{reason.value}",
                correlation_id=position.position_id,
                causation_id=f"guardian-generation:{position.generation}",
            )
            return self.sink.reserve(self.account_ref, request)
        except Exception:
            self.store.release_exit(position_id, client_order_id, "exit_reservation_failed")
            raise

    def process_kill_switches(self) -> list[BrokerOrder]:
        if self.kill_switches is None:
            return []
        created: list[BrokerOrder] = []
        positions = self.store.positions(self.account_ref)
        owners = self.owner_ids | frozenset(item.owner_id for item in positions)
        actions_by_owner = {
            owner_id: self.kill_switches.actions(owner_id, self.account_ref)
            for owner_id in owners
        }
        for owner_id, actions in actions_by_owner.items():
            if any(
                action in actions
                for action in (LiveKillSwitchAction.CANCEL_WORKING, LiveKillSwitchAction.FLATTEN)
            ):
                self._cancel_working_entries(owner_id)
        for position in positions:
            actions = actions_by_owner[position.owner_id]
            if LiveKillSwitchAction.FLATTEN in actions and position.quantity:
                if self._has_working_entry(position.owner_id):
                    continue
                order, was_created = self.request_exit(
                    position.position_id,
                    GuardianExitReason.EMERGENCY_FLATTEN,
                    self.quotes.get(position.symbol, position.contract),
                )
                if was_created:
                    created.append(order)
            elif LiveKillSwitchAction.FLATTEN in actions and not position.quantity:
                self.recovery.force_lock(
                    self.account_ref.broker_name,
                    self.account_ref.account_id,
                    ("emergency_flatten_completed_operator_review_required",),
                    updated_at=self.now(),
                )
        return created

    def public_state(self) -> dict[str, object]:
        positions = self.store.positions(self.account_ref)
        return {
            "enabled": self.config.enabled,
            "platform_managed": True,
            "depends_on_platform_and_broker_connectivity": True,
            "managed_positions": len([item for item in positions if item.quantity]),
            "protected_quantity": sum(item.protection_quantity for item in positions),
            "active_exits": sum(item.active_exit_order_id is not None for item in positions),
            "locked_positions": sum(
                item.state in {ManagedPositionState.LOCKED, ManagedPositionState.UNKNOWN}
                for item in positions
            ),
        }


def default_guardian_policies(config: LivePositionGuardianConfig):
    return (
        MarketableLimitIOCPolicy(max_slippage_ticks=2, max_spread_ticks=4),
        EmergencyExitPolicy(),
    )

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol

from .identity import BrokerAccountRef


LIVE_CANARY_CONFIRMATION = "I_UNDERSTAND_MANUAL_LIVE_CANARY"


@dataclass(frozen=True)
class LiveCanaryConfig:
    """Server-owned limits for the deliberately narrow real-order rollout."""

    enabled: bool = False
    allowed_owner_ids: frozenset[str] = frozenset()
    allowed_broker_accounts: frozenset[BrokerAccountRef] = frozenset()
    allowed_symbols: frozenset[str] = frozenset()
    allowed_contracts: frozenset[str] = frozenset()
    max_quantity: int = 1
    arm_ttl_seconds: int = 600
    protective_stop_ticks: int = 20
    requests_per_minute: int = 4

    def __post_init__(self) -> None:
        if self.max_quantity != 1:
            raise ValueError("first live canary rollout requires max_quantity=1")
        if not 300 <= self.arm_ttl_seconds <= 900:
            raise ValueError("live canary ARM TTL must be between 5 and 15 minutes")
        if self.protective_stop_ticks < 1:
            raise ValueError("live canary protective stop ticks must be positive")
        if not 1 <= self.requests_per_minute <= 10:
            raise ValueError("live canary rate limit must be between 1 and 10")
        if self.enabled and (
            len(self.allowed_owner_ids) != 1
            or len(self.allowed_broker_accounts) != 1
            or len(self.allowed_symbols) != 1
            or len(self.allowed_contracts) != 1
        ):
            raise ValueError(
                "enabled live canary requires exactly one owner, account, symbol, and contract"
            )

    def assert_request_allowed(
        self,
        owner_id: str,
        target: BrokerAccountRef,
        symbol: str,
        contract: str,
        quantity: int,
    ) -> None:
        if not self.enabled:
            raise RuntimeError("live_canary_disabled")
        if owner_id not in self.allowed_owner_ids:
            raise RuntimeError("live_canary_owner_not_allowed")
        if target not in self.allowed_broker_accounts:
            raise RuntimeError("live_canary_target_not_allowed")
        if symbol not in self.allowed_symbols:
            raise RuntimeError("live_canary_symbol_not_allowed")
        if contract not in self.allowed_contracts:
            raise RuntimeError("live_canary_contract_not_allowed")
        if quantity < 1 or quantity > self.max_quantity:
            raise RuntimeError("live_canary_quantity_exceeded")


@dataclass(frozen=True)
class CanaryArmSession:
    arm_id: str
    owner_id: str
    account_ref: BrokerAccountRef
    armed_at: datetime
    expires_at: datetime
    created_by: str
    target_id: str | None = None

    def __post_init__(self) -> None:
        if not self.arm_id.strip() or not self.owner_id.strip() or not self.created_by.strip():
            raise ValueError("live canary ARM identity is required")
        if self.armed_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("live canary ARM timestamps must be timezone-aware")
        if self.expires_at <= self.armed_at:
            raise ValueError("live canary ARM expiry must follow armed_at")

    def active(self, now: datetime) -> bool:
        return self.armed_at <= now < self.expires_at


class CanaryArmStore(Protocol):
    def arm(self, session: CanaryArmSession) -> None: ...

    def active(
        self, owner_id: str, target: BrokerAccountRef, now: datetime,
        target_id: str | None = None,
    ) -> CanaryArmSession | None: ...

    def disarm(self, owner_id: str, target: BrokerAccountRef, *, reason: str) -> bool: ...


@dataclass(frozen=True)
class CanaryOrderAdmissionGate:
    """Contextual API/worker gate; every dispatch re-reads the shared ARM state."""

    config: LiveCanaryConfig
    arms: CanaryArmStore
    target: BrokerAccountRef
    assert_recovery_ready: Callable[[], None]
    readiness: Callable[[], dict[str, object]]
    kill_switch_blocks: Callable[[str, bool], bool]
    now: Callable[[], datetime]
    broker_position: Callable[[str], int | None] | None = None
    execution_target_id: str | None = None

    def assert_ordering_allowed(self) -> None:
        if not self.config.enabled:
            raise RuntimeError("live_canary_disabled")
        self.assert_recovery_ready()

    def assert_order_allowed(self, routed_order: object) -> None:
        from .routing import RoutedBrokerOrder, RoutedBrokerOrderRequest

        if isinstance(routed_order, RoutedBrokerOrderRequest):
            target = routed_order.target
            request = routed_order.request
        elif isinstance(routed_order, RoutedBrokerOrder):
            target = routed_order.target
            request = routed_order.order.request
        else:
            raise RuntimeError("live_canary_invalid_order_context")
        if target != self.target:
            raise RuntimeError("live_canary_target_mismatch")
        if request.source not in {"manual_live_canary", "strategy_live_auto", "live_position_guardian"}:
            raise RuntimeError("live_canary_source_not_allowed")
        guardian_exit = request.source == "live_position_guardian"
        strategy_auto = request.source == "strategy_live_auto"
        if guardian_exit and (
            not request.reduce_only or request.purpose not in {"exit", "liquidation"}
        ):
            raise RuntimeError("guardian_reduce_only_required")
        self.config.assert_request_allowed(
            request.owner_id, target, request.symbol, request.contract, request.quantity
        )
        self.assert_recovery_ready()
        state = self.readiness()
        if state.get("connected") is not True:
            raise RuntimeError("live_canary_broker_disconnected")
        if state.get("ca_ready") is not True:
            raise RuntimeError("live_canary_ca_not_ready")
        if int(state.get("unknown_orders", 0) or 0) > 0:
            raise RuntimeError("live_canary_unknown_order_block")
        if request.reduce_only and self.broker_position is not None:
            position = self.broker_position(request.contract)
            closes_long = position is not None and position > 0 and request.side == "sell"
            closes_short = position is not None and position < 0 and request.side == "buy"
            if not (closes_long or closes_short):
                raise RuntimeError("reduce_only_position_direction_mismatch")
            if request.quantity > abs(position):
                raise RuntimeError("reduce_only_quantity_exceeds_broker_truth")
        if not guardian_exit and self.kill_switch_blocks(request.owner_id, request.reduce_only):
            raise RuntimeError("live_canary_kill_switch_blocked")
        if guardian_exit:
            return
        arm = (
            self.arms.active(request.owner_id, target, self.now(), self.execution_target_id)
            if self.execution_target_id is not None
            else self.arms.active(request.owner_id, target, self.now())
        )
        if arm is None:
            raise RuntimeError("live_canary_arm_inactive")
        if request.arm_id != arm.arm_id:
            raise RuntimeError("live_canary_arm_mismatch")
        if strategy_auto and not arm.created_by.startswith("live_auto:"):
            raise RuntimeError("live_auto_arm_scope_mismatch")

    def assert_cancel_allowed(self, routed_order: object) -> None:
        from .routing import RoutedBrokerOrder

        if not isinstance(routed_order, RoutedBrokerOrder):
            raise RuntimeError("live_canary_invalid_cancel_context")
        request = routed_order.order.request
        if routed_order.target != self.target:
            raise RuntimeError("live_canary_target_mismatch")
        if request.source not in {"manual_live_canary", "strategy_live_auto", "live_position_guardian"}:
            raise RuntimeError("live_canary_source_not_allowed")
        self.config.assert_request_allowed(
            request.owner_id,
            routed_order.target,
            request.symbol,
            request.contract,
            request.quantity,
        )
        self.assert_recovery_ready()
        state = self.readiness()
        if state.get("connected") is not True or state.get("ca_ready") is not True:
            raise RuntimeError("live_canary_broker_unavailable")
        # Cancelling a platform-owned working order is risk reduction. It must
        # remain available after ARM expiry and never applies to external orders.


def arm_expiry(config: LiveCanaryConfig, armed_at: datetime) -> datetime:
    return armed_at + timedelta(seconds=config.arm_ttl_seconds)

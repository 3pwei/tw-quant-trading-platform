from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping, Protocol

from .identity import BrokerAccountRef


class ManagedPositionState(str, Enum):
    ACTIVE = "active"
    EXIT_PENDING = "exit_pending"
    UNKNOWN = "unknown"
    LOCKED = "locked"
    FLAT = "flat"


class GuardianExitReason(str, Enum):
    CONTRACT_ROLL = "contract_roll"
    SESSION_END = "session_end"
    STRATEGY_EXIT = "strategy_exit"
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    EMERGENCY_FLATTEN = "emergency_flatten"

    @property
    def priority(self) -> int:
        return {
            GuardianExitReason.CONTRACT_ROLL: 10,
            GuardianExitReason.SESSION_END: 10,
            GuardianExitReason.STRATEGY_EXIT: 20,
            GuardianExitReason.TAKE_PROFIT: 30,
            GuardianExitReason.STOP_LOSS: 40,
            GuardianExitReason.EMERGENCY_FLATTEN: 50,
        }[self]


@dataclass(frozen=True)
class ManagedLivePosition:
    position_id: str
    owner_id: str
    account_ref: BrokerAccountRef
    symbol: str
    contract: str
    quantity: int
    average_fill_price: float
    protection_quantity: int
    stop_loss_price: float
    take_profit_price: float
    strategy_id: str
    strategy_version: int
    state: ManagedPositionState
    generation: int
    active_exit_order_id: str | None
    active_exit_reason: GuardianExitReason | None
    pending_exit_reason: GuardianExitReason | None
    updated_at: datetime
    issue_code: str | None = None

    def __post_init__(self) -> None:
        if not all((self.position_id, self.owner_id, self.symbol, self.contract)):
            raise ValueError("managed position identity is required")
        if self.quantity == 0 and self.state is not ManagedPositionState.FLAT:
            raise ValueError("zero managed position must be flat")
        if self.quantity != 0 and self.protection_quantity != abs(self.quantity):
            raise ValueError("protection quantity must equal broker-confirmed quantity")
        if min(self.average_fill_price, self.stop_loss_price, self.take_profit_price) <= 0:
            raise ValueError("managed position prices must be positive")
        if self.generation < 1 or self.strategy_version < 1:
            raise ValueError("managed position generations must be positive")
        if self.updated_at.tzinfo is None:
            raise ValueError("managed position timestamp must be timezone-aware")


class PositionGuardianStore(Protocol):
    def synchronize(self, position: ManagedLivePosition) -> ManagedLivePosition: ...
    def mark_flat(self, position_id: str, occurred_at: datetime) -> ManagedLivePosition: ...
    def get(self, position_id: str) -> ManagedLivePosition | None: ...
    def positions(self, target: BrokerAccountRef) -> list[ManagedLivePosition]: ...
    def reserve_exit(
        self,
        position_id: str,
        reason: GuardianExitReason,
        client_order_id: str,
        occurred_at: datetime,
    ) -> tuple[ManagedLivePosition, bool]: ...
    def release_exit(self, position_id: str, client_order_id: str, reason: str) -> None: ...
    def lock(self, position_id: str, issue_code: str, occurred_at: datetime) -> None: ...
    def mark_exit_unknown(self, client_order_id: str, occurred_at: datetime) -> bool: ...
    def audit(
        self, position_id: str, action: str, detail: Mapping[str, object], occurred_at: datetime
    ) -> None: ...

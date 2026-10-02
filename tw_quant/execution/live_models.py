from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from ..broker.identity import BrokerAccountRef


@dataclass(frozen=True)
class InstrumentSpec:
    """Canonical contract metadata consumed by broker-neutral policies."""

    symbol: str
    contract: str
    tick_size: float
    multiplier: float
    expiry_date: date | None = None
    commission_per_side: float = 0.0
    tax_rate: float = 0.0

    def __post_init__(self) -> None:
        if not self.symbol.strip() or not self.contract.strip():
            raise ValueError("instrument symbol and contract are required")
        if self.tick_size <= 0 or self.multiplier <= 0:
            raise ValueError("instrument tick size and multiplier must be positive")
        if self.commission_per_side < 0 or self.tax_rate < 0:
            raise ValueError("instrument costs cannot be negative")


@dataclass(frozen=True)
class LiveExecutionCandidate:
    """Broker-blind strategy decision enriched with one immutable live target."""

    owner_id: str
    runtime_id: str
    decision_id: str
    target: BrokerAccountRef
    strategy_id: str
    strategy_version: int
    symbol: str
    contract: str
    trigger_time: datetime
    action: Literal["entry", "exit"]
    direction: Literal["long", "short"]
    quantity: int
    reason: str
    planned_stop_price: float | None
    session: Literal["day", "night"]

    def __post_init__(self) -> None:
        required = (
            self.owner_id,
            self.runtime_id,
            self.decision_id,
            self.strategy_id,
            self.symbol,
            self.contract,
            self.reason,
        )
        if any(not value.strip() for value in required):
            raise ValueError("live candidate identity fields are required")
        if self.strategy_version < 1 or self.quantity < 1:
            raise ValueError("strategy version and quantity must be positive")
        if self.trigger_time.tzinfo is None:
            raise ValueError("trigger_time must be timezone-aware")

    @property
    def reduce_only(self) -> bool:
        return self.action == "exit"

    @property
    def side(self) -> Literal["buy", "sell"]:
        if self.reduce_only:
            return "sell" if self.direction == "long" else "buy"
        return "buy" if self.direction == "long" else "sell"

    @property
    def signed_quantity(self) -> int:
        return self.quantity if self.side == "buy" else -self.quantity

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal


OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop"]
OrderPurpose = Literal["entry", "exit", "liquidation"]
TimeInForce = Literal["rod", "ioc", "fok"]


class ExecutionMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class BrokerOrderStatus(str, Enum):
    """Canonical lifecycle shared by Paper views and future live adapters."""

    CREATED = "created"
    RISK_APPROVED = "risk_approved"
    SUBMITTING = "submitting"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"

    @property
    def terminal(self) -> bool:
        return self in {
            self.FILLED,
            self.CANCELLED,
            self.REJECTED,
            self.EXPIRED,
        }


PAPER_STATUS_MAP = {
    "pending_risk": BrokerOrderStatus.CREATED,
    "approved": BrokerOrderStatus.RISK_APPROVED,
    "rejected": BrokerOrderStatus.REJECTED,
    "filled": BrokerOrderStatus.FILLED,
}


def canonical_paper_status(status: str) -> BrokerOrderStatus:
    try:
        return PAPER_STATUS_MAP[status]
    except KeyError as exc:
        raise ValueError(f"unknown paper order status: {status}") from exc


@dataclass(frozen=True)
class BrokerOrderRequest:
    """Risk-approved, server-derived request passed to a broker adapter."""

    client_order_id: str
    owner_id: str
    strategy_id: str
    strategy_version: int
    symbol: str
    contract: str
    side: OrderSide
    quantity: int
    mode: ExecutionMode
    order_type: OrderType = "market"
    time_in_force: TimeInForce = "rod"
    limit_price: float | None = None
    stop_price: float | None = None
    reference_price: float | None = None
    risk_stop_price: float | None = None
    reduce_only: bool = False
    purpose: OrderPurpose = "entry"
    reason: str = "order_request"
    correlation_id: str | None = None
    causation_id: str | None = None
    source: str = "unspecified"
    arm_id: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.client_order_id,
            self.owner_id,
            self.strategy_id,
            self.symbol,
            self.contract,
        )
        if any(not value.strip() for value in required):
            raise ValueError("order identifiers, owner, strategy, and contract are required")
        if len(self.client_order_id) > 128:
            raise ValueError("client_order_id cannot exceed 128 characters")
        if self.strategy_version < 1 or self.quantity < 1:
            raise ValueError("strategy_version and quantity must be positive")
        if self.side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if self.order_type not in {"market", "limit", "stop"}:
            raise ValueError("unsupported order_type")
        if self.time_in_force not in {"rod", "ioc", "fok"}:
            raise ValueError("unsupported time_in_force")
        if self.purpose not in {"entry", "exit", "liquidation"}:
            raise ValueError("unsupported order purpose")
        if not self.reason.strip():
            raise ValueError("order reason is required")
        if not self.source.strip():
            raise ValueError("order source is required")
        if self.source == "manual_live_canary" and not self.arm_id:
            raise ValueError("manual live canary orders require arm_id")
        if self.reduce_only and self.purpose == "entry":
            raise ValueError("reduce_only orders cannot have entry purpose")
        if not isinstance(self.mode, ExecutionMode):
            raise ValueError("mode must be an ExecutionMode")
        if self.order_type == "limit" and not self.limit_price:
            raise ValueError("limit orders require limit_price")
        if self.order_type == "stop" and not self.stop_price:
            raise ValueError("stop orders require stop_price")
        for price in (
            self.limit_price,
            self.stop_price,
            self.reference_price,
            self.risk_stop_price,
        ):
            if price is not None and price <= 0:
                raise ValueError("order prices must be positive")


@dataclass(frozen=True)
class BrokerOrder:
    request: BrokerOrderRequest
    status: BrokerOrderStatus
    updated_at: datetime
    broker_order_id: str | None = None
    filled_quantity: int = 0
    average_fill_price: float | None = None
    status_reason: str | None = None

    def __post_init__(self) -> None:
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        if not 0 <= self.filled_quantity <= self.request.quantity:
            raise ValueError("filled_quantity must be within requested quantity")
        if self.average_fill_price is not None and self.average_fill_price <= 0:
            raise ValueError("average_fill_price must be positive")
        if self.filled_quantity and self.average_fill_price is None:
            raise ValueError("filled orders require average_fill_price")
        if self.status is BrokerOrderStatus.FILLED and (
            self.filled_quantity != self.request.quantity
        ):
            raise ValueError("filled status requires the full requested quantity")


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    broker_name: str
    account_id: str | None
    trading_enabled: bool
    mode: ExecutionMode | None

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .models import BrokerOrder, BrokerOrderStatus


class InvalidOrderTransition(ValueError):
    pass


_ALLOWED_TRANSITIONS: dict[BrokerOrderStatus, frozenset[BrokerOrderStatus]] = {
    BrokerOrderStatus.CREATED: frozenset({
        BrokerOrderStatus.RISK_APPROVED,
        BrokerOrderStatus.REJECTED,
    }),
    BrokerOrderStatus.RISK_APPROVED: frozenset({
        BrokerOrderStatus.SUBMITTING,
        BrokerOrderStatus.REJECTED,
        BrokerOrderStatus.UNKNOWN,
    }),
    BrokerOrderStatus.SUBMITTING: frozenset({
        BrokerOrderStatus.ACCEPTED,
        BrokerOrderStatus.PARTIALLY_FILLED,
        BrokerOrderStatus.FILLED,
        BrokerOrderStatus.REJECTED,
        BrokerOrderStatus.UNKNOWN,
    }),
    BrokerOrderStatus.ACCEPTED: frozenset({
        BrokerOrderStatus.PARTIALLY_FILLED,
        BrokerOrderStatus.FILLED,
        BrokerOrderStatus.CANCEL_PENDING,
        BrokerOrderStatus.CANCELLED,
        BrokerOrderStatus.REJECTED,
        BrokerOrderStatus.EXPIRED,
        BrokerOrderStatus.UNKNOWN,
    }),
    BrokerOrderStatus.PARTIALLY_FILLED: frozenset({
        BrokerOrderStatus.PARTIALLY_FILLED,
        BrokerOrderStatus.FILLED,
        BrokerOrderStatus.CANCEL_PENDING,
        BrokerOrderStatus.CANCELLED,
        BrokerOrderStatus.EXPIRED,
        BrokerOrderStatus.UNKNOWN,
    }),
    BrokerOrderStatus.CANCEL_PENDING: frozenset({
        BrokerOrderStatus.CANCELLED,
        BrokerOrderStatus.PARTIALLY_FILLED,
        BrokerOrderStatus.FILLED,
        BrokerOrderStatus.UNKNOWN,
    }),
    BrokerOrderStatus.UNKNOWN: frozenset({
        BrokerOrderStatus.ACCEPTED,
        BrokerOrderStatus.PARTIALLY_FILLED,
        BrokerOrderStatus.FILLED,
        BrokerOrderStatus.CANCELLED,
        BrokerOrderStatus.REJECTED,
        BrokerOrderStatus.EXPIRED,
    }),
}


def transition_order(
    order: BrokerOrder,
    status: BrokerOrderStatus,
    *,
    updated_at: datetime,
    broker_order_id: str | None = None,
    filled_quantity: int | None = None,
    average_fill_price: float | None = None,
    status_reason: str | None = None,
) -> BrokerOrder:
    """Return the next immutable state, rejecting impossible lifecycle jumps."""

    if updated_at < order.updated_at:
        raise InvalidOrderTransition("order lifecycle time cannot move backwards")
    if filled_quantity is not None and filled_quantity < order.filled_quantity:
        raise InvalidOrderTransition("filled quantity cannot decrease")
    if status == order.status and status is not BrokerOrderStatus.PARTIALLY_FILLED:
        return order
    if status not in _ALLOWED_TRANSITIONS.get(order.status, frozenset()):
        raise InvalidOrderTransition(f"{order.status.value} -> {status.value}")
    return replace(
        order,
        status=status,
        updated_at=updated_at,
        broker_order_id=broker_order_id or order.broker_order_id,
        filled_quantity=(
            order.filled_quantity if filled_quantity is None else filled_quantity
        ),
        average_fill_price=(
            order.average_fill_price
            if average_fill_price is None
            else average_fill_price
        ),
        status_reason=status_reason,
    )

"""Reviewed Platform facade for canonical Core event contracts."""

from tw_quant_core.events import (
    BarClosedEvent,
    Direction,
    DomainEvent,
    EventKind,
    EventMetadata,
    FillEvent,
    MarketEvent,
    OrderIntent,
    OrderSide,
    OrderStatusEvent,
    PositionEvent,
    RiskDecision,
    SessionEvent,
    SignalEvent,
    deterministic_event_id,
    event_to_dict,
)

USING_PUBLIC_CORE = True

__all__ = [
    "BarClosedEvent", "Direction", "DomainEvent", "EventKind", "EventMetadata",
    "FillEvent", "MarketEvent", "OrderIntent", "OrderSide", "OrderStatusEvent",
    "PositionEvent", "RiskDecision", "SessionEvent", "SignalEvent",
    "USING_PUBLIC_CORE", "deterministic_event_id", "event_to_dict",
]

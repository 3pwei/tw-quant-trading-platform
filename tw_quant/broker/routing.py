from __future__ import annotations

from dataclasses import dataclass

from .identity import BrokerAccountRef
from .models import BrokerOrder, BrokerOrderRequest, ExecutionMode


@dataclass(frozen=True)
class RoutedBrokerOrderRequest:
    """Live-only envelope that makes the execution target explicit."""

    target: BrokerAccountRef
    request: BrokerOrderRequest

    def __post_init__(self) -> None:
        if self.request.mode is not ExecutionMode.LIVE:
            raise ValueError("routed broker orders must use live execution mode")


@dataclass(frozen=True)
class RoutedBrokerOrder:
    """A durable live order together with its immutable execution target."""

    target: BrokerAccountRef
    order: BrokerOrder

    def __post_init__(self) -> None:
        if self.order.request.mode is not ExecutionMode.LIVE:
            raise ValueError("routed broker orders must use live execution mode")

from __future__ import annotations

from dataclasses import dataclass

from ..broker import (
    BrokerAccountRef,
    BrokerOrder,
    BrokerOrderRequest,
    LiveOrderManager,
    RoutedBrokerOrderRequest,
)


@dataclass(frozen=True)
class LiveExecutionSink:
    """The only real sink: durable manager reservation, never direct broker I/O."""

    manager: LiveOrderManager

    def reserve(
        self, target: BrokerAccountRef, request: BrokerOrderRequest
    ) -> tuple[BrokerOrder, bool]:
        if request.source != "manual_live_canary":
            raise RuntimeError("real_execution_source_not_allowed")
        return self.manager.create(RoutedBrokerOrderRequest(target, request))

    def reserve_cancel(
        self, target: BrokerAccountRef, owner_id: str, client_order_id: str
    ) -> tuple[BrokerOrder, bool]:
        return self.manager.request_cancel(target, owner_id, client_order_id)


@dataclass(frozen=True)
class StrategyLiveExecutionSink:
    """Dedicated real sink for Strategy Auto Live entry orders only."""

    manager: LiveOrderManager

    def reserve(
        self, target: BrokerAccountRef, request: BrokerOrderRequest
    ) -> tuple[BrokerOrder, bool]:
        if (
            request.source != "strategy_live_auto"
            or request.reduce_only
            or request.purpose != "entry"
            or not request.arm_id
        ):
            raise RuntimeError("strategy_live_entry_source_required")
        return self.manager.create(RoutedBrokerOrderRequest(target, request))

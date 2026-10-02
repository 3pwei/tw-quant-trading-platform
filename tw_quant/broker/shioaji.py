from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Mapping, Protocol

from .lifecycle import transition_order
from .models import (
    BrokerOrder,
    BrokerOrderRequest,
    BrokerOrderStatus,
    ExecutionMode,
)
from .safety import LIVE_TRADING_CONFIRMATION


LIVE_CONFIRMATION = LIVE_TRADING_CONFIRMATION
ExternalReportStatus = Literal[
    "accepted",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
    "expired",
]


@dataclass(frozen=True)
class ExternalOrderReport:
    broker_order_id: str | None
    status: ExternalReportStatus
    occurred_at: datetime
    filled_quantity: int = 0
    average_fill_price: float | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if self.filled_quantity < 0:
            raise ValueError("filled_quantity cannot be negative")
        if self.filled_quantity and not self.average_fill_price:
            raise ValueError("filled reports require average_fill_price")


class ShioajiExecutionClient(Protocol):
    """Normalized SDK seam. Concrete SDK calls belong outside the domain core."""

    async def account_state(self) -> Mapping[str, object]: ...

    async def positions(self) -> list[Mapping[str, object]]: ...

    async def submit(self, request: BrokerOrderRequest) -> ExternalOrderReport: ...

    async def cancel(self, broker_order_id: str) -> ExternalOrderReport: ...

    async def order(self, broker_order_id: str) -> ExternalOrderReport | None: ...

    async def order_by_client_id(
        self, client_order_id: str
    ) -> ExternalOrderReport | None: ...


@dataclass(frozen=True)
class LiveTradingSafety:
    account_id: str
    enabled: bool = False
    confirmation: str = ""
    allowed_account_ids: frozenset[str] = frozenset()

    def assert_ordering_allowed(self) -> None:
        if not self.enabled:
            raise RuntimeError("live order execution is disabled")
        if self.confirmation != LIVE_CONFIRMATION:
            raise RuntimeError("live trading confirmation is invalid")
        if self.account_id not in self.allowed_account_ids:
            raise RuntimeError("live broker account is not allowlisted")


_REPORT_STATUS = {
    "accepted": BrokerOrderStatus.ACCEPTED,
    "partially_filled": BrokerOrderStatus.PARTIALLY_FILLED,
    "filled": BrokerOrderStatus.FILLED,
    "cancelled": BrokerOrderStatus.CANCELLED,
    "rejected": BrokerOrderStatus.REJECTED,
    "expired": BrokerOrderStatus.EXPIRED,
}


class ShioajiBrokerAdapter:
    """Fail-closed live adapter backed by a durable caller-owned outbox.

    Durable outbox reservation remains the caller's responsibility. An ambiguous
    SDK failure becomes UNKNOWN and is never automatically resubmitted.
    """

    broker_name = "shioaji"

    def __init__(self, client: ShioajiExecutionClient, safety: LiveTradingSafety):
        self.client = client
        self.safety = safety

    async def account_state(self) -> Mapping[str, object]:
        state = dict(await self.client.account_state())
        state.update({
            "broker": self.broker_name,
            "trading_enabled": self._trading_enabled(),
        })
        return state

    async def positions(self) -> list[Mapping[str, object]]:
        return await self.client.positions()

    def _trading_enabled(self) -> bool:
        try:
            self.safety.assert_ordering_allowed()
        except RuntimeError:
            return False
        return True

    @staticmethod
    def _created(request: BrokerOrderRequest, occurred_at: datetime) -> BrokerOrder:
        order = BrokerOrder(
            request=request,
            status=BrokerOrderStatus.CREATED,
            updated_at=occurred_at,
        )
        return transition_order(
            order,
            BrokerOrderStatus.RISK_APPROVED,
            updated_at=occurred_at,
            status_reason="risk_approved_before_adapter",
        )

    @staticmethod
    def _apply_report(order: BrokerOrder, report: ExternalOrderReport) -> BrokerOrder:
        return transition_order(
            order,
            _REPORT_STATUS[report.status],
            # Broker event time may precede the platform receive time because of
            # network latency or clock skew. Lifecycle storage must stay monotonic.
            updated_at=max(order.updated_at, report.occurred_at),
            broker_order_id=report.broker_order_id,
            filled_quantity=report.filled_quantity,
            average_fill_price=report.average_fill_price,
            status_reason=report.reason,
        )

    async def submit_order(self, request: BrokerOrderRequest) -> BrokerOrder:
        self.safety.assert_ordering_allowed()
        if request.mode is not ExecutionMode.LIVE:
            raise ValueError("ShioajiBrokerAdapter only accepts live orders")
        now = datetime.now().astimezone()
        order = transition_order(
            self._created(request, now),
            BrokerOrderStatus.SUBMITTING,
            updated_at=now,
            status_reason="submitting_to_broker",
        )
        try:
            report = await self.client.submit(request)
        except Exception:
            unknown = transition_order(
                order,
                BrokerOrderStatus.UNKNOWN,
                updated_at=datetime.now().astimezone(),
                status_reason="broker_submission_result_unknown",
            )
            return unknown
        return self._apply_report(order, report)

    async def cancel_order(self, order: BrokerOrder) -> BrokerOrder:
        self.safety.assert_ordering_allowed()
        if order.status.terminal:
            return order
        if not order.broker_order_id:
            raise RuntimeError("order has no broker_order_id; reconcile before cancelling")
        pending = transition_order(
            order,
            BrokerOrderStatus.CANCEL_PENDING,
            updated_at=datetime.now().astimezone(),
            status_reason="cancel_requested",
        )
        try:
            report = await self.client.cancel(order.broker_order_id)
        except Exception:
            unknown = transition_order(
                pending,
                BrokerOrderStatus.UNKNOWN,
                updated_at=datetime.now().astimezone(),
                status_reason="broker_cancel_result_unknown",
            )
            return unknown
        return self._apply_report(pending, report)

    async def refresh_order(self, order: BrokerOrder) -> BrokerOrder:
        report = (
            await self.client.order(order.broker_order_id)
            if order.broker_order_id
            else await self.client.order_by_client_id(
                order.request.client_order_id
            )
        )
        if report is None:
            return transition_order(
                order,
                BrokerOrderStatus.UNKNOWN,
                updated_at=datetime.now().astimezone(),
                status_reason="broker_order_not_found",
            )
        return self._apply_report(order, report)

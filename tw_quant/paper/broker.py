from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Mapping

from ..auth import AccountStatus, AuthUser, TradingMode
from ..broker import (
    BrokerOrder,
    BrokerOrderRequest,
    BrokerOrderStatus,
    ExecutionMode,
    canonical_paper_status,
)
from ..market import KBar
from .service import PaperTradingService


class PaperBrokerAdapter:
    """Account-scoped BrokerPort adapter over the existing Paper use case."""

    broker_name = "paper"

    def __init__(
        self,
        service: PaperTradingService,
        user: AuthUser,
        market_bar: KBar,
    ):
        self.service = service
        self.user = user
        self.market_bar = market_bar

    async def account_state(self) -> Mapping[str, object]:
        enabled = (
            self.user.status is AccountStatus.ACTIVE
            and self.user.trading_mode is TradingMode.PAPER
            and "orders.paper" in self.user.permissions
        )
        return {
            **self.service.account(self.user.user_id),
            "broker": self.broker_name,
            "trading_enabled": enabled,
        }

    async def positions(self) -> list[Mapping[str, object]]:
        return self.service.positions(self.user.user_id)

    def _order(
        self, request: BrokerOrderRequest, record: Mapping[str, object]
    ) -> BrokerOrder:
        status = canonical_paper_status(str(record["status"]))
        fill_id = record.get("fill_id")
        fill = (
            self.service.fill(self.user.user_id, str(fill_id))
            if fill_id is not None else None
        )
        approved_quantity = int(record.get("approved_quantity", 0))
        normalized_request = (
            replace(request, quantity=approved_quantity)
            if approved_quantity > 0 else request
        )
        updated_at = (
            datetime.fromisoformat(str(fill["meta"]["occurred_at"]))
            if fill is not None else datetime.fromisoformat(str(record["submitted_at"]))
        )
        return BrokerOrder(
            request=normalized_request,
            status=status,
            updated_at=updated_at,
            broker_order_id=str(record["order_id"]),
            filled_quantity=(
                int(fill["quantity"])
                if fill is not None and status is BrokerOrderStatus.FILLED else 0
            ),
            average_fill_price=(
                float(fill["price"])
                if fill is not None and status is BrokerOrderStatus.FILLED else None
            ),
            status_reason=str(record["status_reason"]),
        )

    async def submit_order(self, request: BrokerOrderRequest) -> BrokerOrder:
        record, _created = self.service.submit_request(
            self.user,
            request,
            market_bar=self.market_bar,
        )
        return self._order(request, record)

    async def cancel_order(self, order: BrokerOrder) -> BrokerOrder:
        if order.status.terminal:
            return order
        raise RuntimeError("Paper Trading does not support pending order cancellation")

    async def refresh_order(self, order: BrokerOrder) -> BrokerOrder:
        record = self.service.order_for_client_id(
            self.user.user_id, order.request.client_order_id
        )
        return self._order(order.request, record) if record is not None else order

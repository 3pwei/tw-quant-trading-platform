from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from ...auth import AuthUser
from ...broker import BrokerOrderRequest, ExecutionMode
from ...paper import IdempotencyConflict, PaperTradingService
from ..service import LiveMarketService
from ..storage import MarketRepository
from .errors import (
    InvalidInputError,
    ResourceConflictError,
    ServiceUnavailableError,
)


@dataclass(frozen=True)
class PaperOrderInput:
    client_order_id: str
    strategy_id: str
    strategy_version: int
    side: Literal["buy", "sell"]
    quantity: int
    stop_loss_price: float | None
    reduce_only: bool


class PaperApplicationService:
    """Paper use cases with server-owned market and account context."""

    def __init__(
        self,
        repository: MarketRepository,
        paper: PaperTradingService,
        market: LiveMarketService,
        symbol: str,
        stale_after_seconds: float,
    ):
        self.market_repository = repository
        self.paper = paper
        self.market = market
        self.symbol = symbol
        self.stale_after_seconds = stale_after_seconds

    def account(self, owner_id: str) -> dict[str, object]:
        return {
            "mode": "paper",
            "account": self.paper.account(owner_id),
            "positions": self.paper.positions(owner_id),
        }

    def orders(self, owner_id: str) -> dict[str, object]:
        return {"orders": self.paper.orders(owner_id)}

    def fills(self, owner_id: str, limit: int) -> dict[str, object]:
        return {"fills": self.paper.fills(owner_id, limit)}

    def events(self, owner_id: str, limit: int) -> dict[str, object]:
        return {"events": self.paper.events(owner_id, limit)}

    def submit(
        self, user: AuthUser, order_input: PaperOrderInput
    ) -> dict[str, object]:
        market = self.market.status_message()
        block_reason = market.get("trading_block_reason")
        if not order_input.reduce_only and block_reason:
            self.paper.record_market_block()
            detail = (
                "market data provider is disconnected; new positions are blocked"
                if block_reason == "provider_disconnected"
                else "market data is stale; new positions are blocked"
            )
            raise ServiceUnavailableError(detail)

        latest = self.market_repository.latest(self.symbol, 1)
        if not latest:
            self.paper.record_market_block()
            raise ServiceUnavailableError("market price is not available")
        market_bar = latest[0]
        quote_age = (
            datetime.now(market_bar.received_time.tzinfo)
            - market_bar.received_time
        )
        if quote_age > timedelta(seconds=self.stale_after_seconds):
            self.paper.record_market_block()
            raise ServiceUnavailableError("market price is stale")

        try:
            order, created = self.paper.submit_request(
                user,
                BrokerOrderRequest(
                    client_order_id=order_input.client_order_id.strip(),
                    owner_id=user.user_id,
                    strategy_id=order_input.strategy_id.strip(),
                    strategy_version=order_input.strategy_version,
                    symbol=market_bar.symbol,
                    contract=market_bar.contract,
                    side=order_input.side,
                    quantity=order_input.quantity,
                    mode=ExecutionMode.PAPER,
                    reference_price=market_bar.close,
                    risk_stop_price=order_input.stop_loss_price,
                    reduce_only=order_input.reduce_only,
                    purpose="exit" if order_input.reduce_only else "entry",
                    reason="manual_paper_order",
                ),
                market_bar=market_bar,
            )
        except IdempotencyConflict as exc:
            raise ResourceConflictError(str(exc)) from exc
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        return {"created": created, "order": order}

    def activate_kill_switch(
        self, owner_id: str, reason: str
    ) -> dict[str, object]:
        try:
            self.paper.activate_kill_switch(owner_id, reason.strip())
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        return self.paper.account(owner_id)

    def reset_kill_switch(
        self, owner_id: str, reason: str
    ) -> dict[str, object]:
        try:
            self.paper.reset_kill_switch(owner_id, reason.strip())
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        return self.paper.account(owner_id)

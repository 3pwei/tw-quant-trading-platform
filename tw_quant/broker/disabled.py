from __future__ import annotations

from typing import Mapping

from .models import BrokerOrder, BrokerOrderRequest


class DisabledBroker:
    """Safe default for the quote-only phase: every order is rejected."""

    broker_name = "disabled"

    async def account_state(self) -> Mapping[str, object]:
        return {"broker": self.broker_name, "trading_enabled": False}

    async def positions(self) -> list[Mapping[str, object]]:
        return []

    async def submit_order(self, order: BrokerOrderRequest) -> BrokerOrder:
        raise RuntimeError("live order execution is disabled")

    async def cancel_order(self, order: BrokerOrder) -> BrokerOrder:
        raise RuntimeError("live order execution is disabled")

    async def refresh_order(self, order: BrokerOrder) -> BrokerOrder:
        raise RuntimeError("live order execution is disabled")


class LockedBroker:
    """Target-scoped terminal adapter used when production execution is locked."""

    def __init__(self, broker_name: str):
        normalized = broker_name.strip().lower()
        if not normalized or normalized == "disabled":
            raise ValueError("a concrete broker name is required")
        self.broker_name = normalized

    async def account_state(self) -> Mapping[str, object]:
        return {"broker": self.broker_name, "trading_enabled": False}

    async def positions(self) -> list[Mapping[str, object]]:
        return []

    async def submit_order(self, order: BrokerOrderRequest) -> BrokerOrder:
        raise RuntimeError("production broker submission is not implemented")

    async def cancel_order(self, order: BrokerOrder) -> BrokerOrder:
        raise RuntimeError("production broker cancellation is not implemented")

    async def refresh_order(self, order: BrokerOrder) -> BrokerOrder:
        raise RuntimeError("production broker refresh is not implemented")

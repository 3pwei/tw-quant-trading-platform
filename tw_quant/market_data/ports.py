from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from ..market import ConnectionStatus, ExecutionQuote, KBar, TickEvent


TickCallback = Callable[[TickEvent], None]
StatusCallback = Callable[[ConnectionStatus], None]
ExecutionQuoteCallback = Callable[[ExecutionQuote], None]


@dataclass(frozen=True)
class ProviderCapabilities:
    """Capabilities exposed by a market-data adapter."""

    live_ticks: bool
    historical_bars: bool
    heartbeat: bool = True


class LiveMarketDataProvider(Protocol):
    """Streaming quote port consumed by the live market service."""

    provider_name: str
    symbol: str
    contract: str
    capabilities: ProviderCapabilities

    async def start(
        self, on_tick: TickCallback, on_status: StatusCallback
    ) -> None: ...

    async def stop(self) -> None: ...

    async def heartbeat(self) -> bool: ...


class HistoricalMarketDataProvider(Protocol):
    """Historical bar capability; providers may implement this separately."""

    async def load_history(self, limit: int) -> list[KBar]: ...


class ExecutionQuotePublisher(Protocol):
    """Optional BidAsk publisher; providers without true depth omit this port."""

    def set_execution_quote_callback(
        self, callback: ExecutionQuoteCallback
    ) -> None: ...


class MarketDataProvider(LiveMarketDataProvider, HistoricalMarketDataProvider, Protocol):
    """Current application port requiring live ticks and optional backfill."""

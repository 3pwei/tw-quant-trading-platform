from __future__ import annotations

from .ports import LiveMarketDataProvider
from .providers import ReplayMarketDataProvider, ShioajiMarketDataProvider
from .settings import MarketDataSettings


def build_market_data_provider(
    settings: MarketDataSettings,
) -> LiveMarketDataProvider:
    """Composition root for market-data adapters."""

    settings.validate()
    if settings.provider == "replay":
        return ReplayMarketDataProvider(
            settings.replay_csv,
            settings.replay_speed,
        )
    if settings.provider == "shioaji":
        return ShioajiMarketDataProvider(
            api_key=settings.shioaji_api_key or "",
            secret_key=settings.shioaji_secret_key or "",
            symbol=settings.symbol,
            contract=settings.contract,
            production=settings.shioaji_production,
            history_days=settings.history_days,
        )
    raise ValueError(f"unsupported market-data provider: {settings.provider}")

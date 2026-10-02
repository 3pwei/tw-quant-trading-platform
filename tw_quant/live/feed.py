"""Compatibility imports for the provider-neutral market-data package.

New code should import from :mod:`tw_quant.market_data`. These aliases keep
existing integrations working during the migration.
"""

from ..market_data.ports import (
    MarketDataProvider as MarketFeed,
    StatusCallback,
    TickCallback,
)
from ..market_data.providers.replay import (
    ReplayMarketDataProvider as ReplayFeed,
    parse_exchange_time,
)
from ..market_data.providers.shioaji import ShioajiMarketDataProvider as ShioajiFeed

__all__ = [
    "MarketFeed",
    "ReplayFeed",
    "ShioajiFeed",
    "StatusCallback",
    "TickCallback",
    "parse_exchange_time",
]

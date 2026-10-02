from __future__ import annotations

from datetime import datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import unittest

from tw_quant.market import ExecutionQuoteCache, TAIPEI
from tw_quant.market_data.providers.shioaji import ShioajiMarketDataProvider


class ImmediateLoop:
    def call_soon_threadsafe(self, callback, value):
        callback(value)


class FakeQuoteApi:
    def __init__(self):
        self.tick_callback = None
        self.bidask_callback = None
        self.subscriptions: list[str] = []

    def set_on_tick_fop_v1_callback(self, callback):
        self.tick_callback = callback

    def set_on_bidask_fop_v1_callback(self, callback):
        self.bidask_callback = callback

    def set_event_callback(self, _callback):
        return None

    def subscribe(self, _contract, *, quote_type, version):
        self.subscriptions.append(f"{quote_type}:{version}")


class FakeApi:
    def __init__(self):
        contract = SimpleNamespace(code="TMFR1", target_code="TMF202610")
        self.Contracts = SimpleNamespace(Futures=SimpleNamespace(
            TMF=SimpleNamespace(TMFR1=contract),
        ))
        self.quote = FakeQuoteApi()

    def login(self, **_kwargs):
        return None


class ExecutionQuoteTests(unittest.TestCase):
    def test_shioaji_bidask_is_normalized_to_memory_without_fake_spread(self):
        api = FakeApi()
        module = ModuleType("shioaji")
        module.Shioaji = lambda *, simulation: api
        module.constant = SimpleNamespace(
            QuoteType=SimpleNamespace(Tick="tick", BidAsk="bidask"),
            QuoteVersion=SimpleNamespace(v1="v1"),
        )
        provider = ShioajiMarketDataProvider(
            "api", "secret", "missing-front-month", True,
        )
        provider._loop = ImmediateLoop()  # type: ignore[assignment]
        cache = ExecutionQuoteCache()
        provider.set_execution_quote_callback(cache.update)
        provider._on_tick = lambda _event: None
        with patch.dict("sys.modules", {"shioaji": module}):
            provider._login_and_subscribe()
        self.assertEqual(api.quote.subscriptions, ["tick:v1", "bidask:v1"])
        api.quote.bidask_callback(None, SimpleNamespace(
            code="TMF202610", datetime=datetime(2026, 9, 14, 9, 1, tzinfo=TAIPEI),
            bid_price=[20_000], ask_price=[20_002],
        ))
        quote = cache.get("TMF", "TMF202610")
        self.assertEqual((quote.best_bid, quote.best_ask), (20_000, 20_002))
        self.assertIsNone(quote.last_price)


if __name__ == "__main__":
    unittest.main()


from public_fixtures import InertProvider, PublicTestCase, PublicAsyncTestCase, synthetic_csv, services
import asyncio
from pathlib import Path
import unittest
from unittest.mock import patch

from tw_quant.broker import DisabledBroker
from tw_quant.live.feed import ReplayFeed as LegacyReplayFeed
from tw_quant.live.settings import LiveSettings
from tw_quant.market import TickEvent
from tw_quant.market_data import MarketDataSettings, build_market_data_provider
from tw_quant.market_data.providers import ReplayMarketDataProvider


ROOT = Path(__file__).resolve().parents[1]


class MarketDataSettingsTests(PublicTestCase):
    def test_new_provider_environment_variable_has_priority(self):
        with patch.dict(
            "os.environ",
            {"MARKET_DATA_PROVIDER": "replay", "MARKET_MODE": "shioaji"},
            clear=True,
        ):
            settings = MarketDataSettings.from_env()
        self.assertEqual(settings.provider, "replay")

    def test_legacy_mock_mode_maps_to_replay_provider(self):
        with patch.dict("os.environ", {
            "PLATFORM_ENVIRONMENT": "development", "MARKET_MODE": "mock",
        }, clear=True):
            settings = LiveSettings.from_env()
        self.assertEqual(settings.market_data.provider, "replay")
        self.assertEqual(settings.mode, "mock")

    def test_factory_builds_adapter_without_api_dependency(self):
        settings = MarketDataSettings(
            provider="replay",
            replay_csv=str(synthetic_csv()),
            replay_speed=1000,
        )
        provider = build_market_data_provider(settings)
        self.assertIsInstance(provider, ReplayMarketDataProvider)
        self.assertEqual(provider.provider_name, "replay")
        self.assertTrue(provider.capabilities.live_ticks)
        self.assertFalse(provider.capabilities.historical_bars)

    def test_legacy_feed_import_is_a_compatibility_alias(self):
        self.assertIs(LegacyReplayFeed, ReplayMarketDataProvider)


class ProviderContractTests(PublicAsyncTestCase):
    async def test_replay_normalizes_rows_to_canonical_tick_events(self):
        provider = ReplayMarketDataProvider(
            synthetic_csv(), speed=1000, loop=False
        )
        ticks: list[TickEvent] = []
        statuses: list[str] = []
        await provider.start(ticks.append, statuses.append)
        for _ in range(100):
            if ticks:
                break
            await asyncio.sleep(0.01)
        await provider.stop()
        self.assertTrue(ticks)
        self.assertIsInstance(ticks[0], TickEvent)
        self.assertEqual(ticks[0].symbol, "TMF")
        self.assertIsNotNone(ticks[0].exchange_time.tzinfo)
        self.assertEqual(statuses[0], "connected")


class BrokerBoundaryTests(PublicAsyncTestCase):
    async def test_quote_only_default_rejects_every_order(self):
        broker = DisabledBroker()
        self.assertFalse((await broker.account_state())["trading_enabled"])
        self.assertEqual(await broker.positions(), [])
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            await broker.submit_order({"symbol": "TMF", "side": "buy"})


class DependencyDirectionTests(PublicTestCase):
    def test_strategy_and_backtest_do_not_depend_on_shioaji(self):
        roots = (ROOT / "tw_quant/backtest",)
        for root in roots:
            for path in root.rglob("*.py"):
                self.assertNotIn("shioaji", path.read_text(encoding="utf-8").lower())

    def test_fastapi_composition_does_not_import_provider_adapters(self):
        source = (ROOT / "tw_quant/live/api.py").read_text(encoding="utf-8")
        self.assertNotIn("ShioajiMarketDataProvider", source)
        self.assertNotIn("ReplayMarketDataProvider", source)


if __name__ == "__main__":
    unittest.main()

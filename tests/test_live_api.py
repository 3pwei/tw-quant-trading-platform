
from public_fixtures import InertProvider, PublicTestCase, PublicAsyncTestCase, synthetic_csv, services
import asyncio
from pathlib import Path
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from tw_quant.market import KBar, TickEvent
from tw_quant.live.api import create_app
from tw_quant.live.access import AccessIdentity, AccessTokenError
from tw_quant.live.feed import ReplayFeed, ShioajiFeed
from tw_quant.live.service import LiveMarketService
from tw_quant.live.settings import LiveSettings
from tw_quant.live.storage import SQLiteBarRepository
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]


class LiveApiTests(PublicTestCase):
    def make_client(self):
        temp = tempfile.TemporaryDirectory()
        repository = SQLiteBarRepository(Path(temp.name) / "bars.sqlite3")
        settings = LiveSettings(
            mode="mock", db_path=str(Path(temp.name) / "bars.sqlite3"),
            replay_csv=str(synthetic_csv()), replay_speed=1000,
            heartbeat_seconds=0.05,
        )
        feed = ReplayFeed(settings.replay_csv, speed=1000, loop=False)
        app = create_app(settings, feed=feed, repository=repository, strategy_provider=InertProvider())
        return temp, TestClient(app)



    def test_websocket_message_schema_and_replay_updates_forming_bar(self):
        temp, client = self.make_client()
        required = {
            "type", "interval", "symbol", "contract", "exchange_time", "received_time",
            "latency_ms", "time", "open", "high", "low", "close", "volume",
            "status", "connection_status",
        }
        try:
            with client:
                with client.websocket_connect("/ws/market/TMF") as socket:
                    forming_counts = {}
                    deadline = time.time() + 3
                    while time.time() < deadline and not any(
                        count >= 2 for count in forming_counts.values()
                    ):
                        message = socket.receive_json()
                        if message.get("type") == "kbar":
                            self.assertTrue(required.issubset(message))
                            self.assertEqual(message["interval"], "1m")
                            if message["status"] == "forming":
                                bar_time = message["time"]
                                forming_counts[bar_time] = forming_counts.get(bar_time, 0) + 1
                    self.assertTrue(
                        any(count >= 2 for count in forming_counts.values()),
                        "expected repeated forming updates for the same minute",
                    )
        finally:
            temp.cleanup()

    def test_websocket_streams_selected_five_minute_bar(self):
        temp, client = self.make_client()
        try:
            with client:
                with client.websocket_connect(
                    "/ws/market/TMF?interval=5m"
                ) as socket:
                    while True:
                        message = socket.receive_json()
                        if message.get("type") == "kbar":
                            self.assertEqual(message["interval"], "5m")
                            self.assertEqual(message["time"][14:16], "00")
                            self.assertEqual(message["status"], "forming")
                            break
        finally:
            temp.cleanup()

    def test_cloudflare_origin_auth_forwards_verified_identity(self):
        class FakeAccessValidator:
            def authenticate(self, token):
                if token != "signed-assertion":
                    raise AccessTokenError("invalid assertion")
                return AccessIdentity(subject="user-123", email="owner@example.com")

        temp = tempfile.TemporaryDirectory()
        repository = SQLiteBarRepository(Path(temp.name) / "auth.sqlite3")
        settings = LiveSettings(
            mode="mock", db_path=str(Path(temp.name) / "auth.sqlite3"),
            replay_csv=str(synthetic_csv()), replay_speed=1000,
            heartbeat_seconds=0.05,
        )
        feed = ReplayFeed(settings.replay_csv, speed=1000, loop=False)
        app = create_app(
            settings, feed=feed, repository=repository,
            access_validator=FakeAccessValidator(),
         strategy_provider=InertProvider())
        try:
            with TestClient(app) as client:
                denied = client.get("/internal/auth/cloudflare")
                self.assertEqual(denied.status_code, 401)
                accepted = client.get(
                    "/internal/auth/cloudflare",
                    headers={"Cf-Access-Jwt-Assertion": "signed-assertion"},
                )
                self.assertEqual(accepted.status_code, 204)
                self.assertEqual(
                    accepted.headers["x-authenticated-email"], "owner@example.com"
                )
                self.assertEqual(
                    accepted.headers["x-authenticated-subject"], "user-123"
                )
        finally:
            temp.cleanup()


class ReplayConnectionTests(PublicAsyncTestCase):
    async def test_worker_survives_listener_failure_and_reports_metrics(self):
        class ManualFeed:
            contract = "TMFI6"
            healthy = True

            async def start(self, on_tick, on_status):
                self.on_tick = on_tick
                on_status("connected")

            async def stop(self):
                self.healthy = False

            async def heartbeat(self):
                return self.healthy

        temp = tempfile.TemporaryDirectory()
        repository = SQLiteBarRepository(Path(temp.name) / "worker.sqlite3")
        feed = ManualFeed()
        service = LiveMarketService(feed, repository, heartbeat_seconds=1)
        service.add_bar_listener(
            lambda _bar: (_ for _ in ()).throw(RuntimeError("listener failed"))
        )
        at = datetime.now(ZoneInfo("Asia/Taipei"))
        ticks = [
            TickEvent(
                symbol="TMF", contract="TMFI6",
                exchange_time=at + timedelta(seconds=index),
                received_time=at + timedelta(seconds=index, milliseconds=5),
                price=100 + index, volume=1, sequence=f"failure-{index}",
            )
            for index in (1, 2)
        ]
        try:
            await service.start()
            for tick in ticks:
                feed.on_tick(tick)
            await asyncio.wait_for(service.queue.join(), timeout=1)
            status = service.status_message()
            self.assertEqual(status["worker_cycles"], 2)
            self.assertEqual(status["worker_errors"], 2)
            self.assertEqual(status["service_status"], "degraded")
            self.assertGreaterEqual(status["queue_high_watermark"], 1)
            self.assertIsNotNone(status["average_tick_processing_ms"])
            self.assertGreaterEqual(status["database_write_count"], 2)
            self.assertIn("average_database_write_ms", status)
            self.assertEqual(status["websocket_connections"], 0)
            self.assertFalse(service._worker.done())
        finally:
            await service.stop()
            repository.close()
            temp.cleanup()

    async def test_backfill_cleanup_preserves_tick_aggregated_bars(self):
        temp = tempfile.TemporaryDirectory()
        repository = SQLiteBarRepository(Path(temp.name) / "cleanup.sqlite3")
        tz = ZoneInfo("Asia/Taipei")
        base = datetime(2026, 8, 24, 15, 0, tzinfo=tz)
        historical = KBar(
            symbol="TMF", contract="TMFU6", time=base,
            open=100, high=103, low=99, close=102, volume=10,
            status="closed", session="night", trading_date=(base + timedelta(days=1)).date(),
            first_tick_time=base,
            last_tick_time=base.replace(second=59, microsecond=999000),
            exchange_time=base.replace(second=59, microsecond=999000),
            received_time=base + timedelta(days=1), latency_ms=0,
        )
        live_time = base + timedelta(minutes=1)
        live = historical.copy(
            time=live_time, first_tick_time=live_time + timedelta(seconds=2),
            last_tick_time=live_time + timedelta(seconds=48),
            exchange_time=live_time + timedelta(seconds=48), latency_ms=12,
        )
        try:
            repository.save(historical)
            repository.save(live)
            repository.purge_backfill("TMF", "TMFU6")
            bars = repository.latest("TMF", 10)
            self.assertEqual([bar.time for bar in bars], [live_time])
        finally:
            repository.close()
            temp.cleanup()

    async def test_shioaji_history_is_normalized_to_closed_left_edge_bars(self):
        tz = ZoneInfo("Asia/Taipei")
        timestamps = [
            int(datetime(2026, 8, 24, 15, 1, tzinfo=timezone.utc).timestamp() * 1e9),
            int(datetime(2026, 8, 24, 15, 2, tzinfo=timezone.utc).timestamp() * 1e9),
        ]

        class Kbars:
            def dict(self):
                return {
                    "ts": timestamps,
                    "Open": [100, 103], "High": [105, 106],
                    "Low": [99, 102], "Close": [103, 104],
                    "Volume": [12, 8],
                }

        class Api:
            def kbars(self, **kwargs):
                self.kwargs = kwargs
                return Kbars()

        feed = ShioajiFeed("key", "secret", "TMFR1", True, history_days=7)
        feed.api = Api()
        feed._resolved_contract = object()
        feed.contract = "TMFU6"
        bars = await feed.load_history(limit=1)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].time, datetime(2026, 8, 24, 15, 1, tzinfo=tz))
        self.assertEqual(bars[0].contract, "TMFU6")
        self.assertEqual(bars[0].status, "closed")
        self.assertEqual(bars[0].volume, 8)
        self.assertEqual(feed.api.kwargs["contract"], feed._resolved_contract)

    def test_shioaji_numeric_timestamp_uses_taipei_clock_fields(self):
        # Official Shioaji docs show this exact value as 2026-05-18 09:01.
        actual = ShioajiFeed._historical_time(1779094860000000000)
        self.assertEqual(
            actual,
            datetime(2026, 5, 18, 9, 1, tzinfo=ZoneInfo("Asia/Taipei")),
        )

    async def test_replay_heartbeat_and_stop(self):
        feed = ReplayFeed(synthetic_csv(), speed=1000, loop=False)
        statuses = []
        ticks = []
        await feed.start(ticks.append, statuses.append)
        await asyncio.sleep(0.02)
        self.assertEqual(statuses[0], "connected")
        self.assertTrue(await feed.heartbeat() or len(ticks) == 12)
        await feed.stop()
        self.assertFalse(await feed.heartbeat())

    async def test_looping_replay_keeps_ticks_unique_and_time_monotonic(self):
        feed = ReplayFeed(synthetic_csv(), speed=1000, loop=True)
        ticks = []
        try:
            await feed.start(ticks.append, lambda _status: None)
            deadline = time.time() + 2
            while len(ticks) < 24 and time.time() < deadline:
                await asyncio.sleep(0.02)
            self.assertGreaterEqual(len(ticks), 24)
            sample = ticks[:24]
            self.assertEqual(len({item.sequence for item in sample}), len(sample))
            self.assertEqual(
                [item.exchange_time for item in sample],
                sorted(item.exchange_time for item in sample),
            )
            self.assertLess(
                (sample[-1].exchange_time - sample[0].exchange_time).total_seconds(),
                3,
            )
        finally:
            await feed.stop()

    async def test_connection_events_recover_without_using_tick_timeout(self):
        class EventFeed:
            contract = "TMFI6"
            healthy = True

            async def start(self, _on_tick, on_status):
                self.on_status = on_status
                on_status("connected")

            async def stop(self):
                self.healthy = False

            async def heartbeat(self):
                return self.healthy

        temp = tempfile.TemporaryDirectory()
        repository = SQLiteBarRepository(Path(temp.name) / "events.sqlite3")
        feed = EventFeed()
        service = LiveMarketService(feed, repository, heartbeat_seconds=0.01)
        try:
            await service.start()
            feed.on_status("reconnecting")
            self.assertEqual(service.connection_status, "reconnecting")
            feed.on_status("connected")
            self.assertEqual(service.connection_status, "connected")
            # No tick was sent; connection remains healthy because heartbeat,
            # not tick recency, is authoritative.
            await asyncio.sleep(0.02)
            self.assertEqual(service.connection_status, "connected")
        finally:
            await service.stop()
            repository.close()
            temp.cleanup()

    async def test_restart_keeps_persistent_tick_deduplication(self):
        class ManualFeed:
            contract = "TMFI6"
            healthy = True
            async def start(self, on_tick, on_status):
                self.on_tick = on_tick
                on_status("connected")
            async def stop(self): self.healthy = False
            async def heartbeat(self): return self.healthy

        temp = tempfile.TemporaryDirectory()
        path = Path(temp.name) / "restart.sqlite3"
        tz = ZoneInfo("Asia/Taipei")
        when = datetime(2026, 8, 24, 15, 0, 1, tzinfo=tz)
        item = TickEvent(
            symbol="TMF", contract="TMFI6", exchange_time=when,
            received_time=when + timedelta(milliseconds=10), price=100,
            volume=2, sequence="persistent-1",
        )
        try:
            first_repo = SQLiteBarRepository(path)
            first_feed = ManualFeed()
            first = LiveMarketService(first_feed, first_repo, heartbeat_seconds=1)
            await first.start(); first_feed.on_tick(item); await first.queue.join(); await first.stop(); first_repo.close()

            second_repo = SQLiteBarRepository(path)
            second_feed = ManualFeed()
            second = LiveMarketService(second_feed, second_repo, heartbeat_seconds=1)
            await second.start(); second_feed.on_tick(item); await second.queue.join()
            bars = second_repo.latest("TMF", 10)
            self.assertEqual(bars[-1].volume, 2)
            self.assertEqual(second.aggregator.duplicate_ticks, 1)
            await second.stop(); second_repo.close()
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()

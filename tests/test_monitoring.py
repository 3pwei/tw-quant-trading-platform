from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta

from tw_quant.live.api import system_status
from tw_quant.live.hub import BroadcastHub
from tw_quant.live.monitoring import HostResourceMonitor
from tw_quant.live.service import LiveMarketService
from tw_quant.live.storage import SQLiteBarRepository
from tw_quant.market import TAIPEI


class HostResourceMonitorTests(unittest.TestCase):
    def test_resource_snapshot_reports_cpu_delta_memory_and_disk(self):
        cpu_samples = iter(
            (
                "cpu 100 0 50 850 0 0 0 0\n",
                "cpu 150 0 50 900 0 0 0 0\n",
            )
        )

        def read_text(path: str) -> str:
            if path == "/proc/stat":
                return next(cpu_samples)
            if path == "/proc/meminfo":
                return "MemTotal: 1000 kB\nMemAvailable: 250 kB\n"
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as temp:
            monitor = HostResourceMonitor(Path(temp), read_text=read_text)
            first = monitor.snapshot()
            second = monitor.snapshot()

        self.assertIsNone(first["cpu_percent"])
        self.assertEqual(second["cpu_percent"], 50.0)
        self.assertEqual(second["memory_percent"], 75.0)
        self.assertEqual(second["memory_total_bytes"], 1_024_000)
        self.assertGreater(second["disk_total_bytes"], 0)
        self.assertGreaterEqual(second["disk_percent"], 0)


class SystemStatusTests(unittest.TestCase):
    @staticmethod
    def paper(**changes):
        return {
            "status": "healthy",
            "active_kill_switches": 0,
            "inconsistent_owners": 0,
            **changes,
        }

    def test_status_precedence(self):
        self.assertEqual(
            system_status(
                {"service_status": "provider_disconnected"},
                self.paper(active_kill_switches=1),
            ),
            "provider_disconnected",
        )
        self.assertEqual(
            system_status(
                {"service_status": "market_stale"}, self.paper()
            ),
            "market_stale",
        )
        self.assertEqual(
            system_status(
                {"service_status": "healthy"},
                self.paper(active_kill_switches=1),
            ),
            "trading_halted",
        )
        self.assertEqual(
            system_status(
                {"service_status": "degraded"}, self.paper()
            ),
            "degraded",
        )
        self.assertEqual(
            system_status(
                {"service_status": "healthy"}, self.paper()
            ),
            "healthy",
        )
        self.assertEqual(
            system_status(
                {"service_status": "healthy"}, self.paper(),
                {"cpu_percent": 95.0},
            ),
            "degraded",
        )


class MarketStatusTests(unittest.TestCase):
    class Feed:
        contract = "TMFTEST"
        provider_name = "test"

    def test_stale_and_disconnected_states_block_new_exposure(self):
        with tempfile.TemporaryDirectory() as temp:
            repository = SQLiteBarRepository(Path(temp) / "market.sqlite3")
            try:
                service = LiveMarketService(
                    self.Feed(), repository, stale_after_seconds=10
                )
                service.connection_status = "connected"
                service.last_tick_time = datetime.now(TAIPEI) - timedelta(seconds=20)
                service.last_received_time = service.last_tick_time
                stale = service.status_message()
                self.assertEqual(stale["service_status"], "market_stale")
                self.assertEqual(stale["trading_block_reason"], "market_stale")

                service.connection_status = "disconnected"
                disconnected = service.status_message()
                self.assertEqual(
                    disconnected["service_status"], "provider_disconnected"
                )
                self.assertEqual(
                    disconnected["trading_block_reason"],
                    "provider_disconnected",
                )

                service.connection_status = "reconnecting"
                reconnecting = service.status_message()
                self.assertEqual(
                    reconnecting["service_status"], "provider_disconnected"
                )
                self.assertEqual(
                    reconnecting["trading_block_reason"],
                    "provider_disconnected",
                )
            finally:
                repository.close()

    def test_websocket_counts_are_symmetric(self):
        hub = BroadcastHub(queue_size=1)
        client = hub.subscribe()
        self.assertEqual(hub.stats()["active_connections"], 1)
        hub.publish({"type": "first"})
        hub.publish({"type": "second"})
        self.assertEqual(hub.stats()["dropped_messages"], 1)
        hub.unsubscribe(client)
        self.assertEqual(hub.stats()["active_connections"], 0)
        self.assertEqual(hub.stats()["total_disconnections"], 1)


if __name__ == "__main__":
    unittest.main()

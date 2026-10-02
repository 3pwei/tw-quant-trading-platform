from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tw_quant.auth import Role, SQLiteAuthRepository, TradingMode
from tw_quant.market import KBar, TAIPEI
from tw_quant.paper import (
    PaperOrderCommand,
    PaperTradingService,
    SQLitePaperRepository,
)


class PaperReadModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "paper.sqlite3"
        auth = SQLiteAuthRepository(self.db_path)
        self.user = auth.create_user(
            "read-model@example.com",
            role=Role.TRADER,
            trading_mode=TradingMode.PAPER,
        )
        auth.close()
        at = datetime.now(TAIPEI) - timedelta(seconds=10)
        self.bar = KBar(
            symbol="TMF",
            contract="TMFTEST",
            time=at.replace(second=0, microsecond=0),
            open=20_000,
            high=20_010,
            low=19_990,
            close=20_000,
            volume=100,
            status="closed",
            session="day",
            trading_date=at.date(),
            first_tick_time=at,
            last_tick_time=at,
            exchange_time=at,
            received_time=at,
            latency_ms=0,
        )

    def tearDown(self):
        self.temp.cleanup()

    def service(self) -> PaperTradingService:
        return PaperTradingService(SQLitePaperRepository(self.db_path))

    def submit(self, service: PaperTradingService, key: str = "read-model"):
        return service.submit(
            self.user,
            PaperOrderCommand("manual", 1, "buy", 1, 19_950),
            idempotency_key=key,
            market_bar=self.bar,
        )[0]

    def test_account_queries_do_not_scan_the_event_log(self):
        service = self.service()
        try:
            order = self.submit(service)
            with patch.object(
                service.repository,
                "events",
                side_effect=AssertionError("event log scan"),
            ):
                self.assertEqual(service.orders(self.user.user_id), [order])
                self.assertEqual(len(service.fills(self.user.user_id)), 1)
                self.assertEqual(
                    service.positions(self.user.user_id)[0]["quantity"], 1
                )
                self.assertEqual(
                    service.order_for_client_id(
                        self.user.user_id, "read-model"
                    )["order_id"],
                    order["order_id"],
                )
                self.assertIsNotNone(
                    service.fill(self.user.user_id, str(order["fill_id"]))
                )
        finally:
            service.close()

    def test_read_models_are_rebuilt_from_legacy_events(self):
        first = self.service()
        order = self.submit(first)
        expected_positions = first.positions(self.user.user_id)
        expected_fills = first.fills(self.user.user_id)
        first.close()

        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            DROP TABLE paper_order_read_model;
            DROP TABLE paper_fill_read_model;
            DROP TABLE paper_position_read_model;
            DROP TABLE paper_read_model_state;
            """
        )
        connection.commit()
        connection.close()

        restored = self.service()
        try:
            self.assertEqual(
                restored.repository.order_snapshot(
                    self.user.user_id, str(order["order_id"])
                ),
                order,
            )
            self.assertEqual(restored.positions(self.user.user_id), expected_positions)
            self.assertEqual(restored.fills(self.user.user_id), expected_fills)
            stats = restored.repository.stats()
            self.assertEqual(stats["read_model_orders"], 1)
            self.assertEqual(stats["read_model_fills"], 1)
            self.assertEqual(stats["read_model_open_positions"], 1)
        finally:
            restored.close()

    def test_fill_limit_is_applied_by_the_repository(self):
        service = self.service()
        try:
            self.submit(service, "first")
            service.submit(
                self.user,
                PaperOrderCommand(
                    "manual", 1, "sell", 1, None, reduce_only=True
                ),
                idempotency_key="second",
                market_bar=self.bar,
            )
            self.assertEqual(len(service.fills(self.user.user_id, 1)), 1)
            self.assertEqual(len(service.fills(self.user.user_id, 2)), 2)
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()

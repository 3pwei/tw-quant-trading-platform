from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from tw_quant.auth import Role, SQLiteAuthRepository, TradingMode
from tw_quant.market import KBar, TAIPEI
from tw_quant.paper import (
    PaperOrderCommand,
    PaperTradingService,
    SQLitePaperRepository,
)


class PaperRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "paper.sqlite3"
        auth = SQLiteAuthRepository(self.db_path)
        self.user = auth.create_user(
            "trader@example.com",
            role=Role.TRADER,
            trading_mode=TradingMode.PAPER,
        )
        auth.close()
        at = datetime.now(TAIPEI) - timedelta(seconds=10)
        self.bar = KBar(
            symbol="TMF", contract="TMFTEST", time=at.replace(second=0, microsecond=0),
            open=20_000, high=20_010, low=19_990, close=20_000, volume=100,
            status="closed", session="day", trading_date=at.date(),
            first_tick_time=at, last_tick_time=at, exchange_time=at,
            received_time=at, latency_ms=0,
        )

    def tearDown(self):
        self.temp.cleanup()

    def service(self) -> PaperTradingService:
        return PaperTradingService(SQLitePaperRepository(self.db_path))

    def test_restart_restores_position_risk_state_control_and_idempotency(self):
        first = self.service()
        order, created = first.submit(
            self.user,
            PaperOrderCommand("manual", 1, "buy", 1, 19_950),
            idempotency_key="restart-order",
            market_bar=self.bar,
        )
        self.assertTrue(created)
        first.activate_kill_switch(self.user.user_id, "operator_restart_test")
        first.close()

        restored = self.service()
        try:
            self.assertTrue(restored.recovery.healthy)
            self.assertEqual(restored.recovery.restored_orders, 1)
            self.assertEqual(restored.recovery.restored_fills, 1)
            self.assertEqual(restored.positions(self.user.user_id)[0]["quantity"], 1)
            account = restored.account(self.user.user_id)
            self.assertEqual(account["open_contracts"], 1)
            self.assertEqual(account["trades"], 1)
            self.assertTrue(account["kill_switch_active"])
            self.assertEqual(account["recovery_status"], "healthy")

            repeated, created = restored.submit(
                self.user,
                PaperOrderCommand("manual", 1, "buy", 1, 19_950),
                idempotency_key="restart-order",
                market_bar=self.bar,
            )
            self.assertFalse(created)
            self.assertEqual(repeated["order_id"], order["order_id"])
        finally:
            restored.close()

    def test_inconsistent_persistence_fails_closed_after_restart(self):
        repository = SQLitePaperRepository(self.db_path)
        repository.reserve_key(self.user.user_id, "interrupted", "missing-order")
        repository.close()

        restored = self.service()
        try:
            account = restored.account(self.user.user_id)
            self.assertEqual(account["recovery_status"], "degraded")
            self.assertIn("dangling_idempotency_key", account["recovery_issues"])
            self.assertTrue(account["kill_switch_active"])
            self.assertEqual(account["kill_switch_reason"], "recovery_inconsistent")
            health = restored.health()
            self.assertEqual(health["status"], "degraded")
            self.assertEqual(health["inconsistent_owners"], 1)
        finally:
            restored.close()

    def test_restart_restores_realized_pnl_and_flat_position(self):
        first = self.service()
        first.submit(
            self.user,
            PaperOrderCommand("closed", 1, "buy", 1, 19_950),
            idempotency_key="entry",
            market_bar=self.bar,
        )
        lower_bar = self.bar.copy(
            close=19_900, open=19_900, high=19_910, low=19_890
        )
        first.submit(
            self.user,
            PaperOrderCommand(
                "closed", 1, "sell", 1, None, reduce_only=True
            ),
            idempotency_key="exit",
            market_bar=lower_bar,
        )
        before = first.account(self.user.user_id)
        self.assertLess(before["realized_pnl"], 0)
        first.close()

        restored = self.service()
        try:
            after = restored.account(self.user.user_id)
            self.assertEqual(after["realized_pnl"], before["realized_pnl"])
            self.assertEqual(after["trades"], 1)
            self.assertEqual(after["consecutive_losses"], 1)
            self.assertEqual(after["open_contracts"], 0)
            self.assertEqual(restored.positions(self.user.user_id), [])
        finally:
            restored.close()

    def test_paper_submission_performance_budget_and_metrics(self):
        service = self.service()
        try:
            for index in range(200):
                service.submit(
                    self.user,
                    PaperOrderCommand("load", 1, "buy", 1, 19_950),
                    idempotency_key=f"load-{index}",
                    market_bar=self.bar,
                )
            health = service.health()
            self.assertEqual(health["submission_requests"], 200)
            self.assertEqual(health["filled_submissions"], 2)
            self.assertEqual(health["rejected_submissions"], 198)
            # A generous CI regression guard, not a trading latency promise.
            self.assertLess(health["average_submission_ms"], 100)
            self.assertLess(health["max_submission_ms"], 1_000)
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()

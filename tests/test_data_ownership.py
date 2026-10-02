from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from tw_quant.auth import AccessIdentity, AccessTokenError, Role, SQLiteAuthRepository
from tw_quant.live.api import create_app
from tw_quant.live.feed import ReplayFeed
from tw_quant.live.settings import LiveSettings
from tw_quant.live.storage import DEFAULT_OWNER_ID, SQLiteBarRepository
from tw_quant.market import KBar
from public_fixtures import generic_definition as default_composite_definition

ROOT = Path(__file__).resolve().parents[1]
TAIPEI = ZoneInfo("Asia/Taipei")


def market_bar() -> KBar:
    timestamp = datetime(2026, 8, 24, 15, 0, tzinfo=TAIPEI)
    return KBar(
        symbol="TMF",
        contract="TMFU6",
        time=timestamp,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=10,
        status="closed",
        session="night",
        trading_date=date(2026, 8, 25),
        first_tick_time=timestamp,
        last_tick_time=timestamp + timedelta(seconds=50),
        exchange_time=timestamp + timedelta(seconds=50),
        received_time=timestamp + timedelta(seconds=50, milliseconds=5),
        latency_ms=5,
    )


def backtest_result(strategy_id: str) -> dict[str, object]:
    return {
        "metadata": {
            "symbol": "TMF",
            "strategy": "owner test",
            "strategy_key": strategy_id,
            "strategy_version": 1,
            "interval": "多週期",
            "interval_key": "multi",
            "date_range": "2026-08-25 ～ 2026-08-25",
        },
        "config": {},
        "summary": {"net_profit": 0},
        "trades": [],
        "equity": [],
        "bars": [],
    }


class OwnershipRepositoryTests(unittest.TestCase):
    def test_strategies_parameters_and_backtests_are_owner_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SQLiteBarRepository(Path(directory) / "ownership.sqlite3")
            try:
                owner_a = "user-a"
                owner_b = "user-b"
                definition = default_composite_definition()
                repo.save_strategy_parameters(
                    "scripted", {"window": 3}, owner_a
                )
                repo.save_strategy_parameters(
                    "scripted", {"window": 20}, owner_b
                )
                repo.save_composite_strategy("combo-a", definition, owner_a)
                repo.save_composite_strategy("combo-b", definition, owner_b)
                saved = repo.save_backtest_run(
                    backtest_result("combo-a"),
                    "composite",
                    "combo-a",
                    1,
                    definition,
                    owner_a,
                )

                self.assertEqual(
                    repo.strategy_parameters(owner_a)["scripted"][
                        "window"
                    ],
                    3,
                )
                self.assertEqual(
                    repo.strategy_parameters(owner_b)["scripted"][
                        "window"
                    ],
                    20,
                )
                self.assertEqual(
                    [item["id"] for item in repo.composite_strategies(owner_a)],
                    ["combo-a"],
                )
                self.assertIsNone(
                    repo.composite_strategy("combo-a", owner_user_id=owner_b)
                )
                self.assertEqual(repo.backtest_runs(100, 0, owner_user_id=owner_b), [])
                self.assertIsNone(repo.backtest_run(saved["run_id"], owner_b))
                self.assertIsNone(repo.delete_backtest_run(saved["run_id"], owner_b))
                self.assertIsNotNone(repo.backtest_run(saved["run_id"], owner_a))
            finally:
                repo.close()

    def test_legacy_data_is_claimed_once_by_bootstrap_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-owner.sqlite3"
            definition = default_composite_definition()
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE strategy_parameters (
                    strategy TEXT PRIMARY KEY,
                    parameters_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE composite_strategies (
                    strategy_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    definition_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(strategy_id, version)
                );
                CREATE TABLE archived_composite_strategies (
                    strategy_id TEXT PRIMARY KEY,
                    archived_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO strategy_parameters VALUES (?,?,?)",
                ("scripted", '{"legacy": 1}', "2026-09-01T00:00:00+08:00"),
            )
            connection.execute(
                "INSERT INTO composite_strategies VALUES (?,?,?,?,?)",
                (
                    "legacy-combo", 1, definition["name"],
                    json.dumps(definition, ensure_ascii=False),
                    "2026-09-01T00:00:00+08:00",
                ),
            )
            connection.commit()
            connection.close()
            repo = SQLiteBarRepository(path)
            try:
                saved = repo.save_backtest_run(
                    backtest_result("legacy-combo"),
                    "composite",
                    "legacy-combo",
                    1,
                    definition,
                )

                repo.claim_legacy_ownership("bootstrap-admin")

                self.assertEqual(repo.strategy_parameters(DEFAULT_OWNER_ID), {})
                self.assertEqual(
                    repo.composite_strategies(DEFAULT_OWNER_ID), []
                )
                self.assertIn(
                    "scripted", repo.strategy_parameters("bootstrap-admin")
                )
                self.assertIsNotNone(
                    repo.composite_strategy(
                        "legacy-combo", owner_user_id="bootstrap-admin"
                    )
                )
                self.assertIsNotNone(
                    repo.backtest_run(saved["run_id"], "bootstrap-admin")
                )
                repo.claim_legacy_ownership("different-admin")
                self.assertIsNotNone(
                    repo.backtest_run(saved["run_id"], "bootstrap-admin")
                )
            finally:
                repo.close()


class OwnershipApiTests(unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()

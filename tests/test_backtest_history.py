
from public_fixtures import InertProvider, PublicTestCase, PublicAsyncTestCase, synthetic_csv, services
from datetime import date, datetime, timedelta
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from tw_quant.live.api import create_app
from tw_quant.live.feed import ReplayFeed
from tw_quant.live.settings import LiveSettings
from tw_quant.live.storage import SQLiteBarRepository
from tw_quant.market import KBar


ROOT = Path(__file__).resolve().parents[1]
TAIPEI = ZoneInfo("Asia/Taipei")


def bar(minute: int, close: float) -> KBar:
    timestamp = datetime(2026, 8, 24, 15, 0, tzinfo=TAIPEI) + timedelta(
        minutes=minute
    )
    return KBar(
        symbol="TMF", contract="TMFU6", time=timestamp, open=close,
        high=close + 1, low=close - 1, close=close, volume=100,
        status="closed", session="night", trading_date=date(2026, 8, 25),
        first_tick_time=timestamp,
        last_tick_time=timestamp + timedelta(seconds=50),
        exchange_time=timestamp + timedelta(seconds=50),
        received_time=timestamp + timedelta(seconds=50, milliseconds=10),
        latency_ms=10,
    )


class BacktestHistoryTests(PublicTestCase):
    @staticmethod
    def saved_chart_result(
        trades: list[dict[str, object]], interval: str = "1m"
    ) -> dict[str, object]:
        return {
            "metadata": {
                "symbol": "TMF",
                "strategy": "Chart test",
                "interval": interval,
                "interval_key": interval,
                "date_range": "2026-08-25 ～ 2026-08-26",
            },
            "config": {},
            "summary": {"net_profit": 0},
            "trades": trades,
            "equity": [],
            "overlays": [],
        }

    def test_atomic_backtest_is_saved_and_queryable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.sqlite3"
            repo = SQLiteBarRepository(path)
            for minute, close in enumerate([100, 100, 102, 103, 101, 99]):
                repo.save(bar(minute, close))
            settings = LiveSettings(
                mode="mock", db_path=str(path),
                replay_csv=str(synthetic_csv()),
                replay_speed=1000, heartbeat_seconds=0.05,
            )
            app = create_app(
                settings,
                feed=ReplayFeed(settings.replay_csv, speed=1000, loop=False),
                repository=repo,
             strategy_provider=InertProvider())
            with TestClient(app) as client:
                response = client.post(
                    "/api/backtest-runs",
                    json={
                        "strategy": "scripted", "interval": "1m",
                        "start": "2026-08-25", "end": "2026-08-25",
                    },
                )
                self.assertEqual(response.status_code, 201, response.text)
                run_id = response.json()["history_run_id"]
                listing = client.get("/api/backtest-runs").json()["runs"]
                self.assertEqual(listing[0]["run_id"], run_id)
                self.assertEqual(listing[0]["strategy_kind"], "atomic")
                self.assertIsNone(listing[0]["strategy_version"])
                detail = client.get(f"/api/backtest-runs/{run_id}").json()
                self.assertEqual(detail["strategy_snapshot"]["window"], 15)
                self.assertNotIn("bars", detail["result"])
                self.assertNotIn("visualization", detail["result"])

                second = client.post(
                    "/api/backtest-runs",
                    json={
                        "strategy": "scripted", "interval": "1m",
                        "start": "2026-08-25", "end": "2026-08-25",
                    },
                )
                self.assertEqual(second.status_code, 201, second.text)
                first_page = client.get("/api/backtest-runs?limit=1").json()
                second_page = client.get(
                    "/api/backtest-runs?limit=1&offset=1"
                ).json()
                self.assertTrue(first_page["has_more"])
                self.assertFalse(second_page["has_more"])
                self.assertNotEqual(
                    first_page["runs"][0]["run_id"],
                    second_page["runs"][0]["run_id"],
                )

                # List queries use denormalized summaries and never need to
                # load or parse the potentially large result payload.
                with repo.lock:
                    repo.connection.execute(
                        "UPDATE backtest_runs SET result_json='not-list-json' "
                        "WHERE run_id=?",
                        (run_id,),
                    )
                    repo.connection.commit()
                lightweight = client.get("/api/backtest-runs").json()["runs"]
                lightweight_run = next(
                    item for item in lightweight if item["run_id"] == run_id
                )
                self.assertEqual(lightweight_run["summary"], listing[0]["summary"])
                self.assertEqual(
                    lightweight_run["trade_count"], listing[0]["trade_count"]
                )

                deleted = client.delete(f"/api/backtest-runs/{run_id}")
                self.assertEqual(deleted.status_code, 200, deleted.text)
                self.assertEqual(deleted.json()["deleted_run_id"], run_id)
                self.assertFalse(deleted.json()["released_strategy_reference"])
                remaining = client.get("/api/backtest-runs").json()["runs"]
                self.assertEqual(len(remaining), 1)
                self.assertEqual(
                    client.get(f"/api/backtest-runs/{run_id}").status_code, 404
                )
                self.assertEqual(
                    client.delete(f"/api/backtest-runs/{run_id}").status_code, 404
                )

    def test_history_pages_filter_and_delete_only_explicit_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.sqlite3"
            repo = SQLiteBarRepository(path)
            for index in range(25):
                result = self.saved_chart_result([{}] if index == 24 else [])
                result["metadata"]["strategy"] = (
                    "Special Report" if index == 24 else "Ordinary"
                )
                repo.save_backtest_run(
                    result, "atomic", "scripted", None, {},
                )
            repo.save_backtest_run(
                self.saved_chart_result([]), "atomic", "scripted", None, {},
                "another-owner",
            )
            settings = LiveSettings(
                mode="mock", db_path=str(path),
                replay_csv=str(synthetic_csv()),
                replay_speed=1000, heartbeat_seconds=0.05,
            )
            app = create_app(
                settings,
                feed=ReplayFeed(settings.replay_csv, speed=1000, loop=False),
                repository=repo,
             strategy_provider=InertProvider())
            with TestClient(app) as client:
                first = client.get("/api/backtest-runs?limit=20&offset=0").json()
                second = client.get("/api/backtest-runs?limit=20&offset=20").json()
                self.assertEqual((first["total"], len(first["runs"]), first["has_more"]), (25, 20, True))
                self.assertEqual((second["total"], len(second["runs"]), second["has_more"]), (25, 5, False))
                self.assertFalse(
                    {run["run_id"] for run in first["runs"]}
                    & {run["run_id"] for run in second["runs"]}
                )
                matched = client.get(
                    "/api/backtest-runs?limit=20&search=special&outcome=traded&strategy_kind=atomic"
                ).json()
                self.assertEqual(matched["total"], 1)
                self.assertEqual(len(matched["runs"]), 1)
                self.assertEqual(
                    client.get("/api/backtest-runs?outcome=empty").json()["total"], 24
                )
                self.assertEqual(
                    client.get("/api/backtest-runs?strategy_kind=composite").json()["total"], 0
                )
                self.assertEqual(
                    client.get("/api/backtest-runs?outcome=invalid").status_code, 422
                )
                selected = [run["run_id"] for run in first["runs"]]
                deleted = client.request(
                    "DELETE", "/api/backtest-runs", json={"run_ids": selected}
                )
                self.assertEqual(deleted.json()["deleted_runs"], 20)
                remaining = client.get("/api/backtest-runs?limit=20").json()
                self.assertEqual(remaining["total"], 5)
                self.assertEqual(
                    {run["run_id"] for run in remaining["runs"]},
                    {run["run_id"] for run in second["runs"]},
                )

    def test_saved_chart_loads_one_intraday_session_and_all_of_its_trades(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.sqlite3"
            repo = SQLiteBarRepository(path)
            bars = [bar(minute, 100 + minute) for minute in range(60)]
            for item in bars:
                repo.save(item)
            trades = [
                {
                    "contract": "TMFU6", "direction": "long",
                    "entry_time": bars[5].time.isoformat(),
                    "exit_time": bars[8].time.isoformat(),
                    "entry_price": 105, "exit_price": 108, "net_pnl": 3,
                    "trading_date": "2026-08-25",
                },
                {
                    "contract": "TMFU6", "direction": "short",
                    "entry_time": bars[30].time.isoformat(),
                    "exit_time": bars[35].time.isoformat(),
                    "entry_price": 130, "exit_price": 135, "net_pnl": -5,
                    "session": "night", "trading_date": "2026-08-25",
                },
            ]
            saved_result = self.saved_chart_result(trades)
            saved_result["visualization"] = {
                "schema_version": 1,
                "strategy": {"key": "ma_crossover", "name": "MA Crossover"},
                "parameters": [],
                "panels": [],
                "overlays": [],
                "diagnostics": [{
                    "key": "zero", "label": "Zero", "panel": "strategy",
                    "type": "threshold", "color": "#64748b",
                    "points": [{"time": "2026-08-26T15:00:00+08:00", "value": 0}],
                    "metadata": {"value": 0},
                }, {
                    "key": "spread", "label": "Spread", "panel": "strategy",
                    "type": "line", "color": "#38bdf8",
                    "points": [
                        {"time": bars[5].time.isoformat(), "value": 1.5},
                        {"time": "2026-08-26T15:00:00+08:00", "value": 2.5},
                    ],
                }],
            }
            saved = repo.save_backtest_run(
                saved_result,
                "atomic", "ma_crossover", None, {},
            )
            settings = LiveSettings(
                mode="mock", db_path=str(path),
                replay_csv=str(synthetic_csv()),
                replay_speed=1000, heartbeat_seconds=0.05,
            )
            app = create_app(
                settings,
                feed=ReplayFeed(settings.replay_csv, speed=1000, loop=False),
                repository=repo,
             strategy_provider=InertProvider())
            with TestClient(app) as client:
                detail = client.get(f"/api/backtest-runs/{saved['run_id']}")
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertNotIn("visualization", detail.json()["result"])
                self.assertIn(
                    "visualization",
                    repo.backtest_run(saved["run_id"])["result"],
                )
                response = client.get(
                    f"/api/backtest-runs/{saved['run_id']}/chart?trade_index=0"
                )
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertEqual(payload["scope"]["kind"], "session")
                self.assertEqual(payload["scope"]["session"], "night")
                self.assertEqual(len(payload["bars"]), 60)
                self.assertEqual(
                    [trade["trade_index"] for trade in payload["trades"]], [0, 1]
                )
                diagnostics = {
                    item["key"]: item
                    for item in payload["visualization"]["diagnostics"]
                }
                self.assertEqual(diagnostics["zero"]["points"], [])
                self.assertEqual(
                    diagnostics["spread"]["points"],
                    [{"time": bars[5].time.isoformat(), "value": 1.5}],
                )

    def test_saved_daily_chart_uses_the_contract_range_not_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.sqlite3"
            repo = SQLiteBarRepository(path)
            first_night = bar(0, 100)
            first_day = first_night.copy(
                time=datetime(2026, 8, 25, 8, 45, tzinfo=TAIPEI),
                session="day", close=110,
            )
            second_night = first_night.copy(
                time=datetime(2026, 8, 25, 15, 0, tzinfo=TAIPEI),
                trading_date=date(2026, 8, 26), close=120,
            )
            second_day = first_night.copy(
                time=datetime(2026, 8, 26, 8, 45, tzinfo=TAIPEI),
                session="day", trading_date=date(2026, 8, 26), close=130,
            )
            for item in (first_night, first_day, second_night, second_day):
                repo.save(item)
            trades = [{
                "contract": "TMFU6", "direction": "long",
                "entry_time": first_night.time.isoformat(),
                "exit_time": second_night.time.isoformat(),
                "entry_price": 100, "exit_price": 120, "net_pnl": 20,
                "session": "night", "trading_date": "2026-08-25",
            }]
            saved = repo.save_backtest_run(
                self.saved_chart_result(trades, "1d"),
                "atomic", "ma_crossover", None, {},
            )
            settings = LiveSettings(
                mode="mock", db_path=str(path),
                replay_csv=str(synthetic_csv()),
                replay_speed=1000, heartbeat_seconds=0.05,
            )
            app = create_app(
                settings,
                feed=ReplayFeed(settings.replay_csv, speed=1000, loop=False),
                repository=repo,
             strategy_provider=InertProvider())
            with TestClient(app) as client:
                detail = client.get(f"/api/backtest-runs/{saved['run_id']}")
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertNotIn("visualization", detail.json()["result"])
                response = client.get(
                    f"/api/backtest-runs/{saved['run_id']}/chart?trade_index=0"
                )
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertEqual(payload["scope"]["kind"], "range")
                self.assertNotIn("session", payload["scope"])
                self.assertEqual(len(payload["bars"]), 2)
                self.assertEqual(
                    [item["trading_date"] for item in payload["bars"]],
                    ["2026-08-25", "2026-08-26"],
                )
                self.assertNotIn("visualization", payload)


    def test_batch_delete_is_atomic_and_can_delete_all_owned_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.sqlite3"
            repo = SQLiteBarRepository(path)
            for minute, close in enumerate([100, 100, 102, 103, 101, 99]):
                repo.save(bar(minute, close))
            settings = LiveSettings(
                mode="mock", db_path=str(path),
                replay_csv=str(synthetic_csv()),
                replay_speed=1000, heartbeat_seconds=0.05,
            )
            app = create_app(
                settings,
                feed=ReplayFeed(settings.replay_csv, speed=1000, loop=False),
                repository=repo,
             strategy_provider=InertProvider())
            payload = {
                "strategy": "scripted", "interval": "1m",
                "start": "2026-08-25", "end": "2026-08-25",
            }
            with TestClient(app) as client:
                run_ids = [
                    client.post("/api/backtest-runs", json=payload).json()[
                        "history_run_id"
                    ]
                    for _ in range(3)
                ]

                empty = client.request(
                    "DELETE", "/api/backtest-runs", json={"run_ids": []}
                )
                self.assertEqual(empty.status_code, 400, empty.text)

                deleted = client.request(
                    "DELETE", "/api/backtest-runs",
                    json={"run_ids": run_ids[:2]},
                )
                self.assertEqual(deleted.status_code, 200, deleted.text)
                self.assertEqual(deleted.json()["deleted_runs"], 2)
                self.assertEqual(
                    deleted.json()["released_strategy_references"], 0
                )

                missing = client.request(
                    "DELETE", "/api/backtest-runs",
                    json={"run_ids": [run_ids[2], "missing-run"]},
                )
                self.assertEqual(missing.status_code, 404, missing.text)
                self.assertEqual(
                    client.get(f"/api/backtest-runs/{run_ids[2]}").status_code,
                    200,
                )

                conflicting = client.request(
                    "DELETE", "/api/backtest-runs",
                    json={"run_ids": [run_ids[2]], "delete_all": True},
                )
                self.assertEqual(conflicting.status_code, 400, conflicting.text)

                invalid_exclusion = client.request(
                    "DELETE", "/api/backtest-runs",
                    json={"run_ids": [run_ids[2]], "exclude_run_ids": [run_ids[0]]},
                )
                self.assertEqual(invalid_exclusion.status_code, 400, invalid_exclusion.text)

                fourth_id = client.post(
                    "/api/backtest-runs", json=payload
                ).json()["history_run_id"]

                excluded = client.request(
                    "DELETE", "/api/backtest-runs",
                    json={"delete_all": True, "exclude_run_ids": [run_ids[2]]},
                )
                self.assertEqual(excluded.status_code, 200, excluded.text)
                self.assertEqual(excluded.json()["deleted_runs"], 1)
                self.assertEqual(
                    client.get(f"/api/backtest-runs/{run_ids[2]}").status_code,
                    200,
                )
                self.assertEqual(
                    client.get(f"/api/backtest-runs/{fourth_id}").status_code,
                    404,
                )

                deleted_all = client.request(
                    "DELETE", "/api/backtest-runs",
                    json={"delete_all": True},
                )
                self.assertEqual(deleted_all.status_code, 200, deleted_all.text)
                self.assertEqual(deleted_all.json()["deleted_runs"], 1)
                self.assertEqual(
                    client.get("/api/backtest-runs").json()["runs"], []
                )

    def test_batch_delete_never_crosses_owner_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SQLiteBarRepository(Path(directory) / "market.sqlite3")
            result = {
                "metadata": {
                    "strategy": "Scripted", "symbol": "TMF", "interval": "1m",
                    "date_range": "2026-08-25 ～ 2026-08-25",
                },
                "summary": {},
                "trades": [],
            }
            try:
                owner_one = repo.save_backtest_run(
                    result, "atomic", "scripted", None, {}, "owner-1"
                )
                owner_two = repo.save_backtest_run(
                    result, "atomic", "scripted", None, {}, "owner-2"
                )
                preserved = repo.save_backtest_run(
                    result, "atomic", "scripted", None, {}, "owner-1"
                )

                rejected = repo.delete_backtest_runs(
                    [owner_one["run_id"], owner_two["run_id"]],
                    owner_user_id="owner-1",
                )
                self.assertIsNone(rejected)
                self.assertIsNotNone(
                    repo.backtest_run(owner_one["run_id"], "owner-1")
                )

                deleted = repo.delete_backtest_runs(
                    [], delete_all=True, owner_user_id="owner-1",
                    exclude_run_ids=[preserved["run_id"], owner_two["run_id"]],
                )
                self.assertEqual(deleted["deleted_runs"], 1)
                self.assertIsNone(
                    repo.backtest_run(owner_one["run_id"], "owner-1")
                )
                self.assertIsNotNone(
                    repo.backtest_run(preserved["run_id"], "owner-1")
                )
                self.assertIsNotNone(
                    repo.backtest_run(owner_two["run_id"], "owner-2")
                )
            finally:
                repo.close()

    def test_empty_legacy_reference_table_is_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE backtest_runs (
                    run_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    strategy_version INTEGER NOT NULL,
                    strategy_snapshot_json TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
            connection.close()
            repo = SQLiteBarRepository(path)
            try:
                columns = {
                    row["name"]
                    for row in repo.connection.execute(
                        "PRAGMA table_info(backtest_runs)"
                    ).fetchall()
                }
                self.assertIn("strategy_kind", columns)
                self.assertIn("result_json", columns)
                self.assertIn("summary_json", columns)
                self.assertIn("trade_count", columns)
                self.assertNotIn("strategy_id", columns)
            finally:
                repo.close()

    def test_existing_results_are_backfilled_for_lightweight_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current-schema.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE backtest_runs (
                    run_id TEXT PRIMARY KEY,
                    strategy_kind TEXT NOT NULL,
                    strategy_key TEXT NOT NULL,
                    strategy_version INTEGER,
                    strategy_name TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    strategy_snapshot_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL
                )
                """
            )
            result = {
                "summary": {"net_profit": 123.0},
                "trades": [{"net_pnl": 123.0}],
                "equity": [],
            }
            connection.execute(
                "INSERT INTO backtest_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "run-1", "atomic", "scripted", None, "Scripted", "TMF", "1m",
                    "2026-08-25", "2026-08-25", "{}",
                    json.dumps(result), "completed",
                    "2026-08-25T00:00:00+08:00", "owner-1",
                ),
            )
            connection.commit()
            connection.close()

            repo = SQLiteBarRepository(path)
            try:
                listing = repo.backtest_runs(50, 0, owner_user_id="owner-1")
                self.assertEqual(listing[0]["summary"]["net_profit"], 123.0)
                self.assertEqual(listing[0]["trade_count"], 1)
            finally:
                repo.close()

    def test_backtest_date_query_uses_covering_order_index(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SQLiteBarRepository(Path(directory) / "market.sqlite3")
            try:
                plan = repo.connection.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM minute_bars "
                    "WHERE symbol=? AND status='closed' "
                    "AND trading_date BETWEEN ? AND ? "
                    "ORDER BY trading_date, time",
                    ("TMF", "2026-08-01", "2026-08-31"),
                ).fetchall()
                details = " ".join(row["detail"] for row in plan)
                self.assertIn(
                    "idx_minute_bars_symbol_status_date_time", details
                )
                self.assertNotIn("USE TEMP B-TREE", details)
            finally:
                repo.close()


if __name__ == "__main__":
    unittest.main()

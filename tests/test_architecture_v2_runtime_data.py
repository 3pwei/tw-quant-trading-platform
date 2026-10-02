from __future__ import annotations

import math
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tw_quant.live.runtime_data import strategy_lineage
from tw_quant.live.storage import SQLiteBarRepository


PLUGIN = {
    "plugin_id": "inert-scripted",
    "plugin_version": "0.1.0",
    "artifact_name": "inert-double",
    "artifact_version": "0.1.0",
    "artifact_digest": "sha256:" + "9" * 64,
    "parameter_schema_version": "1",
}


def backtest_result(strategy: str = "Scripted") -> dict[str, object]:
    return {
        "metadata": {
            "strategy": strategy,
            "symbol": "TMF",
            "interval": "1 分 K",
            "interval_key": "1m",
            "date_range": "2026-09-01 ～ 2026-09-01",
        },
        "summary": {"net_profit": 0},
        "trades": [],
        "equity": [],
        "bars": [],
    }


def runtime_payload(
    owner: str, version: int, lineage: dict[str, object]
) -> dict[str, object]:
    return {
        "owner_user_id": owner,
        "strategy_kind": "atomic",
        "strategy_id": "scripted",
        "strategy_version": version,
        "strategy_snapshot": {
            "strategy": "scripted",
            "parameters": {"window": 15},
            "atomic_strategy_version": version,
            "plugin_identity": PLUGIN,
        },
        "strategy_lineage": lineage,
        "symbol": "TMF",
        "interval": "1m",
        "quantity": 1,
        "mode": "observe",
        "last_evaluated_bar": None,
    }


class ArchitectureV2RuntimeDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.repo = SQLiteBarRepository(self.path)

    def tearDown(self) -> None:
        self.repo.close()
        self.temp.cleanup()

    def save_atomic(
        self,
        owner: str = "owner-a",
        value: int = 15,
        strategy: str = "scripted",
    ) -> dict[str, object]:
        saved = self.repo.save_strategy_parameters(
            strategy,
            {"window": value},
            owner,
            PLUGIN,
        )
        assert saved is not None
        return saved

    def test_parameter_saves_create_immutable_versions_without_reinterpreting_legacy(self) -> None:
        self.repo.save_strategy_parameters(
            "legacy", {"period": 10}, "owner-a"
        )
        first = self.save_atomic(value=15)
        second = self.save_atomic(value=30)

        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(
            self.repo.strategy_parameters("owner-a")["scripted"]["window"],
            30,
        )
        self.assertEqual(
            [item["version"] for item in self.repo.strategy_versions("scripted", "owner-a")],
            [2, 1],
        )
        self.assertEqual(first["lineage"]["plugin"]["plugin_version"], "0.1.0")
        self.assertEqual(
            self.repo.connection.execute(
                "SELECT COUNT(*) FROM strategy_versions WHERE strategy_key='legacy'"
            ).fetchone()[0],
            0,
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.repo.connection.execute(
                "UPDATE strategy_versions SET parameters_json='{}' "
                "WHERE owner_user_id='owner-a' AND strategy_key='scripted' AND version=1"
            )
        self.repo.connection.rollback()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.repo.connection.execute(
                "DELETE FROM strategy_versions WHERE owner_user_id='owner-a' "
                "AND strategy_key='scripted' AND version=1"
            )
        self.repo.connection.rollback()

    def test_concurrent_version_allocation_is_monotonic_and_gap_free(self) -> None:
        self.repo.close()
        repositories = [SQLiteBarRepository(self.path) for _ in range(8)]
        barrier = threading.Barrier(len(repositories))
        versions: list[int] = []
        failures: list[BaseException] = []
        result_lock = threading.Lock()

        def save(index: int, repository: SQLiteBarRepository) -> None:
            try:
                barrier.wait()
                result = repository.save_strategy_parameters(
                    "scripted",
                    {"window": index + 1},
                    "owner-a",
                    PLUGIN,
                )
                assert result is not None
                with result_lock:
                    versions.append(int(result["version"]))
            except BaseException as exc:  # pragma: no cover - diagnostic capture
                with result_lock:
                    failures.append(exc)

        threads = [
            threading.Thread(target=save, args=(index, repository))
            for index, repository in enumerate(repositories)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        for repository in repositories:
            repository.close()
        self.assertEqual(failures, [])
        self.assertEqual(sorted(versions), list(range(1, 9)))
        self.repo = SQLiteBarRepository(self.path)

    def test_portfolio_versions_are_owner_scoped_weighted_and_reference_exact(self) -> None:
        first = self.save_atomic("owner-a", 15)
        second = self.save_atomic("owner-a", 30)
        self.save_atomic("owner-b", 99)

        invalid_members = (
            [{"kind": "atomic", "key": "scripted", "version": 999, "weight": 1.0}],
            [{"kind": "atomic", "key": "scripted", "version": 1, "weight": -1.0}],
            [{"kind": "atomic", "key": "scripted", "version": 1, "weight": math.nan}],
            [{"kind": "atomic", "key": "scripted", "weight": 1.0}],
            [{"kind": "atomic", "key": "scripted", "version": 1, "weight": 0.5}],
        )
        for members in invalid_members:
            with self.subTest(members=members), self.assertRaises(ValueError):
                self.repo.save_portfolio_version(
                    "portfolio-invalid", "Invalid", members, "owner-a"
                )
        self.assertIsNone(self.repo.portfolio("portfolio-invalid", None, "owner-a"))

        version_one = self.repo.save_portfolio_version(
            "portfolio-1",
            "Core Portfolio",
            [
                {
                    "kind": "atomic",
                    "key": "scripted",
                    "version": first["version"],
                    "weight": 1.0,
                }
            ],
            "owner-a",
        )
        version_two = self.repo.save_portfolio_version(
            "portfolio-1",
            "Core Portfolio",
            [
                {
                    "kind": "atomic",
                    "key": "scripted",
                    "version": second["version"],
                    "weight": 1.0,
                }
            ],
            "owner-a",
        )
        self.assertEqual((version_one["version"], version_two["version"]), (1, 2))
        self.assertEqual(
            self.repo.portfolio("portfolio-1", 1, "owner-a")["members"][0][
                "strategy_version"
            ],
            1,
        )
        self.assertIsNone(self.repo.portfolio("portfolio-1", 1, "owner-b"))
        with self.assertRaises(ValueError):
            self.repo.save_portfolio_version(
                "cross-owner",
                "Cross Owner",
                [{"kind": "atomic", "key": "scripted", "version": 2, "weight": 1.0}],
                "owner-b",
            )
        self.repo.archive_portfolio("portfolio-1", "owner-a")
        with self.assertRaisesRegex(ValueError, "archived"):
            self.repo.save_portfolio_version(
                "portfolio-1",
                "Core Portfolio",
                [{"kind": "atomic", "key": "scripted", "version": 2, "weight": 1.0}],
                "owner-a",
            )

    def test_composite_portfolio_reference_requires_known_lineage_and_protects_version(self) -> None:
        definition = {"name": "Legacy Composite"}
        legacy = self.repo.save_composite_strategy(
            "legacy-composite", definition, "owner-a"
        )
        with self.assertRaisesRegex(ValueError, "legacy-unknown"):
            self.repo.save_portfolio_version(
                "portfolio-legacy",
                "Legacy",
                [
                    {
                        "kind": "composite",
                        "key": "legacy-composite",
                        "version": legacy["version"],
                        "weight": 1.0,
                    }
                ],
                "owner-a",
            )
        known = self.repo.save_composite_strategy(
            "known-composite", {"name": "Known Composite"}, "owner-a", PLUGIN
        )
        self.repo.save_portfolio_version(
            "portfolio-known",
            "Known",
            [
                {
                    "kind": "composite",
                    "key": "known-composite",
                    "version": known["version"],
                    "weight": 1.0,
                }
            ],
            "owner-a",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.connection.execute(
                "DELETE FROM composite_strategies WHERE owner_user_id=? "
                "AND strategy_id=? AND version=?",
                ("owner-a", "known-composite", known["version"]),
            )
        self.repo.connection.rollback()

    def test_risk_profiles_and_bindings_never_authorize_live_or_clear_kill_switch(self) -> None:
        atomic = self.save_atomic()
        self.repo.connection.execute(
            "CREATE TABLE live_kill_switch_state(scope TEXT, scope_key TEXT, "
            "action TEXT, reason TEXT, activated_at TEXT, PRIMARY KEY(scope, scope_key))"
        )
        self.repo.connection.execute(
            "INSERT INTO live_kill_switch_state VALUES "
            "('global','*','block_all','test','2026-10-01T00:00:00Z')"
        )
        self.repo.connection.commit()

        first = self.repo.save_risk_profile_version(
            "live-risk", "Live Risk", "live", {"max_position": 1}, "owner-a"
        )
        second = self.repo.save_risk_profile_version(
            "live-risk", "Live Risk", "live", {"max_position": 2}, "owner-a"
        )
        self.assertEqual((first["version"], second["version"]), (1, 2))
        binding = self.repo.bind_risk_profile(
            "live-risk",
            1,
            {"kind": "atomic", "key": "scripted", "version": atomic["version"]},
            "owner-a",
        )
        self.assertFalse(binding["live_authorized"])
        self.assertFalse(binding["arm_authorized"])
        self.assertFalse(binding["kill_switch_override"])
        self.assertEqual(
            self.repo.connection.execute(
                "SELECT action FROM live_kill_switch_state WHERE scope='global'"
            ).fetchone()[0],
            "block_all",
        )
        for values in (
            {"live_enabled": True},
            {"bypass_arm": True},
            {"clear_kill_switch": True},
            {"api_key": "not-allowed"},
            {"accessToken": "not-allowed"},
            {"max_position": math.inf},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.repo.save_risk_profile_version(
                    "unsafe", "Unsafe", "live", values, "owner-a"
                )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.connection.execute(
                "UPDATE risk_profile_bindings SET authorizes_live=1 "
                "WHERE binding_id=?",
                (binding["binding_id"],),
            )
        self.repo.connection.rollback()

    def test_new_backtest_and_runtime_lineage_is_exact_while_old_rows_are_unknown(self) -> None:
        old = self.repo.save_backtest_run(
            backtest_result(), "atomic", "scripted", None, {}, "owner-a"
        )
        self.assertEqual(old["strategy_lineage"], {"status": "legacy_unknown"})

        atomic = self.save_atomic()
        new = self.repo.save_backtest_run(
            backtest_result(),
            "atomic",
            "scripted",
            None,
            dict(atomic["parameters"]),
            "owner-a",
            dict(atomic["lineage"]),
        )
        self.assertEqual(new["strategy_lineage"]["strategy_version"], 1)
        runtime = self.repo.create_trading_runtime(
            runtime_payload("owner-a", 1, dict(atomic["lineage"]))
        )
        self.assertEqual(runtime["strategy_lineage"]["strategy_version"], 1)
        self.assertEqual(
            self.repo.trading_runtime(str(runtime["runtime_id"]), "owner-a")[
                "strategy_lineage"
            ]["plugin"]["artifact_digest"],
            PLUGIN["artifact_digest"],
        )

    def test_migration_is_additive_idempotent_restart_safe_and_legacy_readable(self) -> None:
        self.repo.close()
        for candidate in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            if candidate.exists():
                candidate.unlink()
        legacy = sqlite3.connect(self.path)
        result = backtest_result("Legacy")
        stored_result = {key: value for key, value in result.items() if key != "bars"}
        legacy.executescript(
            """
            CREATE TABLE strategy_parameters (
                strategy TEXT PRIMARY KEY,
                parameters_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
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
                summary_json TEXT NOT NULL DEFAULT '{}',
                trade_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                owner_user_id TEXT NOT NULL DEFAULT '__legacy__'
            );
            """
        )
        legacy.execute(
            "INSERT INTO strategy_parameters VALUES (?,?,?)",
            ("legacy", json.dumps({"period": 10}), "2026-09-01T00:00:00Z"),
        )
        legacy.execute(
            "INSERT INTO backtest_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-run",
                "atomic",
                "legacy",
                None,
                "Legacy",
                "TMF",
                "1m",
                "2026-09-01",
                "2026-09-01",
                json.dumps({"period": 10}),
                json.dumps(stored_result),
                json.dumps(result["summary"]),
                0,
                "completed",
                "2026-09-01T00:00:00Z",
                "__legacy__",
            ),
        )
        legacy.commit()
        legacy.close()

        self.repo = SQLiteBarRepository(self.path)
        self.repo.claim_legacy_ownership("owner-a")
        self.repo.close()
        self.repo = SQLiteBarRepository(self.path)

        self.assertEqual(
            self.repo.connection.execute(
                "SELECT COUNT(*) FROM architecture_v2_schema_migrations "
                "WHERE migration_id='p4-runtime-data-v1'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.repo.backtest_run("legacy-run", "owner-a")["strategy_lineage"],
            {"status": "legacy_unknown"},
        )
        self.assertEqual(
            self.repo.strategy_parameters("owner-a")["legacy"], {"period": 10}
        )
        self.assertEqual(
            self.repo.connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
            "wal",
        )
        self.assertEqual(self.repo.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

        legacy_reader = sqlite3.connect(self.path)
        legacy_reader.row_factory = sqlite3.Row
        try:
            row = legacy_reader.execute(
                "SELECT strategy,parameters_json,updated_at FROM strategy_parameters "
                "WHERE owner_user_id=?",
                ("owner-a",),
            ).fetchone()
            self.assertEqual(row["strategy"], "legacy")
            base = legacy_reader.execute(
                "SELECT run_id,strategy_kind,strategy_key,strategy_version,"
                "strategy_snapshot_json FROM backtest_runs WHERE run_id=?",
                ("legacy-run",),
            ).fetchone()
            self.assertEqual(base["strategy_key"], "legacy")
        finally:
            legacy_reader.close()

    def test_failed_portfolio_write_rolls_back_identity_and_version(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.save_portfolio_version(
                "rolled-back",
                "Rolled Back",
                [{"kind": "atomic", "key": "missing", "version": 1, "weight": 1.0}],
                "owner-a",
            )
        self.assertEqual(
            self.repo.connection.execute(
                "SELECT COUNT(*) FROM portfolios WHERE portfolio_id='rolled-back'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.repo.connection.execute(
                "SELECT COUNT(*) FROM portfolio_versions WHERE portfolio_id='rolled-back'"
            ).fetchone()[0],
            0,
        )

    def test_runtime_mutations_do_not_touch_git_actions_build_or_deployment(self) -> None:
        git_root = Path(self.temp.name) / "git-proof"
        git_root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=git_root, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=git_root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=git_root,
            check=True,
        )
        marker = git_root / "marker.txt"
        marker.write_text("immutable\n", encoding="utf-8")
        subprocess.run(["git", "add", "marker.txt"], cwd=git_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=git_root, check=True)
        before_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=git_root, text=True
        ).strip()
        before_ref = (git_root / ".git/refs/heads/main").read_bytes()

        with patch("subprocess.run") as process_run, patch(
            "socket.create_connection"
        ) as network_connect:
            atomic = self.save_atomic()
            portfolio = self.repo.save_portfolio_version(
                "runtime-only",
                "Runtime Only",
                [
                    {
                        "kind": "atomic",
                        "key": "scripted",
                        "version": atomic["version"],
                        "weight": 1.0,
                    }
                ],
                "owner-a",
            )
            self.repo.save_risk_profile_version(
                "paper-risk", "Paper Risk", "paper", {"max_position": 1}, "owner-a"
            )
            process_run.assert_not_called()
            network_connect.assert_not_called()
            self.assertEqual(portfolio["version"], 1)

        after_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=git_root, text=True
        ).strip()
        self.assertEqual(after_head, before_head)
        self.assertEqual((git_root / ".git/refs/heads/main").read_bytes(), before_ref)
        self.assertEqual(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=git_root, text=True
            ),
            "",
        )


if __name__ == "__main__":
    unittest.main()

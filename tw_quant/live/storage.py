from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Protocol
from uuid import uuid4

from ..market import KBar
from .runtime_data import (
    PortfolioRepository,
    RiskProfileRepository,
    SQLiteRuntimeDataMixin,
    StrategyVersionRepository,
)

DEFAULT_OWNER_ID = "__legacy__"


class StrategyPurgeError(ValueError):
    """Raised when a composite strategy cannot be permanently deleted."""


class StrategyNameConflictError(ValueError):
    """Raised when an owner already has a composite strategy with this name."""


class StrategyDependencyError(StrategyPurgeError):
    """Raised when another composite strategy references a strategy version."""


class StrategyReferencedError(StrategyPurgeError):
    def __init__(self, references: dict[str, int]):
        self.references = references
        labels = ", ".join(
            f"{strategy_id} ({count} 筆)"
            for strategy_id, count in references.items()
        )
        super().__init__(f"策略已有回測引用，禁止永久刪除：{labels}")


class MarketRepository(Protocol):
    def save(self, bar: KBar) -> None: ...
    def latest(self, symbol: str, limit: int) -> list[KBar]: ...
    def latest_forming(self, symbol: str) -> KBar | None: ...
    def date_bounds(self, symbol: str) -> tuple[date | None, date | None]: ...
    def replay_availability(self, symbol: str) -> list[dict[str, object]]: ...
    def between_trading_dates(
        self, symbol: str, start: date, end: date
    ) -> list[KBar]: ...
    def tick_seen(self, key: str) -> bool: ...
    def remember_tick(self, key: str, exchange_time: datetime) -> None: ...
    def purge_backfill(self, symbol: str, contract: str) -> None: ...


class StrategyRepository(StrategyVersionRepository, Protocol):
    def strategy_parameters(
        self, owner_user_id: str | None = None
    ) -> dict[str, dict[str, object]]: ...
    def save_strategy_parameters(
        self,
        strategy: str,
        parameters: dict[str, int | float],
        owner_user_id: str | None = None,
        plugin_identity: dict[str, object] | None = None,
    ) -> dict[str, object] | None: ...
    def composite_strategies(
        self, owner_user_id: str | None = None
    ) -> list[dict[str, object]]: ...
    def archived_composite_strategies(
        self, owner_user_id: str | None = None
    ) -> list[dict[str, object]]: ...
    def composite_strategy_versions(
        self, strategy_id: str, owner_user_id: str | None = None
    ) -> list[dict[str, object]]: ...
    def composite_strategy(
        self,
        strategy_id: str,
        version: int | None = None,
        owner_user_id: str | None = None,
    ) -> dict[str, object] | None: ...
    def save_composite_strategy(
        self,
        strategy_id: str,
        definition: dict[str, object],
        owner_user_id: str | None = None,
        plugin_identity: dict[str, object] | None = None,
    ) -> dict[str, object]: ...
    def composite_strategy_archived(
        self, strategy_id: str, owner_user_id: str | None = None
    ) -> bool: ...
    def archive_composite_strategy(
        self, strategy_id: str, owner_user_id: str | None = None
    ) -> dict[str, object]: ...
    def purge_archived_composite_strategies(
        self, strategy_ids: list[str], owner_user_id: str | None = None
    ) -> dict[str, object]: ...


class BacktestRepository(Protocol):
    def save_backtest_run(
        self,
        result: dict[str, object],
        strategy_kind: str,
        strategy_key: str,
        strategy_version: int | None,
        strategy_snapshot: dict[str, object],
        owner_user_id: str | None = None,
        strategy_lineage: dict[str, object] | None = None,
    ) -> dict[str, object]: ...
    def backtest_runs(
        self,
        limit: int,
        offset: int,
        strategy_key: str | None = None,
        owner_user_id: str | None = None,
        strategy_kind: str | None = None,
        outcome: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, object]]: ...
    def count_backtest_runs(
        self,
        strategy_key: str | None = None,
        owner_user_id: str | None = None,
        strategy_kind: str | None = None,
        outcome: str | None = None,
        search: str | None = None,
    ) -> int: ...
    def backtest_run(
        self, run_id: str, owner_user_id: str | None = None
    ) -> dict[str, object] | None: ...
    def delete_backtest_run(
        self, run_id: str, owner_user_id: str | None = None
    ) -> dict[str, object] | None: ...
    def delete_backtest_runs(
        self,
        run_ids: list[str],
        delete_all: bool = False,
        owner_user_id: str | None = None,
        exclude_run_ids: list[str] | None = None,
    ) -> dict[str, object] | None: ...


class TradingRuntimeRepository(Protocol):
    def create_trading_runtime(
        self, runtime: dict[str, object]
    ) -> dict[str, object]: ...
    def trading_runtimes(
        self, owner_user_id: str
    ) -> list[dict[str, object]]: ...
    def trading_runtime(
        self, runtime_id: str, owner_user_id: str
    ) -> dict[str, object] | None: ...
    def active_trading_runtimes(
        self, symbol: str
    ) -> list[dict[str, object]]: ...
    def paper_auto_runtimes(
        self, symbol: str
    ) -> list[dict[str, object]]: ...
    def lock_paper_auto_runtimes_for_recovery(
        self, symbol: str
    ) -> list[dict[str, object]]: ...
    def set_trading_runtime_recovery(
        self,
        runtime_id: str,
        owner_user_id: str,
        issue: str | None,
    ) -> dict[str, object] | None: ...
    def trading_decision(
        self, decision_id: str, owner_user_id: str
    ) -> dict[str, object] | None: ...
    def runtime_metrics(self) -> dict[str, object]: ...
    def paper_shadow_metrics(self, owner_user_id: str) -> dict[str, int]: ...
    def stop_trading_runtime(
        self, runtime_id: str, owner_user_id: str
    ) -> dict[str, object] | None: ...
    def set_trading_runtime_status(
        self,
        runtime_id: str,
        owner_user_id: str,
        status: str,
        last_evaluated_bar: str | None = None,
    ) -> dict[str, object] | None: ...
    def trading_decisions(
        self, runtime_id: str, owner_user_id: str, limit: int
    ) -> list[dict[str, object]]: ...
    def record_runtime_evaluation(
        self,
        runtime_id: str,
        evaluated_bar: str,
        decisions: list[dict[str, object]],
    ) -> list[dict[str, object]]: ...
    def update_decision_execution(
        self, decision_id: str, owner_user_id: str, values: dict[str, object]
    ) -> dict[str, object] | None: ...
    def auto_entry_decisions(
        self, symbol: str, statuses: tuple[str, ...]
    ) -> list[dict[str, object]]: ...
    def auto_decisions(
        self, symbol: str, statuses: tuple[str, ...]
    ) -> list[dict[str, object]]: ...


class ApplicationRepository(
    MarketRepository, StrategyRepository, BacktestRepository,
    TradingRuntimeRepository, PortfolioRepository, RiskProfileRepository, Protocol
):
    """Composition-root contract implemented by the shared SQLite adapter."""

    def claim_legacy_ownership(self, owner_user_id: str) -> None: ...
    def close(self) -> None: ...


# Compatibility name for integrations that still inject one repository object.
# Application services must depend on the capability-specific ports above.
BarRepository = ApplicationRepository


class SQLiteBarRepository(SQLiteRuntimeDataMixin):
    def __init__(self, path: str | Path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(target, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = Lock()
        self.duplicate_decisions_blocked = 0
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS minute_bars (
                symbol TEXT NOT NULL,
                contract TEXT NOT NULL,
                time TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER NOT NULL,
                status TEXT NOT NULL,
                session TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                first_tick_time TEXT NOT NULL,
                last_tick_time TEXT NOT NULL,
                exchange_time TEXT NOT NULL,
                received_time TEXT NOT NULL,
                latency_ms REAL NOT NULL,
                no_trade INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(symbol, contract, time)
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_minute_bars_symbol_status_date_time "
            "ON minute_bars(symbol, status, trading_date, time)"
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_ticks (
                dedup_key TEXT PRIMARY KEY,
                exchange_time TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_parameters (
                owner_user_id TEXT NOT NULL,
                strategy TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(owner_user_id, strategy)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS composite_strategies (
                strategy_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                name TEXT NOT NULL,
                definition_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                owner_user_id TEXT NOT NULL DEFAULT '__legacy__',
                PRIMARY KEY(strategy_id, version)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS archived_composite_strategies (
                strategy_id TEXT PRIMARY KEY,
                archived_at TEXT NOT NULL,
                owner_user_id TEXT NOT NULL DEFAULT '__legacy__'
            )
            """
        )
        self.connection.commit()
        self._ensure_backtest_schema()
        self._ensure_backtest_summary_schema()
        self._ensure_ownership_schema()
        self._ensure_composite_dependency_schema()
        self._ensure_trading_runtime_schema()
        self._ensure_architecture_v2_runtime_schema()

    def _ensure_backtest_schema(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(backtest_runs)"
            ).fetchall()
        }
        legacy_rows: list[sqlite3.Row] = []
        if columns and "strategy_kind" not in columns:
            legacy_rows = self.connection.execute(
                "SELECT * FROM backtest_runs"
            ).fetchall()
            self.connection.execute(
                "ALTER TABLE backtest_runs RENAME TO backtest_runs_legacy"
            )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backtest_runs (
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
                owner_user_id TEXT NOT NULL DEFAULT '__legacy__',
                CHECK(strategy_kind IN ('atomic', 'composite')),
                CHECK(
                    (strategy_kind='atomic' AND strategy_version IS NULL)
                    OR
                    (strategy_kind='composite' AND strategy_version IS NOT NULL)
                ),
                FOREIGN KEY(strategy_key, strategy_version)
                    REFERENCES composite_strategies(strategy_id, version)
                    ON DELETE RESTRICT
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_backtest_runs_created "
            "ON backtest_runs(created_at DESC)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy "
            "ON backtest_runs(strategy_key, strategy_version)"
        )
        for row in legacy_rows:
            result = {
                "metadata": {
                    "symbol": "TMF",
                    "strategy": row["strategy_id"],
                    "strategy_key": row["strategy_id"],
                    "strategy_version": row["strategy_version"],
                    "interval": "多週期",
                    "date_range": f"{row['start_date']} ～ {row['end_date']}",
                },
                "config": json.loads(row["parameters_json"]),
                "summary": json.loads(row["metrics_json"]),
                "trades": [],
                "equity": [],
            }
            self.connection.execute(
                "INSERT INTO backtest_runs("
                "run_id,strategy_kind,strategy_key,strategy_version,strategy_name,"
                "symbol,interval,start_date,end_date,strategy_snapshot_json,"
                "result_json,summary_json,trade_count,status,created_at,owner_user_id"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["run_id"], "composite", row["strategy_id"],
                    row["strategy_version"], row["strategy_id"], "TMF", "multi",
                    row["start_date"], row["end_date"],
                    row["strategy_snapshot_json"],
                    json.dumps(result, ensure_ascii=False),
                    json.dumps(result["summary"], ensure_ascii=False), 0,
                    row["status"],
                    row["created_at"], DEFAULT_OWNER_ID,
                ),
            )
        if legacy_rows or "strategy_kind" not in columns and columns:
            self.connection.execute("DROP TABLE IF EXISTS backtest_runs_legacy")
        self.connection.commit()

    def _ensure_backtest_summary_schema(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(backtest_runs)"
            ).fetchall()
        }
        needs_backfill = False
        if "summary_json" not in columns:
            self.connection.execute(
                "ALTER TABLE backtest_runs ADD COLUMN summary_json TEXT "
                "NOT NULL DEFAULT '{}'"
            )
            needs_backfill = True
        if "trade_count" not in columns:
            self.connection.execute(
                "ALTER TABLE backtest_runs ADD COLUMN trade_count INTEGER "
                "NOT NULL DEFAULT 0"
            )
            needs_backfill = True
        if needs_backfill:
            rows = self.connection.execute(
                "SELECT run_id, result_json FROM backtest_runs"
            ).fetchall()
            summaries = []
            for row in rows:
                result = json.loads(row["result_json"])
                summaries.append((
                    json.dumps(result.get("summary", {}), ensure_ascii=False),
                    len(result.get("trades", [])),
                    row["run_id"],
                ))
            self.connection.executemany(
                "UPDATE backtest_runs SET summary_json=?, trade_count=? "
                "WHERE run_id=?",
                summaries,
            )
        self.connection.commit()

    def _ensure_ownership_schema(self) -> None:
        strategy_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(strategy_parameters)"
            ).fetchall()
        }
        if "owner_user_id" not in strategy_columns:
            self.connection.execute(
                "ALTER TABLE strategy_parameters RENAME TO strategy_parameters_legacy"
            )
            self.connection.execute(
                """
                CREATE TABLE strategy_parameters (
                    owner_user_id TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_user_id, strategy)
                )
                """
            )
            self.connection.execute(
                "INSERT INTO strategy_parameters "
                "SELECT ?, strategy, parameters_json, updated_at "
                "FROM strategy_parameters_legacy",
                (DEFAULT_OWNER_ID,),
            )
            self.connection.execute("DROP TABLE strategy_parameters_legacy")

        for table in (
            "composite_strategies",
            "archived_composite_strategies",
            "backtest_runs",
        ):
            columns = {
                row["name"]
                for row in self.connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            if "owner_user_id" not in columns:
                self.connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN owner_user_id TEXT "
                    f"NOT NULL DEFAULT '{DEFAULT_OWNER_ID}'"
                )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_composite_owner "
            "ON composite_strategies(owner_user_id, strategy_id, version)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_archived_composite_owner "
            "ON archived_composite_strategies(owner_user_id, strategy_id)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_backtest_owner_created "
            "ON backtest_runs(owner_user_id, created_at DESC)"
        )
        self.connection.commit()

    def _ensure_composite_dependency_schema(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS composite_strategy_dependencies (
                owner_user_id TEXT NOT NULL,
                parent_strategy_id TEXT NOT NULL,
                parent_version INTEGER NOT NULL,
                child_strategy_id TEXT NOT NULL,
                child_version INTEGER NOT NULL,
                PRIMARY KEY(
                    parent_strategy_id, parent_version,
                    child_strategy_id, child_version
                ),
                FOREIGN KEY(parent_strategy_id, parent_version)
                    REFERENCES composite_strategies(strategy_id, version)
                    ON DELETE CASCADE,
                FOREIGN KEY(child_strategy_id, child_version)
                    REFERENCES composite_strategies(strategy_id, version)
                    ON DELETE RESTRICT
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_composite_dependency_child "
            "ON composite_strategy_dependencies("
            "owner_user_id, child_strategy_id, child_version)"
        )
        self.connection.commit()

    def _ensure_trading_runtime_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS strategy_runtimes (
                runtime_id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                strategy_kind TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_version INTEGER,
                strategy_snapshot_json TEXT NOT NULL,
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                mode TEXT NOT NULL,
                broker_name TEXT,
                account_id TEXT,
                execution_target_id TEXT,
                status TEXT NOT NULL,
                recovery_issue TEXT,
                recovery_checked_at TEXT,
                last_evaluated_bar TEXT,
                last_decision TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK(strategy_kind IN ('atomic', 'composite')),
                CHECK(mode IN ('observe', 'paper_auto', 'live_shadow', 'live_auto')),
                CHECK(status IN (
                    'active', 'paused', 'armed', 'recovery_locked', 'stopped'
                )),
                CHECK((mode IN ('live_shadow','live_auto') AND broker_name IS NOT NULL AND account_id IS NOT NULL)
                   OR (mode NOT IN ('live_shadow','live_auto') AND broker_name IS NULL AND account_id IS NULL))
            );
            CREATE INDEX IF NOT EXISTS idx_strategy_runtimes_owner_updated
                ON strategy_runtimes(owner_user_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_strategy_runtimes_active_symbol
                ON strategy_runtimes(status, symbol);
            CREATE TABLE IF NOT EXISTS trading_decisions (
                decision_id TEXT PRIMARY KEY,
                runtime_id TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_version INTEGER,
                symbol TEXT NOT NULL,
                contract TEXT NOT NULL,
                interval TEXT NOT NULL,
                trigger_time TEXT NOT NULL,
                direction TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                context_json TEXT NOT NULL,
                source_bar_time TEXT NOT NULL,
                source_bar_id TEXT,
                execution_status TEXT NOT NULL DEFAULT 'not_applicable',
                execution_reason TEXT,
                order_id TEXT,
                reference_price REAL,
                planned_stop_price REAL,
                actual_fill_price REAL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(runtime_id) REFERENCES strategy_runtimes(runtime_id)
                    ON DELETE CASCADE,
                CHECK(direction IN ('long', 'short')),
                CHECK(action IN ('entry', 'exit', 'none'))
            );
            CREATE INDEX IF NOT EXISTS idx_trading_decisions_runtime_time
                ON trading_decisions(runtime_id, trigger_time DESC);
            """
        )
        runtime_sql = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='strategy_runtimes'"
        ).fetchone()[0]
        if "'armed'" not in runtime_sql:
            self._upgrade_trading_runtime_schema()
            runtime_sql = self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='strategy_runtimes'"
            ).fetchone()[0]
        if "'recovery_locked'" not in runtime_sql:
            self._upgrade_paper_auto_recovery_schema()
            runtime_sql = self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='strategy_runtimes'"
            ).fetchone()[0]
        if "'live_shadow'" not in runtime_sql:
            self._upgrade_live_shadow_runtime_schema()
            runtime_sql = self.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='strategy_runtimes'"
            ).fetchone()[0]
        if "'live_auto'" not in runtime_sql:
            self._upgrade_live_auto_runtime_schema()
        runtime_columns = {
            str(row[1]) for row in self.connection.execute(
                "PRAGMA table_info(strategy_runtimes)"
            ).fetchall()
        }
        if "execution_target_id" not in runtime_columns:
            self.connection.execute(
                "ALTER TABLE strategy_runtimes ADD COLUMN execution_target_id TEXT"
            )
        decision_columns = {
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(trading_decisions)"
            ).fetchall()
        }
        for name, definition in (
            ("execution_status", "TEXT NOT NULL DEFAULT 'not_applicable'"),
            ("execution_reason", "TEXT"),
            ("order_id", "TEXT"),
            ("reference_price", "REAL"),
            ("planned_stop_price", "REAL"),
            ("actual_fill_price", "REAL"),
            ("source_bar_id", "TEXT"),
        ):
            if name not in decision_columns:
                self.connection.execute(
                    f"ALTER TABLE trading_decisions ADD COLUMN {name} {definition}"
                )
        self.connection.commit()

    def _upgrade_live_auto_runtime_schema(self) -> None:
        """Extend the existing runtime table while preserving all decisions."""
        self.connection.execute("PRAGMA foreign_keys=OFF")
        try:
            self.connection.executescript(
                """
                ALTER TABLE strategy_runtimes RENAME TO strategy_runtimes_live_auto_legacy;
                CREATE TABLE strategy_runtimes (
                    runtime_id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL,
                    strategy_kind TEXT NOT NULL, strategy_id TEXT NOT NULL,
                    strategy_version INTEGER, strategy_snapshot_json TEXT NOT NULL,
                    symbol TEXT NOT NULL, interval TEXT NOT NULL,
                    quantity INTEGER NOT NULL, mode TEXT NOT NULL,
                    broker_name TEXT, account_id TEXT, status TEXT NOT NULL,
                    recovery_issue TEXT, recovery_checked_at TEXT,
                    last_evaluated_bar TEXT, last_decision TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    CHECK(strategy_kind IN ('atomic', 'composite')),
                    CHECK(mode IN ('observe', 'paper_auto', 'live_shadow', 'live_auto')),
                    CHECK(status IN ('active','paused','armed','recovery_locked','stopped')),
                    CHECK((mode IN ('live_shadow','live_auto') AND broker_name IS NOT NULL AND account_id IS NOT NULL)
                       OR (mode NOT IN ('live_shadow','live_auto') AND broker_name IS NULL AND account_id IS NULL))
                );
                INSERT INTO strategy_runtimes SELECT * FROM strategy_runtimes_live_auto_legacy;
                DROP TABLE strategy_runtimes_live_auto_legacy;
                CREATE INDEX idx_strategy_runtimes_owner_updated
                    ON strategy_runtimes(owner_user_id, updated_at DESC);
                CREATE INDEX idx_strategy_runtimes_active_symbol
                    ON strategy_runtimes(status, symbol);
                """
            )
        finally:
            self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.commit()

    def _upgrade_live_shadow_runtime_schema(self) -> None:
        """Add live_shadow and immutable target columns without changing Paper rows."""
        self.connection.execute("PRAGMA foreign_keys=OFF")
        try:
            self.connection.executescript(
                """
                ALTER TABLE trading_decisions RENAME TO trading_decisions_shadow_legacy;
                ALTER TABLE strategy_runtimes RENAME TO strategy_runtimes_shadow_legacy;
                CREATE TABLE strategy_runtimes (
                    runtime_id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL,
                    strategy_kind TEXT NOT NULL, strategy_id TEXT NOT NULL,
                    strategy_version INTEGER, strategy_snapshot_json TEXT NOT NULL,
                    symbol TEXT NOT NULL, interval TEXT NOT NULL,
                    quantity INTEGER NOT NULL, mode TEXT NOT NULL,
                    broker_name TEXT, account_id TEXT, status TEXT NOT NULL,
                    recovery_issue TEXT, recovery_checked_at TEXT,
                    last_evaluated_bar TEXT, last_decision TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    CHECK(strategy_kind IN ('atomic', 'composite')),
                    CHECK(mode IN ('observe', 'paper_auto', 'live_shadow')),
                    CHECK(status IN (
                        'active', 'paused', 'armed', 'recovery_locked', 'stopped'
                    )),
                    CHECK((mode='live_shadow' AND broker_name IS NOT NULL AND account_id IS NOT NULL)
                       OR (mode!='live_shadow' AND broker_name IS NULL AND account_id IS NULL))
                );
                INSERT INTO strategy_runtimes(
                    runtime_id,owner_user_id,strategy_kind,strategy_id,
                    strategy_version,strategy_snapshot_json,symbol,interval,
                    quantity,mode,broker_name,account_id,status,recovery_issue,
                    recovery_checked_at,last_evaluated_bar,last_decision,
                    created_at,updated_at
                ) SELECT runtime_id,owner_user_id,strategy_kind,strategy_id,
                    strategy_version,strategy_snapshot_json,symbol,interval,
                    quantity,mode,NULL,NULL,status,recovery_issue,
                    recovery_checked_at,last_evaluated_bar,last_decision,
                    created_at,updated_at FROM strategy_runtimes_shadow_legacy;
                CREATE TABLE trading_decisions (
                    decision_id TEXT PRIMARY KEY, runtime_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
                    strategy_version INTEGER, symbol TEXT NOT NULL,
                    contract TEXT NOT NULL, interval TEXT NOT NULL,
                    trigger_time TEXT NOT NULL, direction TEXT NOT NULL,
                    action TEXT NOT NULL, reason TEXT NOT NULL,
                    context_json TEXT NOT NULL, source_bar_time TEXT NOT NULL,
                    source_bar_id TEXT,
                    execution_status TEXT NOT NULL DEFAULT 'not_applicable',
                    execution_reason TEXT, order_id TEXT, reference_price REAL,
                    planned_stop_price REAL, actual_fill_price REAL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(runtime_id) REFERENCES strategy_runtimes(runtime_id)
                        ON DELETE CASCADE,
                    CHECK(direction IN ('long', 'short')),
                    CHECK(action IN ('entry', 'exit', 'none'))
                );
                INSERT INTO trading_decisions SELECT * FROM trading_decisions_shadow_legacy;
                DROP TABLE trading_decisions_shadow_legacy;
                DROP TABLE strategy_runtimes_shadow_legacy;
                CREATE INDEX idx_strategy_runtimes_owner_updated
                    ON strategy_runtimes(owner_user_id, updated_at DESC);
                CREATE INDEX idx_strategy_runtimes_active_symbol
                    ON strategy_runtimes(status, symbol);
                CREATE INDEX idx_trading_decisions_runtime_time
                    ON trading_decisions(runtime_id, trigger_time DESC);
                """
            )
        finally:
            self.connection.execute("PRAGMA foreign_keys=ON")

    def _upgrade_paper_auto_recovery_schema(self) -> None:
        """Add the durable Paper Auto recovery state without losing PR #99 data."""
        self.connection.execute("PRAGMA foreign_keys=OFF")
        try:
            self.connection.executescript(
                """
                ALTER TABLE trading_decisions RENAME TO trading_decisions_recovery_legacy;
                ALTER TABLE strategy_runtimes RENAME TO strategy_runtimes_recovery_legacy;
                CREATE TABLE strategy_runtimes (
                    runtime_id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL,
                    strategy_kind TEXT NOT NULL, strategy_id TEXT NOT NULL,
                    strategy_version INTEGER, strategy_snapshot_json TEXT NOT NULL,
                    symbol TEXT NOT NULL, interval TEXT NOT NULL,
                    quantity INTEGER NOT NULL, mode TEXT NOT NULL,
                    status TEXT NOT NULL, recovery_issue TEXT,
                    recovery_checked_at TEXT, last_evaluated_bar TEXT,
                    last_decision TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(strategy_kind IN ('atomic', 'composite')),
                    CHECK(mode IN ('observe', 'paper_auto')),
                    CHECK(status IN (
                        'active', 'paused', 'armed', 'recovery_locked', 'stopped'
                    ))
                );
                INSERT INTO strategy_runtimes(
                    runtime_id,owner_user_id,strategy_kind,strategy_id,
                    strategy_version,strategy_snapshot_json,symbol,interval,
                    quantity,mode,status,last_evaluated_bar,last_decision,
                    created_at,updated_at
                ) SELECT runtime_id,owner_user_id,strategy_kind,strategy_id,
                    strategy_version,strategy_snapshot_json,symbol,interval,
                    quantity,mode,status,last_evaluated_bar,last_decision,
                    created_at,updated_at
                    FROM strategy_runtimes_recovery_legacy;
                CREATE TABLE trading_decisions (
                    decision_id TEXT PRIMARY KEY, runtime_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
                    strategy_version INTEGER, symbol TEXT NOT NULL,
                    contract TEXT NOT NULL, interval TEXT NOT NULL,
                    trigger_time TEXT NOT NULL, direction TEXT NOT NULL,
                    action TEXT NOT NULL, reason TEXT NOT NULL,
                    context_json TEXT NOT NULL, source_bar_time TEXT NOT NULL,
                    source_bar_id TEXT,
                    execution_status TEXT NOT NULL DEFAULT 'not_applicable',
                    execution_reason TEXT, order_id TEXT, reference_price REAL,
                    planned_stop_price REAL, actual_fill_price REAL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(runtime_id) REFERENCES strategy_runtimes(runtime_id)
                        ON DELETE CASCADE,
                    CHECK(direction IN ('long', 'short')),
                    CHECK(action IN ('entry', 'exit', 'none'))
                );
                INSERT INTO trading_decisions(
                    decision_id,runtime_id,owner_user_id,strategy_id,
                    strategy_version,symbol,contract,interval,trigger_time,
                    direction,action,reason,context_json,source_bar_time,
                    execution_status,execution_reason,order_id,reference_price,
                    planned_stop_price,actual_fill_price,created_at
                ) SELECT decision_id,runtime_id,owner_user_id,strategy_id,
                    strategy_version,symbol,contract,interval,trigger_time,
                    direction,action,reason,context_json,source_bar_time,
                    execution_status,execution_reason,order_id,reference_price,
                    planned_stop_price,actual_fill_price,created_at
                    FROM trading_decisions_recovery_legacy;
                DROP TABLE trading_decisions_recovery_legacy;
                DROP TABLE strategy_runtimes_recovery_legacy;
                CREATE INDEX idx_strategy_runtimes_owner_updated
                    ON strategy_runtimes(owner_user_id, updated_at DESC);
                CREATE INDEX idx_strategy_runtimes_active_symbol
                    ON strategy_runtimes(status, symbol);
                CREATE INDEX idx_trading_decisions_runtime_time
                    ON trading_decisions(runtime_id, trigger_time DESC);
                """
            )
        finally:
            self.connection.execute("PRAGMA foreign_keys=ON")

    def _upgrade_trading_runtime_schema(self) -> None:
        """Upgrade the PR #98 runtime tables without an external migration."""
        self.connection.execute("PRAGMA foreign_keys=OFF")
        try:
            self.connection.executescript(
                """
                ALTER TABLE trading_decisions RENAME TO trading_decisions_legacy;
                ALTER TABLE strategy_runtimes RENAME TO strategy_runtimes_legacy;
                CREATE TABLE strategy_runtimes (
                    runtime_id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL,
                    strategy_kind TEXT NOT NULL, strategy_id TEXT NOT NULL,
                    strategy_version INTEGER, strategy_snapshot_json TEXT NOT NULL,
                    symbol TEXT NOT NULL, interval TEXT NOT NULL,
                    quantity INTEGER NOT NULL, mode TEXT NOT NULL,
                    status TEXT NOT NULL, last_evaluated_bar TEXT,
                    last_decision TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(strategy_kind IN ('atomic', 'composite')),
                    CHECK(mode IN ('observe', 'paper_auto')),
                    CHECK(status IN ('active', 'paused', 'armed', 'stopped'))
                );
                INSERT INTO strategy_runtimes SELECT * FROM strategy_runtimes_legacy;
                CREATE TABLE trading_decisions (
                    decision_id TEXT PRIMARY KEY, runtime_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
                    strategy_version INTEGER, symbol TEXT NOT NULL,
                    contract TEXT NOT NULL, interval TEXT NOT NULL,
                    trigger_time TEXT NOT NULL, direction TEXT NOT NULL,
                    action TEXT NOT NULL, reason TEXT NOT NULL,
                    context_json TEXT NOT NULL, source_bar_time TEXT NOT NULL,
                    execution_status TEXT NOT NULL DEFAULT 'not_applicable',
                    execution_reason TEXT, order_id TEXT, reference_price REAL,
                    planned_stop_price REAL, actual_fill_price REAL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(runtime_id) REFERENCES strategy_runtimes(runtime_id)
                        ON DELETE CASCADE,
                    CHECK(direction IN ('long', 'short')),
                    CHECK(action IN ('entry', 'exit', 'none'))
                );
                INSERT INTO trading_decisions(
                    decision_id,runtime_id,owner_user_id,strategy_id,
                    strategy_version,symbol,contract,interval,trigger_time,
                    direction,action,reason,context_json,source_bar_time,created_at
                ) SELECT decision_id,runtime_id,owner_user_id,strategy_id,
                    strategy_version,symbol,contract,interval,trigger_time,
                    direction,action,reason,context_json,source_bar_time,created_at
                    FROM trading_decisions_legacy;
                DROP TABLE trading_decisions_legacy;
                DROP TABLE strategy_runtimes_legacy;
                CREATE INDEX idx_strategy_runtimes_owner_updated
                    ON strategy_runtimes(owner_user_id, updated_at DESC);
                CREATE INDEX idx_strategy_runtimes_active_symbol
                    ON strategy_runtimes(status, symbol);
                CREATE INDEX idx_trading_decisions_runtime_time
                    ON trading_decisions(runtime_id, trigger_time DESC);
                """
            )
        finally:
            self.connection.execute("PRAGMA foreign_keys=ON")

    def claim_legacy_ownership(self, owner_user_id: str) -> None:
        if not owner_user_id or owner_user_id == DEFAULT_OWNER_ID:
            raise ValueError("a real owner user id is required")
        with self.lock:
            for table in (
                "strategy_parameters",
                "composite_strategies",
                "archived_composite_strategies",
                "backtest_runs",
                "composite_strategy_dependencies",
                "strategy_runtimes",
                "trading_decisions",
            ):
                self.connection.execute(
                    f"UPDATE {table} SET owner_user_id=? WHERE owner_user_id=?",
                    (owner_user_id, DEFAULT_OWNER_ID),
                )
            self.connection.commit()

    def _runtime_row(self, row: sqlite3.Row) -> dict[str, object]:
        return {
            "runtime_id": row["runtime_id"],
            "owner_user_id": row["owner_user_id"],
            "strategy_kind": row["strategy_kind"],
            "strategy_id": row["strategy_id"],
            "strategy_version": row["strategy_version"],
            "strategy_snapshot": json.loads(row["strategy_snapshot_json"]),
            "strategy_lineage": self._snapshot_lineage_locked(
                "runtime", row["runtime_id"]
            ),
            "symbol": row["symbol"],
            "interval": row["interval"],
            "quantity": row["quantity"],
            "mode": row["mode"],
            "broker_name": row["broker_name"],
            "account_id": row["account_id"],
            "execution_target_id": row["execution_target_id"],
            "status": row["status"],
            "recovery_issue": row["recovery_issue"],
            "recovery_checked_at": row["recovery_checked_at"],
            "last_evaluated_bar": row["last_evaluated_bar"],
            "last_decision": row["last_decision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _decision_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "decision_id": row["decision_id"],
            "runtime_id": row["runtime_id"],
            "owner_user_id": row["owner_user_id"],
            "strategy_id": row["strategy_id"],
            "strategy_version": row["strategy_version"],
            "symbol": row["symbol"],
            "contract": row["contract"],
            "interval": row["interval"],
            "trigger_time": row["trigger_time"],
            "direction": row["direction"],
            "action": row["action"],
            "reason": row["reason"],
            "context": json.loads(row["context_json"]),
            "source_bar_time": row["source_bar_time"],
            "source_bar_id": row["source_bar_id"],
            "execution_status": row["execution_status"],
            "execution_reason": row["execution_reason"],
            "order_id": row["order_id"],
            "reference_price": row["reference_price"],
            "planned_stop_price": row["planned_stop_price"],
            "actual_fill_price": row["actual_fill_price"],
            "created_at": row["created_at"],
        }

    def create_trading_runtime(
        self, runtime: dict[str, object]
    ) -> dict[str, object]:
        runtime_id = uuid4().hex
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    "INSERT INTO strategy_runtimes("
                    "runtime_id,owner_user_id,strategy_kind,strategy_id,"
                    "strategy_version,strategy_snapshot_json,symbol,interval,"
                    "quantity,mode,broker_name,account_id,execution_target_id,status,last_evaluated_bar,last_decision,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        runtime_id, runtime["owner_user_id"],
                        runtime["strategy_kind"], runtime["strategy_id"],
                        runtime.get("strategy_version"),
                        json.dumps(
                            runtime["strategy_snapshot"], ensure_ascii=False,
                            sort_keys=True,
                        ),
                        runtime["symbol"], runtime["interval"],
                        runtime["quantity"], runtime["mode"],
                        runtime.get("broker_name"), runtime.get("account_id"),
                        runtime.get("execution_target_id"),
                        "active" if runtime["mode"] in {"observe", "live_shadow"} else "paused",
                        runtime.get("last_evaluated_bar"), None, now, now,
                    ),
                )
                self._insert_snapshot_lineage_locked(
                    "runtime",
                    runtime_id,
                    str(runtime["owner_user_id"]),
                    runtime.get("strategy_lineage"),
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            row = self.connection.execute(
                "SELECT * FROM strategy_runtimes WHERE runtime_id=?",
                (runtime_id,),
            ).fetchone()
        return self._runtime_row(row)

    def trading_runtimes(
        self, owner_user_id: str
    ) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM strategy_runtimes WHERE owner_user_id=? "
                "ORDER BY created_at DESC", (owner_user_id,)
            ).fetchall()
        return [self._runtime_row(row) for row in rows]

    def trading_runtime(
        self, runtime_id: str, owner_user_id: str
    ) -> dict[str, object] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM strategy_runtimes WHERE runtime_id=? "
                "AND owner_user_id=?", (runtime_id, owner_user_id)
            ).fetchone()
        return self._runtime_row(row) if row else None

    def active_trading_runtimes(
        self, symbol: str
    ) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM strategy_runtimes WHERE symbol=? AND ("
                "(status='active' AND mode='observe') OR "
                "(status='active' AND mode='live_shadow') OR "
                "(status IN ('armed','paused','recovery_locked') AND mode='live_auto') OR "
                "(status IN ('armed','paused','recovery_locked') "
                "AND mode='paper_auto')) ORDER BY created_at",
                (symbol,),
            ).fetchall()
        return [self._runtime_row(row) for row in rows]

    def disarm_live_auto_runtimes_for_restart(self, symbol: str) -> int:
        """ARM is process-scoped; durable definitions always restart paused."""
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.lock:
            cursor = self.connection.execute(
                "UPDATE strategy_runtimes SET status='recovery_locked',updated_at=? "
                "WHERE symbol=? AND mode='live_auto' AND status!='stopped'",
                (now, symbol),
            )
            self.connection.commit()
            return cursor.rowcount

    def paper_auto_runtimes(
        self, symbol: str
    ) -> list[dict[str, object]]:
        """Return Paper Auto snapshots, including paused/stopped positions."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM strategy_runtimes "
                "WHERE symbol=? AND mode='paper_auto' ORDER BY created_at",
                (symbol,),
            ).fetchall()
        return [self._runtime_row(row) for row in rows]

    def lock_paper_auto_runtimes_for_recovery(
        self, symbol: str
    ) -> list[dict[str, object]]:
        """Durably disarm runtimes that could create exposure after restart."""
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.lock:
            self.connection.execute(
                "UPDATE strategy_runtimes SET status='recovery_locked',"
                "recovery_issue=NULL,recovery_checked_at=NULL,updated_at=? "
                "WHERE symbol=? AND mode='paper_auto' "
                "AND status IN ('armed','recovery_locked')",
                (now, symbol),
            )
            self.connection.commit()
            rows = self.connection.execute(
                "SELECT * FROM strategy_runtimes WHERE symbol=? "
                "AND mode='paper_auto' ORDER BY created_at",
                (symbol,),
            ).fetchall()
        return [self._runtime_row(row) for row in rows]

    def set_trading_runtime_recovery(
        self,
        runtime_id: str,
        owner_user_id: str,
        issue: str | None,
    ) -> dict[str, object] | None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.lock:
            self.connection.execute(
                "UPDATE strategy_runtimes SET recovery_issue=?,"
                "recovery_checked_at=?,updated_at=? "
                "WHERE runtime_id=? AND owner_user_id=?",
                (issue, now, now, runtime_id, owner_user_id),
            )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT * FROM strategy_runtimes WHERE runtime_id=? "
                "AND owner_user_id=?", (runtime_id, owner_user_id),
            ).fetchone()
        return self._runtime_row(row) if row else None

    def stop_trading_runtime(
        self, runtime_id: str, owner_user_id: str
    ) -> dict[str, object] | None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.lock:
            self.connection.execute(
                "UPDATE strategy_runtimes SET status='stopped',updated_at=? "
                "WHERE runtime_id=? AND owner_user_id=?",
                (now, runtime_id, owner_user_id),
            )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT * FROM strategy_runtimes WHERE runtime_id=? "
                "AND owner_user_id=?", (runtime_id, owner_user_id)
            ).fetchone()
        return self._runtime_row(row) if row else None

    def set_trading_runtime_status(
        self,
        runtime_id: str,
        owner_user_id: str,
        status: str,
        last_evaluated_bar: str | None = None,
    ) -> dict[str, object] | None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.lock:
            if last_evaluated_bar is None:
                self.connection.execute(
                    "UPDATE strategy_runtimes SET status=?,updated_at=? "
                    "WHERE runtime_id=? AND owner_user_id=?",
                    (status, now, runtime_id, owner_user_id),
                )
            else:
                self.connection.execute(
                    "UPDATE strategy_runtimes SET status=?,last_evaluated_bar=?,"
                    "updated_at=? WHERE runtime_id=? AND owner_user_id=?",
                    (status, last_evaluated_bar, now, runtime_id, owner_user_id),
                )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT * FROM strategy_runtimes WHERE runtime_id=? "
                "AND owner_user_id=?", (runtime_id, owner_user_id)
            ).fetchone()
        return self._runtime_row(row) if row else None

    def trading_decisions(
        self, runtime_id: str, owner_user_id: str, limit: int
    ) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM trading_decisions WHERE runtime_id=? "
                "AND owner_user_id=? ORDER BY trigger_time DESC LIMIT ?",
                (runtime_id, owner_user_id, limit),
            ).fetchall()
        return [self._decision_row(row) for row in rows]

    def trading_decision(
        self, decision_id: str, owner_user_id: str
    ) -> dict[str, object] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM trading_decisions WHERE decision_id=? "
                "AND owner_user_id=?", (decision_id, owner_user_id),
            ).fetchone()
        return self._decision_row(row) if row else None

    def record_runtime_evaluation(
        self,
        runtime_id: str,
        evaluated_bar: str,
        decisions: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        inserted: list[dict[str, object]] = []
        last_decision: str | None = None
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                runtime = self.connection.execute(
                    "SELECT status,last_evaluated_bar FROM strategy_runtimes "
                    "WHERE runtime_id=?", (runtime_id,)
                ).fetchone()
                if (
                    runtime is None
                    or runtime["status"] not in {
                        "active", "armed", "paused", "recovery_locked"
                    }
                    or (
                        runtime["last_evaluated_bar"] is not None
                        and evaluated_bar <= runtime["last_evaluated_bar"]
                    )
                ):
                    self.duplicate_decisions_blocked += len(decisions)
                    self.connection.rollback()
                    return []
                for decision in decisions:
                    cursor = self.connection.execute(
                        "INSERT OR IGNORE INTO trading_decisions("
                        "decision_id,runtime_id,owner_user_id,strategy_id,"
                        "strategy_version,symbol,contract,interval,trigger_time,"
                        "direction,action,reason,context_json,source_bar_time,"
                        "source_bar_id,execution_status,execution_reason,"
                        "reference_price,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            decision["decision_id"], runtime_id,
                            decision["owner_user_id"], decision["strategy_id"],
                            decision.get("strategy_version"), decision["symbol"],
                            decision["contract"], decision["interval"],
                            decision["trigger_time"], decision["direction"],
                            decision["action"], decision["reason"],
                            json.dumps(
                                decision.get("context", {}),
                                ensure_ascii=False, sort_keys=True,
                            ),
                            decision["source_bar_time"],
                            decision.get("source_bar_id"),
                            decision.get("execution_status", "not_applicable"),
                            decision.get("execution_reason"),
                            decision.get("reference_price"),
                            now,
                        ),
                    )
                    if cursor.rowcount:
                        inserted.append(decision)
                        last_decision = str(decision["decision_id"])
                    else:
                        self.duplicate_decisions_blocked += 1
                if last_decision is None:
                    previous = self.connection.execute(
                        "SELECT last_decision FROM strategy_runtimes "
                        "WHERE runtime_id=?", (runtime_id,)
                    ).fetchone()
                    last_decision = previous["last_decision"]
                self.connection.execute(
                    "UPDATE strategy_runtimes SET last_evaluated_bar=?,"
                    "last_decision=?,updated_at=? WHERE runtime_id=?",
                    (evaluated_bar, last_decision, now, runtime_id),
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return inserted

    def runtime_metrics(self) -> dict[str, object]:
        with self.lock:
            runtimes = self.connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(status='active') AS active, "
                "SUM(status='armed') AS armed, "
                "SUM(status='recovery_locked') AS recovery_locked "
                "FROM strategy_runtimes"
            ).fetchone()
            decisions = self.connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(execution_status='filled') AS executed, "
                "SUM(execution_status='skipped') AS skipped, "
                "MAX(trigger_time) AS last_decision_time "
                "FROM trading_decisions"
            ).fetchone()
        assert runtimes is not None and decisions is not None
        return {
            "runtimes": int(runtimes["total"] or 0),
            "active_runtimes": int(runtimes["active"] or 0),
            "armed_runtimes": int(runtimes["armed"] or 0),
            "recovery_locked_runtimes": int(runtimes["recovery_locked"] or 0),
            "decisions": int(decisions["total"] or 0),
            "executed_decisions": int(decisions["executed"] or 0),
            "skipped_decisions": int(decisions["skipped"] or 0),
            "last_runtime_decision_time": decisions["last_decision_time"],
            "duplicate_decisions_blocked": self.duplicate_decisions_blocked,
        }

    def paper_shadow_metrics(self, owner_user_id: str) -> dict[str, int]:
        """Compare equivalent decisions without assuming equal execution prices."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT paper.action AS paper_action,paper.direction AS paper_direction,"
                "shadow.action AS shadow_action,shadow.direction AS shadow_direction "
                "FROM trading_decisions paper "
                "JOIN strategy_runtimes paper_runtime ON paper_runtime.runtime_id=paper.runtime_id "
                "JOIN trading_decisions shadow ON shadow.source_bar_id=paper.source_bar_id "
                "JOIN strategy_runtimes shadow_runtime ON shadow_runtime.runtime_id=shadow.runtime_id "
                "WHERE paper.owner_user_id=? AND shadow.owner_user_id=? "
                "AND paper_runtime.mode='paper_auto' AND shadow_runtime.mode='live_shadow' "
                "AND paper.strategy_id=shadow.strategy_id AND paper.source_bar_id IS NOT NULL",
                (owner_user_id, owner_user_id),
            ).fetchall()
        return {
            "paper_shadow_direction_match_total": sum(row["paper_direction"] == row["shadow_direction"] for row in rows),
            "paper_shadow_direction_mismatch_total": sum(row["paper_direction"] != row["shadow_direction"] for row in rows),
            "paper_shadow_action_match_total": sum(row["paper_action"] == row["shadow_action"] for row in rows),
            "paper_shadow_action_mismatch_total": sum(row["paper_action"] != row["shadow_action"] for row in rows),
        }

    def update_decision_execution(
        self, decision_id: str, owner_user_id: str, values: dict[str, object]
    ) -> dict[str, object] | None:
        allowed = {
            "execution_status", "execution_reason", "order_id",
            "reference_price", "planned_stop_price", "actual_fill_price",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            raise ValueError("at least one execution field is required")
        assignments = ",".join(f"{key}=?" for key in updates)
        with self.lock:
            self.connection.execute(
                f"UPDATE trading_decisions SET {assignments} "
                "WHERE decision_id=? AND owner_user_id=?",
                (*updates.values(), decision_id, owner_user_id),
            )
            self.connection.commit()
            row = self.connection.execute(
                "SELECT * FROM trading_decisions WHERE decision_id=? "
                "AND owner_user_id=?", (decision_id, owner_user_id)
            ).fetchone()
        return self._decision_row(row) if row else None

    def auto_decisions(
        self, symbol: str, statuses: tuple[str, ...]
    ) -> list[dict[str, object]]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self.lock:
            rows = self.connection.execute(
                "SELECT decision.* FROM trading_decisions AS decision "
                "JOIN strategy_runtimes AS runtime "
                "ON runtime.runtime_id=decision.runtime_id "
                "WHERE decision.symbol=? AND decision.action IN ('entry','exit') "
                "AND runtime.mode='paper_auto' "
                f"AND decision.execution_status IN ({placeholders}) "
                "ORDER BY decision.trigger_time",
                (symbol, *statuses),
            ).fetchall()
        return [self._decision_row(row) for row in rows]

    def auto_entry_decisions(
        self, symbol: str, statuses: tuple[str, ...]
    ) -> list[dict[str, object]]:
        """Compatibility query retained for PR #99 callers."""
        return [
            item for item in self.auto_decisions(symbol, statuses)
            if item["action"] == "entry"
        ]

    def save(self, bar: KBar) -> None:
        values = (
            bar.symbol, bar.contract, bar.time.isoformat(), bar.open, bar.high,
            bar.low, bar.close, bar.volume, bar.status, bar.session,
            bar.trading_date.isoformat(), bar.first_tick_time.isoformat(),
            bar.last_tick_time.isoformat(), bar.exchange_time.isoformat(),
            bar.received_time.isoformat(), bar.latency_ms, int(bar.no_trade),
        )
        with self.lock:
            self.connection.execute(
                """
                INSERT INTO minute_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, contract, time) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume,
                    status=excluded.status, session=excluded.session,
                    trading_date=excluded.trading_date,
                    first_tick_time=excluded.first_tick_time,
                    last_tick_time=excluded.last_tick_time,
                    exchange_time=excluded.exchange_time,
                    received_time=excluded.received_time,
                    latency_ms=excluded.latency_ms,
                    no_trade=excluded.no_trade
                """,
                values,
            )
            self.connection.commit()

    @staticmethod
    def _to_bar(row: sqlite3.Row) -> KBar:
        return KBar(
            symbol=row["symbol"], contract=row["contract"],
            time=datetime.fromisoformat(row["time"]), open=row["open"],
            high=row["high"], low=row["low"], close=row["close"],
            volume=row["volume"], status=row["status"], session=row["session"],
            trading_date=date.fromisoformat(row["trading_date"]),
            first_tick_time=datetime.fromisoformat(row["first_tick_time"]),
            last_tick_time=datetime.fromisoformat(row["last_tick_time"]),
            exchange_time=datetime.fromisoformat(row["exchange_time"]),
            received_time=datetime.fromisoformat(row["received_time"]),
            latency_ms=row["latency_ms"], no_trade=bool(row["no_trade"]),
        )

    def latest(self, symbol: str, limit: int) -> list[KBar]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM minute_bars WHERE symbol=? ORDER BY time DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        return [self._to_bar(row) for row in reversed(rows)]

    def latest_forming(self, symbol: str) -> KBar | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM minute_bars WHERE symbol=? AND status='forming' "
                "ORDER BY time DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        return self._to_bar(row) if row else None

    def date_bounds(self, symbol: str) -> tuple[date | None, date | None]:
        with self.lock:
            row = self.connection.execute(
                "SELECT MIN(trading_date) AS first_date, "
                "MAX(trading_date) AS last_date FROM minute_bars "
                "WHERE symbol=? AND status='closed'",
                (symbol,),
            ).fetchone()
        first = date.fromisoformat(row["first_date"]) if row["first_date"] else None
        last = date.fromisoformat(row["last_date"]) if row["last_date"] else None
        return first, last

    def replay_availability(self, symbol: str) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT trading_date, session, COUNT(*) AS bar_count "
                "FROM minute_bars WHERE symbol=? AND status='closed' "
                "GROUP BY trading_date, session ORDER BY trading_date, session",
                (symbol,),
            ).fetchall()
        dates: dict[str, dict[str, object]] = {}
        for row in rows:
            item = dates.setdefault(
                row["trading_date"],
                {"date": row["trading_date"], "sessions": []},
            )
            item["sessions"].append({
                "key": row["session"], "bar_count": row["bar_count"]
            })
        return list(dates.values())

    def between_trading_dates(
        self, symbol: str, start: date, end: date
    ) -> list[KBar]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM minute_bars WHERE symbol=? AND status='closed' "
                "AND trading_date BETWEEN ? AND ? ORDER BY trading_date, time",
                (symbol, start.isoformat(), end.isoformat()),
            ).fetchall()
        return [self._to_bar(row) for row in rows]

    def tick_seen(self, key: str) -> bool:
        with self.lock:
            row = self.connection.execute(
                "SELECT 1 FROM processed_ticks WHERE dedup_key=?", (key,)
            ).fetchone()
        return row is not None

    def remember_tick(self, key: str, exchange_time: datetime) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT OR IGNORE INTO processed_ticks VALUES (?,?)",
                (key, exchange_time.isoformat()),
            )
            self.connection.commit()

    def purge_backfill(self, symbol: str, contract: str) -> None:
        """Remove only bars generated by the historical backfill adapter.

        Backfilled bars use exact minute boundaries, a synthetic 59.999 second
        last-tick marker, zero latency, and closed status. Tick-aggregated bars
        retain their real first/last Tick timestamps and are not matched.
        """
        with self.lock:
            rows = self.connection.execute(
                "SELECT time, first_tick_time, last_tick_time FROM minute_bars "
                "WHERE symbol=? AND contract=? AND status='closed' "
                "AND latency_ms=0",
                (symbol, contract),
            ).fetchall()
            keys = []
            for row in rows:
                bar_time = datetime.fromisoformat(row["time"])
                first = datetime.fromisoformat(row["first_tick_time"])
                last = datetime.fromisoformat(row["last_tick_time"])
                if first == bar_time and last == bar_time.replace(
                    second=59, microsecond=999000
                ):
                    keys.append((symbol, contract, row["time"]))
            self.connection.executemany(
                "DELETE FROM minute_bars WHERE symbol=? AND contract=? AND time=?",
                keys,
            )
            self.connection.commit()

    @staticmethod
    def _owner(owner_user_id: str | None) -> str:
        return owner_user_id or DEFAULT_OWNER_ID

    def strategy_parameters(
        self, owner_user_id: str | None = None
    ) -> dict[str, dict[str, object]]:
        owner = self._owner(owner_user_id)
        with self.lock:
            rows = self.connection.execute(
                "SELECT strategy, parameters_json FROM strategy_parameters "
                "WHERE owner_user_id=?",
                (owner,),
            ).fetchall()
        return {
            row["strategy"]: json.loads(row["parameters_json"])
            for row in rows
        }

    def save_strategy_parameters(
        self,
        strategy: str,
        parameters: dict[str, int | float],
        owner_user_id: str | None = None,
        plugin_identity: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        owner = self._owner(owner_user_id)
        payload = json.dumps(parameters, ensure_ascii=False, sort_keys=True)
        updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                version = (
                    self._insert_strategy_version_locked(
                        owner, strategy, parameters, plugin_identity
                    )
                    if plugin_identity is not None else None
                )
                self.connection.execute(
                    """
                    INSERT INTO strategy_parameters(
                        owner_user_id, strategy, parameters_json, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(owner_user_id, strategy) DO UPDATE SET
                        parameters_json=excluded.parameters_json,
                        updated_at=excluded.updated_at
                    """,
                    (owner, strategy, payload, updated_at),
                )
                self.connection.commit()
                return version
            except Exception:
                self.connection.rollback()
                raise

    @staticmethod
    def _composite_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "id": row["strategy_id"],
            "version": row["version"],
            "name": row["name"],
            "definition": json.loads(row["definition_json"]),
            "created_at": row["created_at"],
        }

    def composite_strategies(
        self, owner_user_id: str | None = None
    ) -> list[dict[str, object]]:
        owner = self._owner(owner_user_id)
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT item.* FROM composite_strategies item
                JOIN (
                    SELECT strategy_id, MAX(version) AS version
                    FROM composite_strategies WHERE owner_user_id=?
                    GROUP BY strategy_id
                ) latest ON latest.strategy_id=item.strategy_id
                    AND latest.version=item.version
                WHERE item.owner_user_id=?
                AND NOT EXISTS (
                    SELECT 1 FROM archived_composite_strategies archived
                    WHERE archived.strategy_id=item.strategy_id
                    AND archived.owner_user_id=item.owner_user_id
                )
                ORDER BY item.created_at DESC
                """,
                (owner, owner),
            ).fetchall()
        return [self._composite_row(row) for row in rows]

    def archived_composite_strategies(
        self, owner_user_id: str | None = None
    ) -> list[dict[str, object]]:
        owner = self._owner(owner_user_id)
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT item.*, archived.archived_at
                FROM archived_composite_strategies archived
                JOIN composite_strategies item
                  ON item.strategy_id=archived.strategy_id
                  AND item.owner_user_id=archived.owner_user_id
                JOIN (
                    SELECT strategy_id, MAX(version) AS version
                    FROM composite_strategies WHERE owner_user_id=?
                    GROUP BY strategy_id
                ) latest ON latest.strategy_id=item.strategy_id
                    AND latest.version=item.version
                WHERE archived.owner_user_id=?
                ORDER BY archived.archived_at DESC
                """,
                (owner, owner),
            ).fetchall()
        return [
            {**self._composite_row(row), "archived_at": row["archived_at"]}
            for row in rows
        ]

    def composite_strategy_versions(
        self, strategy_id: str, owner_user_id: str | None = None
    ) -> list[dict[str, object]]:
        owner = self._owner(owner_user_id)
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM composite_strategies WHERE strategy_id=? "
                "AND owner_user_id=? "
                "ORDER BY version DESC",
                (strategy_id, owner),
            ).fetchall()
        return [self._composite_row(row) for row in rows]

    def composite_strategy(
        self,
        strategy_id: str,
        version: int | None = None,
        owner_user_id: str | None = None,
    ) -> dict[str, object] | None:
        owner = self._owner(owner_user_id)
        with self.lock:
            if version is None:
                row = self.connection.execute(
                    "SELECT * FROM composite_strategies WHERE strategy_id=? "
                    "AND owner_user_id=? "
                    "ORDER BY version DESC LIMIT 1",
                    (strategy_id, owner),
                ).fetchone()
            else:
                row = self.connection.execute(
                    "SELECT * FROM composite_strategies "
                    "WHERE strategy_id=? AND version=? AND owner_user_id=?",
                    (strategy_id, version, owner),
                ).fetchone()
        return self._composite_row(row) if row else None

    def save_composite_strategy(
        self,
        strategy_id: str,
        definition: dict[str, object],
        owner_user_id: str | None = None,
        plugin_identity: dict[str, object] | None = None,
    ) -> dict[str, object]:
        owner = self._owner(owner_user_id)
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        payload = json.dumps(definition, ensure_ascii=False, sort_keys=True)
        with self.lock:
            archived = self.connection.execute(
                "SELECT 1 FROM archived_composite_strategies WHERE strategy_id=? "
                "AND owner_user_id=?",
                (strategy_id, owner),
            ).fetchone()
            if archived:
                raise ValueError("已封存的組合策略不可修改")
            latest = self.connection.execute(
                "SELECT name FROM composite_strategies WHERE strategy_id=? "
                "AND owner_user_id=? ORDER BY version DESC LIMIT 1",
                (strategy_id, owner),
            ).fetchone()
            if latest is not None and latest["name"] != definition["name"]:
                raise ValueError("變更策略名稱必須另存為新策略")
            names = self.connection.execute(
                """
                SELECT item.strategy_id, item.name
                FROM composite_strategies item
                JOIN (
                    SELECT strategy_id, MAX(version) AS version
                    FROM composite_strategies WHERE owner_user_id=?
                    GROUP BY strategy_id
                ) latest ON latest.strategy_id=item.strategy_id
                    AND latest.version=item.version
                WHERE item.owner_user_id=? AND item.strategy_id<>?
                """,
                (owner, owner, strategy_id),
            ).fetchall()
            wanted_name = str(definition["name"]).casefold()
            if any(str(item["name"]).casefold() == wanted_name for item in names):
                raise StrategyNameConflictError(
                    f"策略名稱「{definition['name']}」已存在（包含封存策略）"
                )
            row = self.connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version "
                "FROM composite_strategies WHERE strategy_id=? AND owner_user_id=?",
                (strategy_id, owner),
            ).fetchone()
            version = int(row["version"]) + 1
            dependencies: set[tuple[str, int]] = set()
            for role in ("setup", "entry", "exit"):
                group = definition.get(role, {})
                if not isinstance(group, dict):
                    continue
                for rule in group.get("rules", []):
                    if isinstance(rule, dict) and rule.get("source") == "composite":
                        dependencies.add((
                            str(rule["strategy_id"]), int(rule["version"])
                        ))
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    "INSERT INTO composite_strategies("
                    "strategy_id,version,name,definition_json,created_at,"
                    "owner_user_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        strategy_id, version, definition["name"], payload,
                        created_at, owner,
                    ),
                )
                self.connection.executemany(
                    "INSERT INTO composite_strategy_dependencies("
                    "owner_user_id,parent_strategy_id,parent_version,"
                    "child_strategy_id,child_version) VALUES (?,?,?,?,?)",
                    [
                        (owner, strategy_id, version, child_id, child_version)
                        for child_id, child_version in dependencies
                    ],
                )
                if plugin_identity is not None:
                    self._insert_composite_lineage_locked(
                        owner, strategy_id, version, plugin_identity, created_at
                    )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return {
            "id": strategy_id,
            "version": version,
            "name": definition["name"],
            "definition": definition,
            "created_at": created_at,
        }

    def composite_strategy_archived(
        self, strategy_id: str, owner_user_id: str | None = None
    ) -> bool:
        owner = self._owner(owner_user_id)
        with self.lock:
            row = self.connection.execute(
                "SELECT 1 FROM archived_composite_strategies WHERE strategy_id=? "
                "AND owner_user_id=?",
                (strategy_id, owner),
            ).fetchone()
        return row is not None

    def archive_composite_strategy(
        self, strategy_id: str, owner_user_id: str | None = None
    ) -> dict[str, object]:
        owner = self._owner(owner_user_id)
        archived_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.lock:
            exists = self.connection.execute(
                "SELECT 1 FROM composite_strategies WHERE strategy_id=? "
                "AND owner_user_id=? LIMIT 1",
                (strategy_id, owner),
            ).fetchone()
            if not exists:
                raise ValueError("找不到組合策略")
            self.connection.execute(
                "INSERT INTO archived_composite_strategies("
                "strategy_id,archived_at,owner_user_id) VALUES (?, ?, ?) "
                "ON CONFLICT(strategy_id) DO UPDATE SET "
                "archived_at=excluded.archived_at, "
                "owner_user_id=excluded.owner_user_id",
                (strategy_id, archived_at, owner),
            )
            self.connection.commit()
        return {"id": strategy_id, "archived_at": archived_at}

    def purge_archived_composite_strategies(
        self, strategy_ids: list[str], owner_user_id: str | None = None
    ) -> dict[str, object]:
        owner = self._owner(owner_user_id)
        ids = list(dict.fromkeys(item.strip() for item in strategy_ids if item.strip()))
        if not ids:
            raise StrategyPurgeError("至少需要選擇一個封存策略")
        placeholders = ",".join("?" for _ in ids)
        with self.lock:
            archived_rows = self.connection.execute(
                f"SELECT strategy_id FROM archived_composite_strategies "
                f"WHERE strategy_id IN ({placeholders}) AND owner_user_id=?",
                [*ids, owner],
            ).fetchall()
            archived = {row["strategy_id"] for row in archived_rows}
            not_archived = [item for item in ids if item not in archived]
            if not_archived:
                raise StrategyPurgeError(
                    "只有封存策略可以永久刪除：" + ", ".join(not_archived)
                )
            reference_rows = self.connection.execute(
                f"SELECT strategy_key, COUNT(*) AS count FROM backtest_runs "
                f"WHERE strategy_kind='composite' "
                f"AND strategy_key IN ({placeholders}) AND owner_user_id=? "
                f"GROUP BY strategy_key",
                [*ids, owner],
            ).fetchall()
            references = {
                row["strategy_key"]: int(row["count"]) for row in reference_rows
            }
            if references:
                raise StrategyReferencedError(references)
            dependency_rows = self.connection.execute(
                f"SELECT child_strategy_id, parent_strategy_id "
                f"FROM composite_strategy_dependencies "
                f"WHERE child_strategy_id IN ({placeholders}) "
                f"AND parent_strategy_id NOT IN ({placeholders}) "
                f"AND owner_user_id=?",
                [*ids, *ids, owner],
            ).fetchall()
            if dependency_rows:
                labels = ", ".join(sorted({
                    f"{row['child_strategy_id']} ← {row['parent_strategy_id']}"
                    for row in dependency_rows
                }))
                raise StrategyDependencyError(
                    f"策略仍被其他組合策略引用，禁止永久刪除：{labels}"
                )
            version_rows = self.connection.execute(
                f"SELECT strategy_id, COUNT(*) AS count FROM composite_strategies "
                f"WHERE strategy_id IN ({placeholders}) AND owner_user_id=? "
                f"GROUP BY strategy_id",
                [*ids, owner],
            ).fetchall()
            version_count = sum(int(row["count"]) for row in version_rows)
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    f"DELETE FROM composite_strategy_dependencies "
                    f"WHERE parent_strategy_id IN ({placeholders}) "
                    f"AND owner_user_id=?",
                    [*ids, owner],
                )
                self.connection.execute(
                    f"DELETE FROM archived_composite_strategies "
                    f"WHERE strategy_id IN ({placeholders}) AND owner_user_id=?",
                    [*ids, owner],
                )
                self.connection.execute(
                    f"DELETE FROM composite_strategies "
                    f"WHERE strategy_id IN ({placeholders}) AND owner_user_id=?",
                    [*ids, owner],
                )
                self.connection.commit()
            except sqlite3.IntegrityError as exc:
                self.connection.rollback()
                raise StrategyReferencedError(
                    {strategy_id: 1 for strategy_id in ids}
                ) from exc
            except Exception:
                self.connection.rollback()
                raise
        return {
            "purged_strategy_ids": ids,
            "deleted_strategies": len(ids),
            "deleted_versions": version_count,
        }

    def _backtest_row(
        self, row: sqlite3.Row, *, detail: bool = False
    ) -> dict[str, object]:
        result = json.loads(row["result_json"]) if detail else None
        summary = (
            result.get("summary", {})
            if result is not None
            else json.loads(row["summary_json"])
        )
        item: dict[str, object] = {
            "run_id": row["run_id"],
            "strategy_kind": row["strategy_kind"],
            "strategy_key": row["strategy_key"],
            "strategy_version": row["strategy_version"],
            "strategy_name": row["strategy_name"],
            "symbol": row["symbol"],
            "interval": row["interval"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "status": row["status"],
            "created_at": row["created_at"],
            "trade_count": (
                len(result.get("trades", []))
                if result is not None
                else row["trade_count"]
            ),
            "summary": summary,
            "strategy_lineage": self._snapshot_lineage_locked(
                "backtest", row["run_id"]
            ),
        }
        if detail:
            item["strategy_snapshot"] = json.loads(row["strategy_snapshot_json"])
            item["result"] = result
        return item

    def save_backtest_run(
        self,
        result: dict[str, object],
        strategy_kind: str,
        strategy_key: str,
        strategy_version: int | None,
        strategy_snapshot: dict[str, object],
        owner_user_id: str | None = None,
        strategy_lineage: dict[str, object] | None = None,
    ) -> dict[str, object]:
        owner = self._owner(owner_user_id)
        if strategy_kind not in {"atomic", "composite"}:
            raise ValueError("unsupported strategy kind")
        metadata = result["metadata"]
        date_range = str(metadata["date_range"]).split(" ～ ")
        if len(date_range) != 2:
            raise ValueError("invalid backtest date range")
        stored_result = {
            key: value for key, value in result.items() if key != "bars"
        }
        run_id = uuid4().hex
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    "INSERT INTO backtest_runs("
                    "run_id,strategy_kind,strategy_key,strategy_version,strategy_name,"
                    "symbol,interval,start_date,end_date,strategy_snapshot_json,"
                    "result_json,summary_json,trade_count,status,created_at,owner_user_id"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id, strategy_kind, strategy_key, strategy_version,
                        metadata["strategy"], metadata["symbol"],
                        metadata.get("interval_key", metadata["interval"]),
                        date_range[0], date_range[1],
                        json.dumps(strategy_snapshot, ensure_ascii=False, sort_keys=True),
                        json.dumps(stored_result, ensure_ascii=False),
                        json.dumps(result.get("summary", {}), ensure_ascii=False),
                        len(result.get("trades", [])),
                        "completed", created_at, owner,
                    ),
                )
                self._insert_snapshot_lineage_locked(
                    "backtest", run_id, owner, strategy_lineage
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            row = self.connection.execute(
                "SELECT * FROM backtest_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._backtest_row(row, detail=True)

    def backtest_runs(
        self,
        limit: int,
        offset: int,
        strategy_key: str | None = None,
        owner_user_id: str | None = None,
        strategy_kind: str | None = None,
        outcome: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, object]]:
        where, parameters = self._backtest_run_filter(
            strategy_key, owner_user_id, strategy_kind, outcome, search
        )
        sql = (
            "SELECT run_id,strategy_kind,strategy_key,strategy_version,"
            "strategy_name,symbol,interval,start_date,end_date,status,created_at,"
            f"summary_json,trade_count FROM backtest_runs WHERE {where}"
        )
        sql += " ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?"
        with self.lock:
            rows = self.connection.execute(sql, [*parameters, limit, offset]).fetchall()
        return [self._backtest_row(row) for row in rows]

    def count_backtest_runs(
        self,
        strategy_key: str | None = None,
        owner_user_id: str | None = None,
        strategy_kind: str | None = None,
        outcome: str | None = None,
        search: str | None = None,
    ) -> int:
        where, parameters = self._backtest_run_filter(
            strategy_key, owner_user_id, strategy_kind, outcome, search
        )
        with self.lock:
            row = self.connection.execute(
                f"SELECT COUNT(*) FROM backtest_runs WHERE {where}", parameters
            ).fetchone()
        return int(row[0])

    def _backtest_run_filter(
        self,
        strategy_key: str | None,
        owner_user_id: str | None,
        strategy_kind: str | None,
        outcome: str | None,
        search: str | None,
    ) -> tuple[str, list[object]]:
        conditions = ["owner_user_id=?"]
        parameters: list[object] = [self._owner(owner_user_id)]
        if strategy_key:
            conditions.append("strategy_key=?")
            parameters.append(strategy_key)
        if strategy_kind:
            conditions.append("strategy_kind=?")
            parameters.append(strategy_kind)
        if outcome:
            conditions.append("trade_count=0" if outcome == "empty" else "trade_count>0")
        if search and search.strip():
            conditions.append(
                "(instr(lower(strategy_name), lower(?))>0 OR "
                "instr(lower(strategy_key), lower(?))>0)"
            )
            parameters.extend([search.strip(), search.strip()])
        return " AND ".join(conditions), parameters

    def backtest_run(
        self, run_id: str, owner_user_id: str | None = None
    ) -> dict[str, object] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM backtest_runs WHERE run_id=? AND owner_user_id=?",
                (run_id, self._owner(owner_user_id)),
            ).fetchone()
        return self._backtest_row(row, detail=True) if row else None

    def delete_backtest_run(
        self, run_id: str, owner_user_id: str | None = None
    ) -> dict[str, object] | None:
        owner = self._owner(owner_user_id)
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM backtest_runs WHERE run_id=? AND owner_user_id=?",
                (run_id, owner),
            ).fetchone()
            if row is None:
                return None
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    "DELETE FROM backtest_runs WHERE run_id=? AND owner_user_id=?",
                    (run_id, owner),
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        deleted = self._backtest_row(row)
        deleted["released_strategy_reference"] = (
            row["strategy_kind"] == "composite"
        )
        return deleted

    def delete_backtest_runs(
        self,
        run_ids: list[str],
        delete_all: bool = False,
        owner_user_id: str | None = None,
        exclude_run_ids: list[str] | None = None,
    ) -> dict[str, object] | None:
        owner = self._owner(owner_user_id)
        ids = list(dict.fromkeys(item.strip() for item in run_ids if item.strip()))
        excluded = list(dict.fromkeys(
            item.strip() for item in (exclude_run_ids or []) if item.strip()
        ))
        if not delete_all and not ids:
            raise ValueError("至少需要選擇一筆回測紀錄")

        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                if delete_all:
                    where = "owner_user_id=?"
                    params: list[str] = [owner]
                    if excluded:
                        placeholders = ",".join("?" for _ in excluded)
                        where += f" AND run_id NOT IN ({placeholders})"
                        params.extend(excluded)
                    rows = self.connection.execute(
                        "SELECT run_id,strategy_kind FROM backtest_runs "
                        f"WHERE {where}", params,
                    ).fetchall()
                    self.connection.execute(
                        f"DELETE FROM backtest_runs WHERE {where}", params,
                    )
                else:
                    placeholders = ",".join("?" for _ in ids)
                    rows = self.connection.execute(
                        f"SELECT run_id,strategy_kind FROM backtest_runs "
                        f"WHERE run_id IN ({placeholders}) AND owner_user_id=?",
                        [*ids, owner],
                    ).fetchall()
                    found = {str(row["run_id"]) for row in rows}
                    if any(run_id not in found for run_id in ids):
                        self.connection.rollback()
                        return None
                    self.connection.execute(
                        f"DELETE FROM backtest_runs "
                        f"WHERE run_id IN ({placeholders}) AND owner_user_id=?",
                        [*ids, owner],
                    )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

        return {
            "deleted_runs": len(rows),
            "released_strategy_references": sum(
                row["strategy_kind"] == "composite" for row in rows
            ),
        }

    def close(self) -> None:
        with self.lock:
            self.connection.close()

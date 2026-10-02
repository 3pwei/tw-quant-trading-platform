from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime
from typing import Mapping, Protocol, Sequence
from uuid import uuid4


UNKNOWN_LINEAGE: dict[str, str] = {"status": "legacy_unknown"}


class StrategyVersionRepository(Protocol):
    def ensure_strategy_version(
        self,
        strategy_key: str,
        parameters: Mapping[str, object],
        plugin_identity: Mapping[str, object],
        owner_user_id: str | None = None,
    ) -> dict[str, object]: ...

    def strategy_version(
        self,
        strategy_key: str,
        version: int,
        owner_user_id: str | None = None,
    ) -> dict[str, object] | None: ...

    def strategy_versions(
        self, strategy_key: str, owner_user_id: str | None = None
    ) -> list[dict[str, object]]: ...


class PortfolioRepository(Protocol):
    def save_portfolio_version(
        self,
        portfolio_id: str,
        name: str,
        members: Sequence[Mapping[str, object]],
        owner_user_id: str | None = None,
    ) -> dict[str, object]: ...

    def portfolio(
        self,
        portfolio_id: str,
        version: int | None,
        owner_user_id: str | None = None,
    ) -> dict[str, object] | None: ...

    def archive_portfolio(
        self, portfolio_id: str, owner_user_id: str | None = None
    ) -> dict[str, object]: ...


class RiskProfileRepository(Protocol):
    def save_risk_profile_version(
        self,
        profile_id: str,
        name: str,
        environment: str,
        values: Mapping[str, object],
        owner_user_id: str | None = None,
    ) -> dict[str, object]: ...

    def risk_profile(
        self,
        profile_id: str,
        version: int | None,
        owner_user_id: str | None = None,
    ) -> dict[str, object] | None: ...

    def bind_risk_profile(
        self,
        profile_id: str,
        profile_version: int,
        subject: Mapping[str, object],
        owner_user_id: str | None = None,
    ) -> dict[str, object]: ...


_PLUGIN_COLUMNS = (
    "plugin_id",
    "plugin_version",
    "distribution_name",
    "distribution_version",
    "artifact_digest",
    "parameter_schema_version",
)

_FORBIDDEN_RISK_KEYS = {
    "api_key",
    "arm",
    "armed",
    "authorization",
    "authorized",
    "bypass_arm",
    "ca_password",
    "ca_path",
    "certificate",
    "clear_kill_switch",
    "credential",
    "credentials",
    "kill_switch_active",
    "live_authorized",
    "live_enabled",
    "password",
    "private_key",
    "secret",
    "secret_ref",
    "token",
}

_FORBIDDEN_RISK_KEY_PARTS = {
    "apikey",
    "authorization",
    "certificate",
    "credential",
    "password",
    "privatekey",
    "secret",
    "token",
}


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def known_plugin_lineage(identity: Mapping[str, object]) -> dict[str, object]:
    values = {name: str(identity.get(name, "")).strip() for name in _PLUGIN_COLUMNS}
    values["distribution_name"] = values["distribution_name"] or str(
        identity.get("artifact_name", "")
    ).strip()
    values["distribution_version"] = values["distribution_version"] or str(
        identity.get("artifact_version", "")
    ).strip()
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError("incomplete plugin lineage: " + ", ".join(missing))
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", values["artifact_digest"]):
        raise ValueError("plugin artifact digest must use sha256 identity")
    return {"status": "known", **values}


def strategy_lineage(
    kind: str,
    strategy_key: str,
    version: int,
    plugin_identity: Mapping[str, object],
) -> dict[str, object]:
    if kind not in {"atomic", "composite"}:
        raise ValueError("unsupported strategy lineage kind")
    if not strategy_key.strip() or int(version) < 1:
        raise ValueError("exact strategy version is required")
    return {
        "status": "known",
        "strategy_kind": kind,
        "strategy_key": strategy_key,
        "strategy_version": int(version),
        "plugin": known_plugin_lineage(plugin_identity),
    }


class SQLiteRuntimeDataMixin:
    """Additive Architecture v2 runtime-data repositories.

    The host adapter supplies ``connection``, ``lock`` and ``_owner``.  This
    mixin never invokes Git, a build tool, a network client or deployment code.
    """

    connection: sqlite3.Connection

    def _ensure_architecture_v2_runtime_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS architecture_v2_schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_composite_owner_version
            ON composite_strategies(owner_user_id, strategy_id, version)
            """,
            """
            CREATE TABLE IF NOT EXISTS strategy_versions (
                owner_user_id TEXT NOT NULL,
                strategy_key TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version > 0),
                parameters_json TEXT NOT NULL,
                plugin_id TEXT NOT NULL,
                plugin_version TEXT NOT NULL,
                distribution_name TEXT NOT NULL,
                distribution_version TEXT NOT NULL,
                artifact_digest TEXT NOT NULL CHECK(artifact_digest LIKE 'sha256:%'),
                parameter_schema_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(owner_user_id, strategy_key, version)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_strategy_versions_owner_latest
            ON strategy_versions(owner_user_id, strategy_key, version DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS composite_strategy_lineage (
                owner_user_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                plugin_id TEXT NOT NULL,
                plugin_version TEXT NOT NULL,
                distribution_name TEXT NOT NULL,
                distribution_version TEXT NOT NULL,
                artifact_digest TEXT NOT NULL CHECK(artifact_digest LIKE 'sha256:%'),
                parameter_schema_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(owner_user_id, strategy_id, version),
                FOREIGN KEY(owner_user_id, strategy_id, version)
                    REFERENCES composite_strategies(owner_user_id, strategy_id, version)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS portfolios (
                owner_user_id TEXT NOT NULL,
                portfolio_id TEXT NOT NULL,
                name TEXT NOT NULL,
                current_version INTEGER NOT NULL DEFAULT 0 CHECK(current_version >= 0),
                archived_at TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY(owner_user_id, portfolio_id)
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_portfolio_owner_name
            ON portfolios(owner_user_id, name COLLATE NOCASE)
            """,
            """
            CREATE TABLE IF NOT EXISTS portfolio_versions (
                owner_user_id TEXT NOT NULL,
                portfolio_id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version > 0),
                created_at TEXT NOT NULL,
                PRIMARY KEY(owner_user_id, portfolio_id, version),
                FOREIGN KEY(owner_user_id, portfolio_id)
                    REFERENCES portfolios(owner_user_id, portfolio_id)
                    ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS portfolio_atomic_members (
                owner_user_id TEXT NOT NULL,
                portfolio_id TEXT NOT NULL,
                portfolio_version INTEGER NOT NULL,
                ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                strategy_key TEXT NOT NULL,
                strategy_version INTEGER NOT NULL,
                weight REAL NOT NULL CHECK(weight >= 0.0),
                plugin_id TEXT NOT NULL,
                plugin_version TEXT NOT NULL,
                distribution_name TEXT NOT NULL,
                distribution_version TEXT NOT NULL,
                artifact_digest TEXT NOT NULL CHECK(artifact_digest LIKE 'sha256:%'),
                parameter_schema_version TEXT NOT NULL,
                PRIMARY KEY(owner_user_id, portfolio_id, portfolio_version, ordinal),
                UNIQUE(owner_user_id, portfolio_id, portfolio_version, strategy_key, strategy_version),
                FOREIGN KEY(owner_user_id, portfolio_id, portfolio_version)
                    REFERENCES portfolio_versions(owner_user_id, portfolio_id, version)
                    ON DELETE RESTRICT,
                FOREIGN KEY(owner_user_id, strategy_key, strategy_version)
                    REFERENCES strategy_versions(owner_user_id, strategy_key, version)
                    ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS portfolio_composite_members (
                owner_user_id TEXT NOT NULL,
                portfolio_id TEXT NOT NULL,
                portfolio_version INTEGER NOT NULL,
                ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                strategy_id TEXT NOT NULL,
                strategy_version INTEGER NOT NULL,
                weight REAL NOT NULL CHECK(weight >= 0.0),
                plugin_id TEXT NOT NULL,
                plugin_version TEXT NOT NULL,
                distribution_name TEXT NOT NULL,
                distribution_version TEXT NOT NULL,
                artifact_digest TEXT NOT NULL CHECK(artifact_digest LIKE 'sha256:%'),
                parameter_schema_version TEXT NOT NULL,
                PRIMARY KEY(owner_user_id, portfolio_id, portfolio_version, ordinal),
                UNIQUE(owner_user_id, portfolio_id, portfolio_version, strategy_id, strategy_version),
                FOREIGN KEY(owner_user_id, portfolio_id, portfolio_version)
                    REFERENCES portfolio_versions(owner_user_id, portfolio_id, version)
                    ON DELETE RESTRICT,
                FOREIGN KEY(owner_user_id, strategy_id, strategy_version)
                    REFERENCES composite_strategies(owner_user_id, strategy_id, version)
                    ON DELETE RESTRICT
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_portfolio_atomic_reference
            ON portfolio_atomic_members(owner_user_id, strategy_key, strategy_version)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_portfolio_composite_reference
            ON portfolio_composite_members(owner_user_id, strategy_id, strategy_version)
            """,
            """
            CREATE TABLE IF NOT EXISTS risk_profiles (
                owner_user_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                name TEXT NOT NULL,
                environment TEXT NOT NULL CHECK(environment IN ('paper', 'live')),
                current_version INTEGER NOT NULL DEFAULT 0 CHECK(current_version >= 0),
                archived_at TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY(owner_user_id, profile_id)
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_risk_profile_owner_name
            ON risk_profiles(owner_user_id, name COLLATE NOCASE)
            """,
            """
            CREATE TABLE IF NOT EXISTS risk_profile_versions (
                owner_user_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version > 0),
                values_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(owner_user_id, profile_id, version),
                FOREIGN KEY(owner_user_id, profile_id)
                    REFERENCES risk_profiles(owner_user_id, profile_id)
                    ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS risk_profile_bindings (
                binding_id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                profile_version INTEGER NOT NULL,
                environment TEXT NOT NULL CHECK(environment IN ('paper', 'live')),
                subject_kind TEXT NOT NULL CHECK(subject_kind IN ('atomic', 'composite', 'portfolio')),
                subject_key TEXT NOT NULL,
                subject_version INTEGER NOT NULL CHECK(subject_version > 0),
                strategy_lineage_json TEXT NOT NULL,
                authorizes_live INTEGER NOT NULL DEFAULT 0 CHECK(authorizes_live = 0),
                bypasses_arm INTEGER NOT NULL DEFAULT 0 CHECK(bypasses_arm = 0),
                clears_kill_switch INTEGER NOT NULL DEFAULT 0 CHECK(clears_kill_switch = 0),
                created_at TEXT NOT NULL,
                FOREIGN KEY(owner_user_id, profile_id, profile_version)
                    REFERENCES risk_profile_versions(owner_user_id, profile_id, version)
                    ON DELETE RESTRICT
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_risk_bindings_subject
            ON risk_profile_bindings(owner_user_id, subject_kind, subject_key, subject_version)
            """,
            """
            CREATE TABLE IF NOT EXISTS backtest_atomic_lineage (
                run_id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                strategy_key TEXT NOT NULL,
                strategy_version INTEGER NOT NULL,
                plugin_id TEXT NOT NULL,
                plugin_version TEXT NOT NULL,
                distribution_name TEXT NOT NULL,
                distribution_version TEXT NOT NULL,
                artifact_digest TEXT NOT NULL,
                parameter_schema_version TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
                FOREIGN KEY(owner_user_id, strategy_key, strategy_version)
                    REFERENCES strategy_versions(owner_user_id, strategy_key, version)
                    ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS backtest_composite_lineage (
                run_id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_version INTEGER NOT NULL,
                plugin_id TEXT NOT NULL,
                plugin_version TEXT NOT NULL,
                distribution_name TEXT NOT NULL,
                distribution_version TEXT NOT NULL,
                artifact_digest TEXT NOT NULL,
                parameter_schema_version TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
                FOREIGN KEY(owner_user_id, strategy_id, strategy_version)
                    REFERENCES composite_strategies(owner_user_id, strategy_id, version)
                    ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS strategy_runtime_atomic_lineage (
                runtime_id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                strategy_key TEXT NOT NULL,
                strategy_version INTEGER NOT NULL,
                plugin_id TEXT NOT NULL,
                plugin_version TEXT NOT NULL,
                distribution_name TEXT NOT NULL,
                distribution_version TEXT NOT NULL,
                artifact_digest TEXT NOT NULL,
                parameter_schema_version TEXT NOT NULL,
                FOREIGN KEY(runtime_id) REFERENCES strategy_runtimes(runtime_id) ON DELETE CASCADE,
                FOREIGN KEY(owner_user_id, strategy_key, strategy_version)
                    REFERENCES strategy_versions(owner_user_id, strategy_key, version)
                    ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS strategy_runtime_composite_lineage (
                runtime_id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_version INTEGER NOT NULL,
                plugin_id TEXT NOT NULL,
                plugin_version TEXT NOT NULL,
                distribution_name TEXT NOT NULL,
                distribution_version TEXT NOT NULL,
                artifact_digest TEXT NOT NULL,
                parameter_schema_version TEXT NOT NULL,
                FOREIGN KEY(runtime_id) REFERENCES strategy_runtimes(runtime_id) ON DELETE CASCADE,
                FOREIGN KEY(owner_user_id, strategy_id, strategy_version)
                    REFERENCES composite_strategies(owner_user_id, strategy_id, version)
                    ON DELETE RESTRICT
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS strategy_versions_no_update
            BEFORE UPDATE ON strategy_versions BEGIN
                SELECT RAISE(ABORT, 'strategy versions are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS strategy_versions_no_delete
            BEFORE DELETE ON strategy_versions BEGIN
                SELECT RAISE(ABORT, 'strategy versions are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS portfolio_versions_no_update
            BEFORE UPDATE ON portfolio_versions BEGIN
                SELECT RAISE(ABORT, 'portfolio versions are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS portfolio_versions_no_delete
            BEFORE DELETE ON portfolio_versions BEGIN
                SELECT RAISE(ABORT, 'portfolio versions are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS portfolio_atomic_members_no_update
            BEFORE UPDATE ON portfolio_atomic_members BEGIN
                SELECT RAISE(ABORT, 'portfolio members are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS portfolio_atomic_members_no_delete
            BEFORE DELETE ON portfolio_atomic_members BEGIN
                SELECT RAISE(ABORT, 'portfolio members are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS portfolio_composite_members_no_update
            BEFORE UPDATE ON portfolio_composite_members BEGIN
                SELECT RAISE(ABORT, 'portfolio members are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS portfolio_composite_members_no_delete
            BEFORE DELETE ON portfolio_composite_members BEGIN
                SELECT RAISE(ABORT, 'portfolio members are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS risk_profile_versions_no_update
            BEFORE UPDATE ON risk_profile_versions BEGIN
                SELECT RAISE(ABORT, 'risk profile versions are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS risk_profile_versions_no_delete
            BEFORE DELETE ON risk_profile_versions BEGIN
                SELECT RAISE(ABORT, 'risk profile versions are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS risk_profile_bindings_no_update
            BEFORE UPDATE ON risk_profile_bindings BEGIN
                SELECT RAISE(ABORT, 'risk profile bindings are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS risk_profile_bindings_no_delete
            BEFORE DELETE ON risk_profile_bindings BEGIN
                SELECT RAISE(ABORT, 'risk profile bindings are immutable');
            END
            """,
        )
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                self.connection.execute(statement)
            self.connection.execute(
                "INSERT OR IGNORE INTO architecture_v2_schema_migrations "
                "(migration_id, applied_at) VALUES (?, ?)",
                ("p4-runtime-data-v1", self._runtime_now()),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    @staticmethod
    def _runtime_now() -> str:
        return datetime.now().astimezone().isoformat(timespec="microseconds")

    @staticmethod
    def _plugin_values(lineage: Mapping[str, object]) -> tuple[str, ...]:
        known = known_plugin_lineage(lineage)
        return tuple(str(known[name]) for name in _PLUGIN_COLUMNS)

    @staticmethod
    def _plugin_from_row(row: sqlite3.Row) -> dict[str, object]:
        return {"status": "known", **{name: row[name] for name in _PLUGIN_COLUMNS}}

    def _insert_strategy_version_locked(
        self,
        owner: str,
        strategy_key: str,
        parameters: Mapping[str, object],
        plugin_identity: Mapping[str, object],
    ) -> dict[str, object]:
        key = strategy_key.strip().lower()
        if not key:
            raise ValueError("strategy key is required")
        payload = canonical_json(dict(parameters))
        plugin_values = self._plugin_values(plugin_identity)
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM strategy_versions "
            "WHERE owner_user_id=? AND strategy_key=?",
            (owner, key),
        ).fetchone()
        version = int(row["version"]) + 1
        created_at = self._runtime_now()
        self.connection.execute(
            "INSERT INTO strategy_versions(owner_user_id,strategy_key,version,"
            "parameters_json,plugin_id,plugin_version,distribution_name,"
            "distribution_version,artifact_digest,parameter_schema_version,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (owner, key, version, payload, *plugin_values, created_at),
        )
        return {
            "owner_user_id": owner,
            "strategy_key": key,
            "version": version,
            "parameters": json.loads(payload),
            "lineage": strategy_lineage("atomic", key, version, plugin_identity),
            "created_at": created_at,
        }

    def ensure_strategy_version(
        self,
        strategy_key: str,
        parameters: Mapping[str, object],
        plugin_identity: Mapping[str, object],
        owner_user_id: str | None = None,
    ) -> dict[str, object]:
        owner = self._owner(owner_user_id)
        key = strategy_key.strip().lower()
        payload = canonical_json(dict(parameters))
        plugin_values = self._plugin_values(plugin_identity)
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                row = self.connection.execute(
                    "SELECT * FROM strategy_versions WHERE owner_user_id=? "
                    "AND strategy_key=? ORDER BY version DESC LIMIT 1",
                    (owner, key),
                ).fetchone()
                if row is not None and row["parameters_json"] == payload and tuple(
                    str(row[name]) for name in _PLUGIN_COLUMNS
                ) == plugin_values:
                    self.connection.commit()
                    return self._strategy_version_row(row)
                result = self._insert_strategy_version_locked(
                    owner, key, parameters, plugin_identity
                )
                self.connection.commit()
                return result
            except Exception:
                self.connection.rollback()
                raise

    def strategy_version(
        self,
        strategy_key: str,
        version: int,
        owner_user_id: str | None = None,
    ) -> dict[str, object] | None:
        owner = self._owner(owner_user_id)
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM strategy_versions WHERE owner_user_id=? "
                "AND strategy_key=? AND version=?",
                (owner, strategy_key.strip().lower(), int(version)),
            ).fetchone()
        return self._strategy_version_row(row) if row else None

    def strategy_versions(
        self, strategy_key: str, owner_user_id: str | None = None
    ) -> list[dict[str, object]]:
        owner = self._owner(owner_user_id)
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM strategy_versions WHERE owner_user_id=? "
                "AND strategy_key=? ORDER BY version DESC",
                (owner, strategy_key.strip().lower()),
            ).fetchall()
        return [self._strategy_version_row(row) for row in rows]

    def _strategy_version_row(self, row: sqlite3.Row) -> dict[str, object]:
        plugin = self._plugin_from_row(row)
        return {
            "owner_user_id": row["owner_user_id"],
            "strategy_key": row["strategy_key"],
            "version": int(row["version"]),
            "parameters": json.loads(row["parameters_json"]),
            "lineage": strategy_lineage(
                "atomic", row["strategy_key"], int(row["version"]), plugin
            ),
            "created_at": row["created_at"],
        }

    def _insert_composite_lineage_locked(
        self,
        owner: str,
        strategy_id: str,
        version: int,
        plugin_identity: Mapping[str, object],
        created_at: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO composite_strategy_lineage(owner_user_id,strategy_id,"
            "version,plugin_id,plugin_version,distribution_name,distribution_version,"
            "artifact_digest,parameter_schema_version,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (owner, strategy_id, version, *self._plugin_values(plugin_identity), created_at),
        )

    def _known_composite_lineage_locked(
        self, owner: str, strategy_id: str, version: int
    ) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT * FROM composite_strategy_lineage WHERE owner_user_id=? "
            "AND strategy_id=? AND version=?",
            (owner, strategy_id, int(version)),
        ).fetchone()
        if row is None:
            return None
        return strategy_lineage(
            "composite", strategy_id, int(version), self._plugin_from_row(row)
        )

    @staticmethod
    def _normalize_portfolio_members(
        members: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        if not members:
            raise ValueError("portfolio requires at least one member")
        normalized: list[dict[str, object]] = []
        identities: set[tuple[str, str, int]] = set()
        for item in members:
            kind = str(item.get("kind", "")).strip().lower()
            key = str(item.get("strategy_key", item.get("key", ""))).strip()
            try:
                version = int(item.get("strategy_version", item.get("version", 0)))
                weight = float(item.get("weight"))
            except (TypeError, ValueError) as exc:
                raise ValueError("portfolio version and weight must be numeric") from exc
            if kind not in {"atomic", "composite"} or not key or version < 1:
                raise ValueError("portfolio members require exact strategy versions")
            if not math.isfinite(weight) or weight < 0:
                raise ValueError("portfolio weights must be finite and non-negative")
            identity = (kind, key, version)
            if identity in identities:
                raise ValueError("duplicate portfolio member reference")
            identities.add(identity)
            normalized.append(
                {"kind": kind, "strategy_key": key, "strategy_version": version, "weight": weight}
            )
        if not math.isclose(
            math.fsum(float(item["weight"]) for item in normalized),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("portfolio weights must sum to 1.0")
        return normalized

    def save_portfolio_version(
        self,
        portfolio_id: str,
        name: str,
        members: Sequence[Mapping[str, object]],
        owner_user_id: str | None = None,
    ) -> dict[str, object]:
        owner = self._owner(owner_user_id)
        identifier = portfolio_id.strip()
        label = name.strip()
        if not identifier or not label:
            raise ValueError("portfolio id and name are required")
        normalized = self._normalize_portfolio_members(members)
        created_at = self._runtime_now()
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                identity = self.connection.execute(
                    "SELECT * FROM portfolios WHERE owner_user_id=? AND portfolio_id=?",
                    (owner, identifier),
                ).fetchone()
                if identity is not None:
                    if identity["archived_at"] is not None:
                        raise ValueError("archived portfolio cannot be modified")
                    if identity["name"] != label:
                        raise ValueError("portfolio name is immutable")
                else:
                    self.connection.execute(
                        "INSERT INTO portfolios(owner_user_id,portfolio_id,name,created_at) "
                        "VALUES (?,?,?,?)",
                        (owner, identifier, label, created_at),
                    )
                current = self.connection.execute(
                    "SELECT COALESCE(MAX(version),0) AS version FROM portfolio_versions "
                    "WHERE owner_user_id=? AND portfolio_id=?",
                    (owner, identifier),
                ).fetchone()
                portfolio_version = int(current["version"]) + 1
                prepared: list[tuple[dict[str, object], dict[str, object]]] = []
                for member in normalized:
                    if member["kind"] == "atomic":
                        row = self.connection.execute(
                            "SELECT * FROM strategy_versions WHERE owner_user_id=? "
                            "AND strategy_key=? AND version=?",
                            (owner, member["strategy_key"], member["strategy_version"]),
                        ).fetchone()
                        lineage = (
                            strategy_lineage(
                                "atomic",
                                str(member["strategy_key"]),
                                int(member["strategy_version"]),
                                self._plugin_from_row(row),
                            )
                            if row is not None else None
                        )
                    else:
                        lineage = self._known_composite_lineage_locked(
                            owner,
                            str(member["strategy_key"]),
                            int(member["strategy_version"]),
                        )
                    if lineage is None:
                        raise ValueError("unknown, cross-owner, or legacy-unknown strategy version")
                    prepared.append((member, lineage))
                self.connection.execute(
                    "INSERT INTO portfolio_versions(owner_user_id,portfolio_id,version,created_at) "
                    "VALUES (?,?,?,?)",
                    (owner, identifier, portfolio_version, created_at),
                )
                for ordinal, (member, lineage) in enumerate(prepared):
                    plugin = lineage["plugin"]
                    table = (
                        "portfolio_atomic_members"
                        if member["kind"] == "atomic"
                        else "portfolio_composite_members"
                    )
                    key_column = "strategy_key" if member["kind"] == "atomic" else "strategy_id"
                    self.connection.execute(
                        f"INSERT INTO {table}(owner_user_id,portfolio_id,portfolio_version,"
                        f"ordinal,{key_column},strategy_version,weight,plugin_id,plugin_version,"
                        "distribution_name,distribution_version,artifact_digest,"
                        "parameter_schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            owner,
                            identifier,
                            portfolio_version,
                            ordinal,
                            member["strategy_key"],
                            member["strategy_version"],
                            member["weight"],
                            *self._plugin_values(plugin),
                        ),
                    )
                self.connection.execute(
                    "UPDATE portfolios SET current_version=? WHERE owner_user_id=? "
                    "AND portfolio_id=?",
                    (portfolio_version, owner, identifier),
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        result = self.portfolio(identifier, portfolio_version, owner)
        assert result is not None
        return result

    def portfolio(
        self,
        portfolio_id: str,
        version: int | None = None,
        owner_user_id: str | None = None,
    ) -> dict[str, object] | None:
        owner = self._owner(owner_user_id)
        with self.lock:
            identity = self.connection.execute(
                "SELECT * FROM portfolios WHERE owner_user_id=? AND portfolio_id=?",
                (owner, portfolio_id),
            ).fetchone()
            if identity is None:
                return None
            selected = int(version) if version is not None else int(identity["current_version"])
            if selected < 1:
                return None
            exists = self.connection.execute(
                "SELECT created_at FROM portfolio_versions WHERE owner_user_id=? "
                "AND portfolio_id=? AND version=?",
                (owner, portfolio_id, selected),
            ).fetchone()
            if exists is None:
                return None
            members: list[dict[str, object]] = []
            for kind, table, key_column in (
                ("atomic", "portfolio_atomic_members", "strategy_key"),
                ("composite", "portfolio_composite_members", "strategy_id"),
            ):
                rows = self.connection.execute(
                    f"SELECT * FROM {table} WHERE owner_user_id=? AND portfolio_id=? "
                    "AND portfolio_version=?",
                    (owner, portfolio_id, selected),
                ).fetchall()
                members.extend(
                    {
                        "kind": kind,
                        "strategy_key": row[key_column],
                        "strategy_version": int(row["strategy_version"]),
                        "weight": float(row["weight"]),
                        "ordinal": int(row["ordinal"]),
                        "plugin_lineage": self._plugin_from_row(row),
                    }
                    for row in rows
                )
        members.sort(key=lambda item: int(item["ordinal"]))
        for item in members:
            item.pop("ordinal", None)
        return {
            "owner_user_id": owner,
            "portfolio_id": portfolio_id,
            "name": identity["name"],
            "version": selected,
            "current": selected == int(identity["current_version"]),
            "archived": identity["archived_at"] is not None,
            "archived_at": identity["archived_at"],
            "members": members,
            "created_at": exists["created_at"],
        }

    def archive_portfolio(
        self, portfolio_id: str, owner_user_id: str | None = None
    ) -> dict[str, object]:
        owner = self._owner(owner_user_id)
        archived_at = self._runtime_now()
        with self.lock:
            cursor = self.connection.execute(
                "UPDATE portfolios SET archived_at=? WHERE owner_user_id=? "
                "AND portfolio_id=? AND archived_at IS NULL",
                (archived_at, owner, portfolio_id),
            )
            self.connection.commit()
        if not cursor.rowcount:
            raise ValueError("portfolio not found or already archived")
        return {"portfolio_id": portfolio_id, "archived_at": archived_at}

    @classmethod
    def _validate_risk_values(cls, value: object, path: str = "risk") -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                normalized = re.sub(
                    r"[^a-z0-9]+", "_", str(key).strip().lower()
                ).strip("_")
                compact = normalized.replace("_", "")
                if not normalized:
                    raise ValueError("risk profile keys must be non-empty")
                if normalized in _FORBIDDEN_RISK_KEYS or any(
                    part in compact for part in _FORBIDDEN_RISK_KEY_PARTS
                ):
                    raise ValueError(
                        "risk profiles cannot persist authority or secrets: "
                        f"{path}.{key}"
                    )
                cls._validate_risk_values(nested, f"{path}.{key}")
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for index, nested in enumerate(value):
                cls._validate_risk_values(nested, f"{path}[{index}]")
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"risk profile values must be finite: {path}")

    def save_risk_profile_version(
        self,
        profile_id: str,
        name: str,
        environment: str,
        values: Mapping[str, object],
        owner_user_id: str | None = None,
    ) -> dict[str, object]:
        owner = self._owner(owner_user_id)
        identifier = profile_id.strip()
        label = name.strip()
        scope = environment.strip().lower()
        if not identifier or not label or scope not in {"paper", "live"}:
            raise ValueError("risk profile id, name and paper/live environment are required")
        self._validate_risk_values(values)
        payload = canonical_json(dict(values))
        created_at = self._runtime_now()
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                identity = self.connection.execute(
                    "SELECT * FROM risk_profiles WHERE owner_user_id=? AND profile_id=?",
                    (owner, identifier),
                ).fetchone()
                if identity is not None:
                    if identity["archived_at"] is not None:
                        raise ValueError("archived risk profile cannot be modified")
                    if identity["name"] != label or identity["environment"] != scope:
                        raise ValueError("risk profile identity is immutable")
                else:
                    self.connection.execute(
                        "INSERT INTO risk_profiles(owner_user_id,profile_id,name,environment,created_at) "
                        "VALUES (?,?,?,?,?)",
                        (owner, identifier, label, scope, created_at),
                    )
                current = self.connection.execute(
                    "SELECT COALESCE(MAX(version),0) AS version FROM risk_profile_versions "
                    "WHERE owner_user_id=? AND profile_id=?",
                    (owner, identifier),
                ).fetchone()
                version = int(current["version"]) + 1
                self.connection.execute(
                    "INSERT INTO risk_profile_versions(owner_user_id,profile_id,version,"
                    "values_json,created_at) VALUES (?,?,?,?,?)",
                    (owner, identifier, version, payload, created_at),
                )
                self.connection.execute(
                    "UPDATE risk_profiles SET current_version=? WHERE owner_user_id=? "
                    "AND profile_id=?",
                    (version, owner, identifier),
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        result = self.risk_profile(identifier, version, owner)
        assert result is not None
        return result

    def risk_profile(
        self,
        profile_id: str,
        version: int | None = None,
        owner_user_id: str | None = None,
    ) -> dict[str, object] | None:
        owner = self._owner(owner_user_id)
        with self.lock:
            identity = self.connection.execute(
                "SELECT * FROM risk_profiles WHERE owner_user_id=? AND profile_id=?",
                (owner, profile_id),
            ).fetchone()
            if identity is None:
                return None
            selected = int(version) if version is not None else int(identity["current_version"])
            row = self.connection.execute(
                "SELECT * FROM risk_profile_versions WHERE owner_user_id=? "
                "AND profile_id=? AND version=?",
                (owner, profile_id, selected),
            ).fetchone()
        if row is None:
            return None
        return {
            "owner_user_id": owner,
            "profile_id": profile_id,
            "name": identity["name"],
            "environment": identity["environment"],
            "version": selected,
            "current": selected == int(identity["current_version"]),
            "archived": identity["archived_at"] is not None,
            "values": json.loads(row["values_json"]),
            "created_at": row["created_at"],
            "live_authorized": False,
            "arm_authorized": False,
            "kill_switch_override": False,
        }

    def _portfolio_lineage_locked(
        self, owner: str, portfolio_id: str, version: int
    ) -> dict[str, object] | None:
        exists = self.connection.execute(
            "SELECT 1 FROM portfolio_versions WHERE owner_user_id=? "
            "AND portfolio_id=? AND version=?",
            (owner, portfolio_id, version),
        ).fetchone()
        if exists is None:
            return None
        members: list[dict[str, object]] = []
        for kind, table, key_column in (
            ("atomic", "portfolio_atomic_members", "strategy_key"),
            ("composite", "portfolio_composite_members", "strategy_id"),
        ):
            rows = self.connection.execute(
                f"SELECT * FROM {table} WHERE owner_user_id=? AND portfolio_id=? "
                "AND portfolio_version=? ORDER BY ordinal",
                (owner, portfolio_id, version),
            ).fetchall()
            members.extend(
                {
                    "strategy_kind": kind,
                    "strategy_key": row[key_column],
                    "strategy_version": int(row["strategy_version"]),
                    "weight": float(row["weight"]),
                    "plugin": self._plugin_from_row(row),
                }
                for row in rows
            )
        return {
            "status": "known",
            "strategy_kind": "portfolio",
            "strategy_key": portfolio_id,
            "strategy_version": version,
            "members": members,
        }

    def bind_risk_profile(
        self,
        profile_id: str,
        profile_version: int,
        subject: Mapping[str, object],
        owner_user_id: str | None = None,
    ) -> dict[str, object]:
        owner = self._owner(owner_user_id)
        kind = str(subject.get("kind", "")).strip().lower()
        key = str(subject.get("strategy_key", subject.get("key", ""))).strip()
        try:
            version = int(subject.get("strategy_version", subject.get("version", 0)))
        except (TypeError, ValueError) as exc:
            raise ValueError("risk binding requires an exact subject version") from exc
        if kind not in {"atomic", "composite", "portfolio"} or not key or version < 1:
            raise ValueError("risk binding requires an exact subject version")
        binding_id = uuid4().hex
        created_at = self._runtime_now()
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                profile = self.connection.execute(
                    "SELECT identity.environment FROM risk_profiles identity "
                    "JOIN risk_profile_versions version ON "
                    "version.owner_user_id=identity.owner_user_id AND "
                    "version.profile_id=identity.profile_id "
                    "WHERE identity.owner_user_id=? AND identity.profile_id=? "
                    "AND version.version=? AND identity.archived_at IS NULL",
                    (owner, profile_id, int(profile_version)),
                ).fetchone()
                if profile is None:
                    raise ValueError("unknown or cross-owner risk profile version")
                if kind == "atomic":
                    row = self.connection.execute(
                        "SELECT * FROM strategy_versions WHERE owner_user_id=? "
                        "AND strategy_key=? AND version=?",
                        (owner, key, version),
                    ).fetchone()
                    lineage = (
                        strategy_lineage("atomic", key, version, self._plugin_from_row(row))
                        if row is not None else None
                    )
                elif kind == "composite":
                    lineage = self._known_composite_lineage_locked(owner, key, version)
                else:
                    lineage = self._portfolio_lineage_locked(owner, key, version)
                if lineage is None:
                    raise ValueError("unknown, cross-owner, or legacy-unknown binding subject")
                self.connection.execute(
                    "INSERT INTO risk_profile_bindings(binding_id,owner_user_id,profile_id,"
                    "profile_version,environment,subject_kind,subject_key,subject_version,"
                    "strategy_lineage_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        binding_id,
                        owner,
                        profile_id,
                        int(profile_version),
                        profile["environment"],
                        kind,
                        key,
                        version,
                        canonical_json(lineage),
                        created_at,
                    ),
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        return {
            "binding_id": binding_id,
            "owner_user_id": owner,
            "profile_id": profile_id,
            "profile_version": int(profile_version),
            "environment": profile["environment"],
            "subject_kind": kind,
            "subject_key": key,
            "subject_version": version,
            "strategy_lineage": lineage,
            "live_authorized": False,
            "arm_authorized": False,
            "kill_switch_override": False,
            "created_at": created_at,
        }

    def _insert_snapshot_lineage_locked(
        self,
        target: str,
        record_id: str,
        owner: str,
        lineage: Mapping[str, object] | None,
    ) -> None:
        if lineage is None:
            return
        if lineage.get("status") != "known":
            raise ValueError("new snapshots require known strategy lineage")
        kind = str(lineage.get("strategy_kind", ""))
        key = str(lineage.get("strategy_key", ""))
        version = int(lineage.get("strategy_version", 0))
        plugin = lineage.get("plugin")
        if kind not in {"atomic", "composite"} or not isinstance(plugin, Mapping):
            raise ValueError("invalid strategy lineage")
        prefix = "backtest" if target == "backtest" else "strategy_runtime"
        table = f"{prefix}_{kind}_lineage"
        id_column = "run_id" if target == "backtest" else "runtime_id"
        key_column = "strategy_key" if kind == "atomic" else "strategy_id"
        self.connection.execute(
            f"INSERT INTO {table}({id_column},owner_user_id,{key_column},strategy_version,"
            "plugin_id,plugin_version,distribution_name,distribution_version,"
            "artifact_digest,parameter_schema_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (record_id, owner, key, version, *self._plugin_values(plugin)),
        )

    def _snapshot_lineage_locked(self, target: str, record_id: str) -> dict[str, object]:
        prefix = "backtest" if target == "backtest" else "strategy_runtime"
        id_column = "run_id" if target == "backtest" else "runtime_id"
        for kind, key_column in (("atomic", "strategy_key"), ("composite", "strategy_id")):
            row = self.connection.execute(
                f"SELECT * FROM {prefix}_{kind}_lineage WHERE {id_column}=?",
                (record_id,),
            ).fetchone()
            if row is not None:
                return strategy_lineage(
                    kind,
                    row[key_column],
                    int(row["strategy_version"]),
                    self._plugin_from_row(row),
                )
        return dict(UNKNOWN_LINEAGE)

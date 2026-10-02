from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Protocol

from .execution_targets import (
    ExecutionTarget,
    ExecutionTargetStatus,
    mask_account_id,
)
from .identity import BrokerAccountRef
from tw_quant_core.broker.registry import BrokerRegistry, BrokerRuntimeState
from .settings import BrokerConnectionSettings


class ExecutionTargetRepository(Protocol):
    def create(self, target: ExecutionTarget) -> ExecutionTarget: ...
    def get(self, target_id: str) -> ExecutionTarget | None: ...
    def get_owned(self, owner_user_id: str, target_id: str) -> ExecutionTarget | None: ...
    def list_for_owner(self, owner_user_id: str) -> list[ExecutionTarget]: ...
    def list_active(self) -> list[ExecutionTarget]: ...
    def list_all(self) -> list[ExecutionTarget]: ...
    def find_by_account_ref(self, account_ref: BrokerAccountRef) -> ExecutionTarget | None: ...
    def update_status(self, target_id: str, status: ExecutionTargetStatus) -> ExecutionTarget: ...
    def exists(self, target_id: str) -> bool: ...


class SQLiteExecutionTargetRepository:
    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(target, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = Lock()
        self._migrate()

    def _migrate(self) -> None:
        with self.lock, self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_target_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_targets (
                    target_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    broker_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    masked_account_id TEXT NOT NULL,
                    secret_ref TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','disabled','locked')),
                    connection_id TEXT,
                    display_name TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(broker_name, account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_execution_targets_owner
                    ON execution_targets(owner_user_id, created_at, target_id);
                """
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO execution_target_schema_migrations VALUES (?, ?)",
                (self.SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
            )

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> ExecutionTarget | None:
        if row is None:
            return None
        return ExecutionTarget(
            target_id=str(row["target_id"]),
            owner_user_id=str(row["owner_user_id"]),
            broker_name=str(row["broker_name"]),
            account_id=str(row["account_id"]),
            masked_account_id=str(row["masked_account_id"]),
            secret_ref=str(row["secret_ref"]),
            status=ExecutionTargetStatus(str(row["status"])),
            connection_id=row["connection_id"],
            display_name=row["display_name"],
            metadata=json.loads(str(row["metadata_json"])),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def create(self, target: ExecutionTarget) -> ExecutionTarget:
        try:
            with self.lock, self.connection:
                self.connection.execute(
                    """INSERT INTO execution_targets (
                        target_id, owner_user_id, broker_name, account_id,
                        masked_account_id, secret_ref, status, connection_id,
                        display_name, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        target.target_id, target.owner_user_id, target.broker_name,
                        target.account_id, target.masked_account_id, target.secret_ref,
                        target.status.value, target.connection_id, target.display_name,
                        json.dumps(dict(target.metadata), sort_keys=True),
                        target.created_at.isoformat(), target.updated_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("duplicate execution target identity or broker account") from exc
        return target

    def get(self, target_id: str) -> ExecutionTarget | None:
        with self.lock:
            return self._from_row(self.connection.execute(
                "SELECT * FROM execution_targets WHERE target_id = ?", (target_id,)
            ).fetchone())

    def get_owned(self, owner_user_id: str, target_id: str) -> ExecutionTarget | None:
        with self.lock:
            return self._from_row(self.connection.execute(
                "SELECT * FROM execution_targets WHERE owner_user_id = ? AND target_id = ?",
                (owner_user_id, target_id),
            ).fetchone())

    def list_for_owner(self, owner_user_id: str) -> list[ExecutionTarget]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM execution_targets WHERE owner_user_id = ? ORDER BY created_at, target_id",
                (owner_user_id,),
            ).fetchall()
        return [target for row in rows if (target := self._from_row(row)) is not None]

    def list_active(self) -> list[ExecutionTarget]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM execution_targets WHERE status = 'active' ORDER BY created_at, target_id"
            ).fetchall()
        return [target for row in rows if (target := self._from_row(row)) is not None]

    def list_all(self) -> list[ExecutionTarget]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM execution_targets ORDER BY created_at, target_id"
            ).fetchall()
        return [target for row in rows if (target := self._from_row(row)) is not None]

    def find_by_account_ref(self, account_ref: BrokerAccountRef) -> ExecutionTarget | None:
        with self.lock:
            return self._from_row(self.connection.execute(
                "SELECT * FROM execution_targets WHERE broker_name = ? AND account_id = ?",
                (account_ref.broker_name, account_ref.account_id),
            ).fetchone())

    def update_status(self, target_id: str, status: ExecutionTargetStatus) -> ExecutionTarget:
        updated_at = datetime.now(timezone.utc).isoformat()
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE execution_targets SET status = ?, updated_at = ? WHERE target_id = ?",
                (status.value, updated_at, target_id),
            )
        if cursor.rowcount != 1 or (target := self.get(target_id)) is None:
            raise KeyError("unknown execution target")
        return target

    def exists(self, target_id: str) -> bool:
        return self.get(target_id) is not None

    def close(self) -> None:
        self.connection.close()


def legacy_target_id(owner_user_id: str, account_ref: BrokerAccountRef) -> str:
    digest = sha256(
        f"execution-target-v1|{owner_user_id}|{account_ref.broker_name}|{account_ref.account_id}".encode()
    ).hexdigest()[:32]
    return "exec_" + digest


def bootstrap_legacy_execution_target(
    repository: ExecutionTargetRepository,
    *,
    owner_user_ids: frozenset[str],
    connection: BrokerConnectionSettings,
) -> ExecutionTarget:
    """Idempotently adopt one exact legacy account; never guesses its owner."""

    owners = frozenset(owner.strip() for owner in owner_user_ids if owner.strip())
    if len(owners) != 1:
        raise ValueError("legacy execution target requires exactly one owner")
    account_ref = connection.account_ref
    if account_ref is None or account_ref.broker_name == "disabled":
        raise ValueError("legacy execution target requires an exact broker account")
    owner = next(iter(owners))
    existing = repository.find_by_account_ref(account_ref)
    if existing is not None:
        if existing.owner_user_id != owner:
            raise PermissionError("legacy execution target owner mismatch")
        return existing
    now = datetime.now(timezone.utc)
    return repository.create(ExecutionTarget(
        target_id=legacy_target_id(owner, account_ref),
        owner_user_id=owner,
        broker_name=account_ref.broker_name,
        account_id=account_ref.account_id,
        masked_account_id=mask_account_id(account_ref.account_id),
        secret_ref=connection.secret_ref,
        status=ExecutionTargetStatus.ACTIVE,
        connection_id=connection.connection_id,
        created_at=now,
        updated_at=now,
        metadata={"source": "legacy_environment_bootstrap"},
    ))


class OwnedExecutionTargetResolver:
    """Exact owner-scoped target-to-runtime boundary with no fallback routing."""

    def __init__(self, targets: ExecutionTargetRepository, registry: BrokerRegistry):
        self.targets = targets
        self.registry = registry

    def resolve(self, owner_user_id: str, target_id: str) -> BrokerAccountRef:
        target = self.targets.get_owned(owner_user_id, target_id)
        if target is None:
            raise KeyError("unknown owned execution target")
        if target.status is not ExecutionTargetStatus.ACTIVE:
            raise RuntimeError(f"execution target is {target.status.value}")
        registration = self.registry.registration(target.account_ref)
        if registration.account_ref != target.account_ref:
            raise RuntimeError("broker execution target mismatch")
        if registration.state is not BrokerRuntimeState.READY:
            raise RuntimeError(f"broker execution target is {registration.state.value}")
        return target.account_ref


class OwnedExecutionTargetCatalog:
    """Application-facing exact owned-target lookup without legacy public IDs."""

    def __init__(self, targets: ExecutionTargetRepository):
        self.targets_repository = targets

    def resolve(self, target_id: str, owner_user_id: str) -> BrokerAccountRef:
        target = self.targets_repository.get_owned(owner_user_id, target_id)
        if target is None or target.status is not ExecutionTargetStatus.ACTIVE:
            raise KeyError("unknown owned execution target")
        return target.account_ref

    def targets(self, owner_user_id: str) -> tuple[BrokerAccountRef, ...]:
        return tuple(
            target.account_ref for target in self.targets_repository.list_for_owner(owner_user_id)
            if target.status is ExecutionTargetStatus.ACTIVE
        )

    def public_targets(self, owner_user_id: str) -> list[dict[str, str]]:
        return [
            target.to_public_dict()
            for target in self.targets_repository.list_for_owner(owner_user_id)
            if target.status is ExecutionTargetStatus.ACTIVE
        ]

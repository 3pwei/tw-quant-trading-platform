from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Protocol, Sequence

from .identity import BrokerAccountRef


class RecoveryStatus(str, Enum):
    LOCKED = "locked"
    RECONCILING = "reconciling"
    READY = "ready"


@dataclass(frozen=True)
class RecoveryState:
    broker_name: str
    account_id: str
    status: RecoveryStatus
    updated_at: datetime
    generation: int
    issue_codes: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.status is RecoveryStatus.READY


class RecoveryLockStore(Protocol):
    def state(self, broker_name: str, account_id: str) -> RecoveryState: ...

    def begin(
        self, broker_name: str, account_id: str, *, updated_at: datetime
    ) -> RecoveryState: ...

    def force_lock(
        self,
        broker_name: str,
        account_id: str,
        issue_codes: Sequence[str],
        *,
        updated_at: datetime,
    ) -> RecoveryState: ...

    def complete(
        self,
        broker_name: str,
        account_id: str,
        issue_codes: Sequence[str],
        *,
        expected_generation: int,
        updated_at: datetime,
    ) -> RecoveryState: ...

    def assert_ready(self, broker_name: str, account_id: str) -> None: ...


@dataclass(frozen=True)
class RecoveryOrderGate:
    store: RecoveryLockStore
    account_ref: BrokerAccountRef

    def assert_ordering_allowed(self) -> None:
        self.store.assert_ready(
            self.account_ref.broker_name, self.account_ref.account_id
        )


class SQLiteRecoveryLockRepository:
    """Persistent startup gate; absence of a row means trading is locked."""

    def __init__(self, path: str | Path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(target, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = Lock()
        self._migrate()

    def _migrate(self) -> None:
        with self.lock:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS live_recovery_lock (
                    broker_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    issue_codes_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    PRIMARY KEY (broker_name, account_id)
                )
                """
            )
            self.connection.commit()

    @staticmethod
    def _record(row: sqlite3.Row) -> RecoveryState:
        return RecoveryState(
            broker_name=str(row["broker_name"]),
            account_id=str(row["account_id"]),
            status=RecoveryStatus(str(row["status"])),
            issue_codes=tuple(json.loads(str(row["issue_codes_json"]))),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            generation=int(row["generation"]),
        )

    @staticmethod
    def _validate_identity(broker_name: str, account_id: str) -> None:
        if not broker_name.strip() or not account_id.strip():
            raise ValueError("broker_name and account_id are required")

    def state(self, broker_name: str, account_id: str) -> RecoveryState:
        self._validate_identity(broker_name, account_id)
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM live_recovery_lock "
                "WHERE broker_name=? AND account_id=?",
                (broker_name, account_id),
            ).fetchone()
            if row is None:
                now = datetime.now(timezone.utc)
                self.connection.execute(
                    "INSERT OR IGNORE INTO live_recovery_lock "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        broker_name,
                        account_id,
                        RecoveryStatus.LOCKED.value,
                        json.dumps(["reconciliation_required"]),
                        now.isoformat(timespec="microseconds"),
                        0,
                    ),
                )
                self.connection.commit()
                row = self.connection.execute(
                    "SELECT * FROM live_recovery_lock "
                    "WHERE broker_name=? AND account_id=?",
                    (broker_name, account_id),
                ).fetchone()
        assert row is not None
        return self._record(row)

    @staticmethod
    def _validate_time(updated_at: datetime) -> None:
        if updated_at.tzinfo is None or updated_at.utcoffset() is None:
            raise ValueError("recovery lock updated_at must be timezone-aware")

    def begin(
        self, broker_name: str, account_id: str, *, updated_at: datetime
    ) -> RecoveryState:
        self._validate_identity(broker_name, account_id)
        self._validate_time(updated_at)
        with self.lock:
            self.connection.execute(
                "INSERT INTO live_recovery_lock VALUES (?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(broker_name, account_id) DO UPDATE SET "
                "status=?, issue_codes_json=?, updated_at=?, "
                "generation=live_recovery_lock.generation+1",
                (
                    broker_name,
                    account_id,
                    RecoveryStatus.RECONCILING.value,
                    json.dumps(["reconciliation_in_progress"]),
                    updated_at.isoformat(timespec="microseconds"),
                    RecoveryStatus.RECONCILING.value,
                    json.dumps(["reconciliation_in_progress"]),
                    updated_at.isoformat(timespec="microseconds"),
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM live_recovery_lock "
                "WHERE broker_name=? AND account_id=?",
                (broker_name, account_id),
            ).fetchone()
            self.connection.commit()
        assert row is not None
        return self._record(row)

    def force_lock(
        self,
        broker_name: str,
        account_id: str,
        issue_codes: Sequence[str],
        *,
        updated_at: datetime,
    ) -> RecoveryState:
        """Persist a new locked generation before or after unsafe broker I/O.

        Incrementing the generation invalidates any older reconciliation that
        may still be completing after a timeout or disconnect.
        """

        self._validate_identity(broker_name, account_id)
        self._validate_time(updated_at)
        unique_codes = tuple(dict.fromkeys(issue_codes)) or (
            "reconciliation_required",
        )
        with self.lock:
            self.connection.execute(
                "INSERT INTO live_recovery_lock VALUES (?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(broker_name, account_id) DO UPDATE SET "
                "status=?, issue_codes_json=?, updated_at=?, "
                "generation=live_recovery_lock.generation+1",
                (
                    broker_name,
                    account_id,
                    RecoveryStatus.LOCKED.value,
                    json.dumps(unique_codes),
                    updated_at.isoformat(timespec="microseconds"),
                    RecoveryStatus.LOCKED.value,
                    json.dumps(unique_codes),
                    updated_at.isoformat(timespec="microseconds"),
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM live_recovery_lock "
                "WHERE broker_name=? AND account_id=?",
                (broker_name, account_id),
            ).fetchone()
            self.connection.commit()
        assert row is not None
        return self._record(row)

    def complete(
        self,
        broker_name: str,
        account_id: str,
        issue_codes: Sequence[str],
        *,
        expected_generation: int,
        updated_at: datetime,
    ) -> RecoveryState:
        self._validate_identity(broker_name, account_id)
        self._validate_time(updated_at)
        if expected_generation < 1:
            raise ValueError("expected_generation must be positive")
        unique_codes = tuple(dict.fromkeys(issue_codes))
        status = RecoveryStatus.LOCKED if issue_codes else RecoveryStatus.READY
        with self.lock:
            cursor = self.connection.execute(
                "UPDATE live_recovery_lock SET status=?, issue_codes_json=?, "
                "updated_at=? WHERE broker_name=? AND account_id=? AND generation=?",
                (
                    status.value,
                    json.dumps(unique_codes),
                    updated_at.isoformat(timespec="microseconds"),
                    broker_name,
                    account_id,
                    expected_generation,
                ),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise RuntimeError("stale recovery reconciliation cannot change lock")
            row = self.connection.execute(
                "SELECT * FROM live_recovery_lock "
                "WHERE broker_name=? AND account_id=?",
                (broker_name, account_id),
            ).fetchone()
            self.connection.commit()
        assert row is not None
        return self._record(row)

    def assert_ready(self, broker_name: str, account_id: str) -> None:
        current = self.state(broker_name, account_id)
        if not current.ready:
            issues = ",".join(current.issue_codes) or "reconciliation_required"
            raise RuntimeError(f"live recovery lock is active: {issues}")

    def close(self) -> None:
        self.connection.close()

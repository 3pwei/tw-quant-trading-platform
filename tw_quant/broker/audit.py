from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Protocol

from .events import BrokerEvent, canonical_payload
from .models import BrokerOrder


class BrokerEventAuditStatus(str, Enum):
    RECEIVED = "received"
    RECONCILED = "reconciled"
    UNMATCHED = "unmatched"
    FAILED = "failed"


@dataclass(frozen=True)
class BrokerEventAuditRecord:
    event: BrokerEvent
    status: BrokerEventAuditStatus
    updated_at: datetime
    owner_id: str | None = None
    client_order_id: str | None = None
    error: str | None = None


class BrokerEventAuditStore(Protocol):
    def record_received(
        self, event: BrokerEvent
    ) -> tuple[BrokerEventAuditRecord, bool]: ...

    def mark(
        self,
        event_id: str,
        status: BrokerEventAuditStatus,
        *,
        updated_at: datetime,
        order: BrokerOrder | None = None,
        error: str | None = None,
    ) -> BrokerEventAuditRecord: ...


class SQLiteBrokerEventAuditRepository:
    """Append-first audit store for normalized broker callback events."""

    def __init__(self, path: str | Path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(target, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = Lock()
        self._migrate()

    def _migrate(self) -> None:
        with self.lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS live_broker_events (
                    event_id TEXT PRIMARY KEY,
                    broker_name TEXT NOT NULL,
                    account_id TEXT,
                    event_type TEXT NOT NULL,
                    broker_order_id TEXT,
                    received_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    processing_status TEXT NOT NULL,
                    owner_user_id TEXT,
                    client_order_id TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_broker_events_status_updated
                    ON live_broker_events(processing_status, updated_at);
                """
            )
            columns = {
                str(row["name"])
                for row in self.connection.execute(
                    "PRAGMA table_info(live_broker_events)"
                ).fetchall()
            }
            if "account_id" not in columns:
                self.connection.execute(
                    "ALTER TABLE live_broker_events ADD COLUMN account_id TEXT"
                )
            self.connection.executescript(
                """
                DROP INDEX IF EXISTS idx_broker_events_order_received;
                CREATE INDEX idx_broker_events_order_received
                    ON live_broker_events(
                        broker_name, account_id, broker_order_id, received_at DESC
                    );
                """
            )
            self.connection.commit()

    @staticmethod
    def _record(row: sqlite3.Row) -> BrokerEventAuditRecord:
        event = BrokerEvent(
            event_id=str(row["event_id"]),
            broker_name=str(row["broker_name"]),
            account_id=(
                str(row["account_id"]) if row["account_id"] is not None else None
            ),
            event_type=str(row["event_type"]),
            broker_order_id=(
                str(row["broker_order_id"]) if row["broker_order_id"] else None
            ),
            received_at=datetime.fromisoformat(str(row["received_at"])),
            payload=json.loads(str(row["payload_json"])),
        )
        return BrokerEventAuditRecord(
            event=event,
            status=BrokerEventAuditStatus(str(row["processing_status"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            owner_id=(str(row["owner_user_id"]) if row["owner_user_id"] else None),
            client_order_id=(
                str(row["client_order_id"]) if row["client_order_id"] else None
            ),
            error=str(row["error"]) if row["error"] else None,
        )

    def record_received(
        self, event: BrokerEvent
    ) -> tuple[BrokerEventAuditRecord, bool]:
        timestamp = event.received_at.isoformat(timespec="microseconds")
        with self.lock:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO live_broker_events "
                "(event_id, broker_name, event_type, broker_order_id, received_at, "
                "account_id, payload_json, processing_status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.broker_name,
                    event.event_type,
                    event.broker_order_id,
                    timestamp,
                    event.account_id,
                    canonical_payload(event.payload),
                    BrokerEventAuditStatus.RECEIVED.value,
                    timestamp,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM live_broker_events WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
            self.connection.commit()
        assert row is not None
        return self._record(row), cursor.rowcount == 1

    def mark(
        self,
        event_id: str,
        status: BrokerEventAuditStatus,
        *,
        updated_at: datetime,
        order: BrokerOrder | None = None,
        error: str | None = None,
    ) -> BrokerEventAuditRecord:
        if updated_at.tzinfo is None or updated_at.utcoffset() is None:
            raise ValueError("broker event updated_at must be timezone-aware")
        with self.lock:
            cursor = self.connection.execute(
                "UPDATE live_broker_events SET processing_status=?, "
                "owner_user_id=?, client_order_id=?, error=?, updated_at=? "
                "WHERE event_id=?",
                (
                    status.value,
                    order.request.owner_id if order else None,
                    order.request.client_order_id if order else None,
                    error[:1_000] if error else None,
                    updated_at.isoformat(timespec="microseconds"),
                    event_id,
                ),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise KeyError(f"unknown broker event: {event_id}")
            row = self.connection.execute(
                "SELECT * FROM live_broker_events WHERE event_id=?", (event_id,)
            ).fetchone()
            self.connection.commit()
        assert row is not None
        return self._record(row)

    def get(self, event_id: str) -> BrokerEventAuditRecord | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM live_broker_events WHERE event_id=?", (event_id,)
            ).fetchone()
        return self._record(row) if row else None

    def close(self) -> None:
        self.connection.close()

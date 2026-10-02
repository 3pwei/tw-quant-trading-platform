from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import Lock

from .canary import CanaryArmSession
from .identity import BrokerAccountRef


class SQLiteCanaryArmRepository:
    """Shared ARM/audit state; active ARM records are cleared on every process boot."""

    def __init__(self, path: str | Path, *, clear_on_start: bool = True):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(target, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = Lock()
        with self.lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS live_canary_arms (
                    arm_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    broker_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    armed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    target_id TEXT,
                    UNIQUE(owner_user_id, broker_name, account_id)
                );
                CREATE TABLE IF NOT EXISTS live_canary_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    broker_name TEXT,
                    account_id TEXT,
                    arm_id TEXT,
                    request_id TEXT,
                    detail_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_live_canary_audit_owner_time
                    ON live_canary_audit(owner_user_id, occurred_at DESC);
                """
            )
            columns = {str(row[1]) for row in self.connection.execute(
                "PRAGMA table_info(live_canary_arms)"
            ).fetchall()}
            if "target_id" not in columns:
                self.connection.execute("ALTER TABLE live_canary_arms ADD COLUMN target_id TEXT")
            if clear_on_start:
                rows = self.connection.execute(
                    "SELECT * FROM live_canary_arms"
                ).fetchall()
                for row in rows:
                    self._audit_locked(
                        "arm.cleared_restart",
                        str(row["owner_user_id"]),
                        BrokerAccountRef(str(row["broker_name"]), str(row["account_id"])),
                        arm_id=str(row["arm_id"]),
                        detail={"reason": "process_restart"},
                        occurred_at=datetime.now().astimezone(),
                    )
                self.connection.execute("DELETE FROM live_canary_arms")
            self.connection.commit()

    @staticmethod
    def _session(row: sqlite3.Row) -> CanaryArmSession:
        return CanaryArmSession(
            arm_id=str(row["arm_id"]),
            owner_id=str(row["owner_user_id"]),
            account_ref=BrokerAccountRef(
                str(row["broker_name"]), str(row["account_id"])
            ),
            armed_at=datetime.fromisoformat(str(row["armed_at"])),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
            created_by=str(row["created_by"]),
            target_id=row["target_id"],
        )

    def _audit_locked(
        self,
        action: str,
        owner_id: str,
        target: BrokerAccountRef | None,
        *,
        arm_id: str | None = None,
        request_id: str | None = None,
        detail: dict[str, object] | None = None,
        occurred_at: datetime,
    ) -> None:
        self.connection.execute(
            "INSERT INTO live_canary_audit "
            "(action,owner_user_id,broker_name,account_id,arm_id,request_id,detail_json,occurred_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                action,
                owner_id,
                target.broker_name if target else None,
                target.account_id if target else None,
                arm_id,
                request_id,
                json.dumps(detail or {}, sort_keys=True, separators=(",", ":")),
                occurred_at.isoformat(timespec="microseconds"),
            ),
        )

    def audit(
        self,
        action: str,
        owner_id: str,
        target: BrokerAccountRef | None,
        *,
        arm_id: str | None = None,
        request_id: str | None = None,
        detail: dict[str, object] | None = None,
        occurred_at: datetime,
    ) -> None:
        with self.lock:
            self._audit_locked(
                action, owner_id, target, arm_id=arm_id, request_id=request_id,
                detail=detail, occurred_at=occurred_at,
            )
            self.connection.commit()

    def arm(self, session: CanaryArmSession) -> None:
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "DELETE FROM live_canary_arms WHERE owner_user_id=?",
                (session.owner_id,),
            )
            self.connection.execute(
                "INSERT INTO live_canary_arms "
                "(arm_id,owner_user_id,broker_name,account_id,armed_at,expires_at,created_by,target_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    session.arm_id,
                    session.owner_id,
                    session.account_ref.broker_name,
                    session.account_ref.account_id,
                    session.armed_at.isoformat(timespec="microseconds"),
                    session.expires_at.isoformat(timespec="microseconds"),
                    session.created_by,
                    session.target_id,
                ),
            )
            self._audit_locked(
                "arm.activated", session.owner_id, session.account_ref,
                arm_id=session.arm_id,
                detail={"expires_at": session.expires_at.isoformat(timespec="seconds")},
                occurred_at=session.armed_at,
            )
            self.connection.commit()

    def active(
        self, owner_id: str, target: BrokerAccountRef, now: datetime,
        target_id: str | None = None,
    ) -> CanaryArmSession | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM live_canary_arms WHERE owner_user_id=? "
                "AND broker_name=? AND account_id=?",
                (owner_id, target.broker_name, target.account_id),
            ).fetchone()
            if row is None:
                return None
            session = self._session(row)
            if target_id is not None and session.target_id != target_id:
                return None
            if session.active(now):
                return session
            self.connection.execute(
                "DELETE FROM live_canary_arms WHERE arm_id=?", (session.arm_id,)
            )
            self._audit_locked(
                "arm.expired", owner_id, target, arm_id=session.arm_id,
                detail={"reason": "ttl_expired"}, occurred_at=now,
            )
            self.connection.commit()
            return None

    def disarm(
        self, owner_id: str, target: BrokerAccountRef, *, reason: str
    ) -> bool:
        with self.lock:
            row = self.connection.execute(
                "SELECT arm_id FROM live_canary_arms WHERE owner_user_id=? "
                "AND broker_name=? AND account_id=?",
                (owner_id, target.broker_name, target.account_id),
            ).fetchone()
            if row is None:
                return False
            self.connection.execute(
                "DELETE FROM live_canary_arms WHERE arm_id=?", (row["arm_id"],)
            )
            self._audit_locked(
                "arm.deactivated", owner_id, target, arm_id=str(row["arm_id"]),
                detail={"reason": reason}, occurred_at=datetime.now().astimezone(),
            )
            self.connection.commit()
            return True

    def audit_events(self, owner_id: str) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM live_canary_audit WHERE owner_user_id=? "
                "ORDER BY audit_id", (owner_id,),
            ).fetchall()
        return [
            {
                "action": str(row["action"]),
                "owner_id": str(row["owner_user_id"]),
                "broker_name": row["broker_name"],
                "masked_account_id": (
                    "****" + str(row["account_id"])[-4:]
                    if row["account_id"] else None
                ),
                "arm_id": row["arm_id"],
                "request_id": row["request_id"],
                "detail": json.loads(str(row["detail_json"])),
                "occurred_at": str(row["occurred_at"]),
            }
            for row in rows
        ]

    def close(self) -> None:
        with self.lock:
            self.connection.close()

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Mapping

from .guardian_models import GuardianExitReason, ManagedLivePosition, ManagedPositionState
from .identity import BrokerAccountRef


class SQLitePositionGuardianRepository:
    """Durable managed-position state and atomic per-position exit reservation."""

    def __init__(self, path: str | Path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(target, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = Lock()
        with self.lock:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS live_managed_positions (
                    position_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    broker_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    average_fill_price REAL NOT NULL,
                    protection_quantity INTEGER NOT NULL,
                    stop_loss_price REAL NOT NULL,
                    take_profit_price REAL NOT NULL,
                    strategy_id TEXT NOT NULL,
                    strategy_version INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    active_exit_order_id TEXT,
                    active_exit_reason TEXT,
                    pending_exit_reason TEXT,
                    issue_code TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(owner_user_id, broker_name, account_id, contract)
                );
                CREATE INDEX IF NOT EXISTS idx_live_managed_positions_target
                    ON live_managed_positions(broker_name, account_id, state);
                CREATE TABLE IF NOT EXISTS live_guardian_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_live_guardian_audit_position
                    ON live_guardian_audit(position_id, audit_id);
                """
            )
            self.connection.commit()

    @staticmethod
    def _position(row: sqlite3.Row) -> ManagedLivePosition:
        return ManagedLivePosition(
            position_id=str(row["position_id"]),
            owner_id=str(row["owner_user_id"]),
            account_ref=BrokerAccountRef(str(row["broker_name"]), str(row["account_id"])),
            symbol=str(row["symbol"]),
            contract=str(row["contract"]),
            quantity=int(row["quantity"]),
            average_fill_price=float(row["average_fill_price"]),
            protection_quantity=int(row["protection_quantity"]),
            stop_loss_price=float(row["stop_loss_price"]),
            take_profit_price=float(row["take_profit_price"]),
            strategy_id=str(row["strategy_id"]),
            strategy_version=int(row["strategy_version"]),
            state=ManagedPositionState(str(row["state"])),
            generation=int(row["generation"]),
            active_exit_order_id=row["active_exit_order_id"],
            active_exit_reason=(
                GuardianExitReason(str(row["active_exit_reason"]))
                if row["active_exit_reason"] else None
            ),
            pending_exit_reason=(
                GuardianExitReason(str(row["pending_exit_reason"]))
                if row["pending_exit_reason"] else None
            ),
            issue_code=row["issue_code"],
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def _save(self, position: ManagedLivePosition) -> None:
        self.connection.execute(
            "INSERT INTO live_managed_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(position_id) DO UPDATE SET "
            "quantity=excluded.quantity,average_fill_price=excluded.average_fill_price,"
            "protection_quantity=excluded.protection_quantity,"
            "stop_loss_price=excluded.stop_loss_price,take_profit_price=excluded.take_profit_price,"
            "strategy_id=excluded.strategy_id,strategy_version=excluded.strategy_version,"
            "state=excluded.state,generation=excluded.generation,"
            "active_exit_order_id=excluded.active_exit_order_id,"
            "active_exit_reason=excluded.active_exit_reason,"
            "pending_exit_reason=excluded.pending_exit_reason,issue_code=excluded.issue_code,"
            "updated_at=excluded.updated_at",
            (
                position.position_id, position.owner_id,
                position.account_ref.broker_name, position.account_ref.account_id,
                position.symbol, position.contract, position.quantity,
                position.average_fill_price, position.protection_quantity,
                position.stop_loss_price, position.take_profit_price,
                position.strategy_id, position.strategy_version, position.state.value,
                position.generation, position.active_exit_order_id,
                position.active_exit_reason.value if position.active_exit_reason else None,
                position.pending_exit_reason.value if position.pending_exit_reason else None,
                position.issue_code,
                position.updated_at.isoformat(timespec="microseconds"),
            ),
        )

    def _audit_locked(
        self, position_id: str, action: str, detail: Mapping[str, object], occurred_at: datetime
    ) -> None:
        self.connection.execute(
            "INSERT INTO live_guardian_audit(position_id,action,detail_json,occurred_at) "
            "VALUES (?,?,?,?)",
            (position_id, action, json.dumps(dict(detail), sort_keys=True), occurred_at.isoformat(timespec="microseconds")),
        )

    def synchronize(self, position: ManagedLivePosition) -> ManagedLivePosition:
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            self._save(position)
            self._audit_locked(
                position.position_id, "position.synchronized",
                {"quantity": position.quantity, "state": position.state.value, "generation": position.generation},
                position.updated_at,
            )
            self.connection.commit()
        return position

    def mark_flat(self, position_id: str, occurred_at: datetime) -> ManagedLivePosition:
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT * FROM live_managed_positions WHERE position_id=?", (position_id,)
            ).fetchone()
            if row is None:
                self.connection.rollback()
                raise KeyError("unknown managed live position")
            current = self._position(row)
            flat = replace(
                current, quantity=0, protection_quantity=0,
                state=ManagedPositionState.FLAT, active_exit_order_id=None,
                active_exit_reason=None, pending_exit_reason=None, issue_code=None,
                updated_at=occurred_at,
            )
            self._save(flat)
            self._audit_locked(position_id, "position.flat", {}, occurred_at)
            self.connection.commit()
        return flat

    def get(self, position_id: str) -> ManagedLivePosition | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM live_managed_positions WHERE position_id=?", (position_id,)
            ).fetchone()
        return self._position(row) if row else None

    def positions(self, target: BrokerAccountRef) -> list[ManagedLivePosition]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM live_managed_positions WHERE broker_name=? AND account_id=? "
                "ORDER BY position_id", (target.broker_name, target.account_id)
            ).fetchall()
        return [self._position(row) for row in rows]

    def reserve_exit(self, position_id, reason, client_order_id, occurred_at):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT * FROM live_managed_positions WHERE position_id=?", (position_id,)
            ).fetchone()
            if row is None:
                self.connection.rollback()
                raise KeyError("unknown managed live position")
            current = self._position(row)
            if current.state is ManagedPositionState.FLAT:
                self.connection.rollback()
                raise RuntimeError("guardian_position_flat")
            if current.active_exit_order_id:
                pending = current.pending_exit_reason
                if current.active_exit_reason and reason.priority > current.active_exit_reason.priority:
                    pending = reason
                    current = replace(current, pending_exit_reason=pending, updated_at=occurred_at)
                    self._save(current)
                    self._audit_locked(
                        position_id, "exit.priority_deferred",
                        {"active": current.active_exit_reason.value, "pending": reason.value}, occurred_at,
                    )
                    self.connection.commit()
                    return current, False
                self.connection.commit()
                return current, False
            reserved = replace(
                current, state=ManagedPositionState.EXIT_PENDING,
                active_exit_order_id=client_order_id, active_exit_reason=reason,
                pending_exit_reason=None, issue_code=None, updated_at=occurred_at,
            )
            self._save(reserved)
            self._audit_locked(
                position_id, "exit.reserved",
                {"reason": reason.value, "client_order_id": client_order_id, "quantity": current.protection_quantity},
                occurred_at,
            )
            self.connection.commit()
            return reserved, True

    def release_exit(self, position_id, client_order_id, reason):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT * FROM live_managed_positions WHERE position_id=?", (position_id,)
            ).fetchone()
            if row is None:
                self.connection.rollback()
                return
            current = self._position(row)
            if current.active_exit_order_id == client_order_id:
                current = replace(
                    current, state=ManagedPositionState.ACTIVE,
                    active_exit_order_id=None, active_exit_reason=None,
                    updated_at=datetime.now(timezone.utc), issue_code=reason,
                )
                self._save(current)
                self._audit_locked(position_id, "exit.released", {"reason": reason}, current.updated_at)
            self.connection.commit()

    def lock(self, position_id, issue_code, occurred_at):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT * FROM live_managed_positions WHERE position_id=?", (position_id,)
            ).fetchone()
            if row is not None:
                current = replace(
                    self._position(row), state=ManagedPositionState.LOCKED,
                    issue_code=issue_code, updated_at=occurred_at,
                )
                self._save(current)
                self._audit_locked(position_id, "position.locked", {"issue_code": issue_code}, occurred_at)
            self.connection.commit()

    def mark_exit_unknown(self, client_order_id, occurred_at):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT * FROM live_managed_positions WHERE active_exit_order_id=?",
                (client_order_id,),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return False
            current = replace(
                self._position(row), state=ManagedPositionState.UNKNOWN,
                issue_code="guardian_exit_unknown", updated_at=occurred_at,
            )
            self._save(current)
            self._audit_locked(
                current.position_id, "exit.unknown",
                {"client_order_id": client_order_id}, occurred_at,
            )
            self.connection.commit()
            return True

    def audit(self, position_id, action, detail, occurred_at):
        with self.lock:
            self._audit_locked(position_id, action, detail, occurred_at)
            self.connection.commit()

    def audit_events(self, position_id: str) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT action,detail_json,occurred_at FROM live_guardian_audit "
                "WHERE position_id=? ORDER BY audit_id", (position_id,)
            ).fetchall()
        return [
            {"action": str(row[0]), "detail": json.loads(str(row[1])), "occurred_at": str(row[2])}
            for row in rows
        ]

    def close(self) -> None:
        with self.lock:
            self.connection.close()

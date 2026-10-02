from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Iterable

from ..broker import BrokerAccountRef, BrokerOrderRequest, ExecutionMode
from ..execution.shadow import ShadowExecutionResult
from ..risk.live import (
    LiveKillSwitchAction,
    LiveKillSwitchScope,
    LiveKillSwitchState,
)


def _serialize(result: ShadowExecutionResult) -> str:
    value = asdict(result)
    for key in ("trigger_time", "quote_time", "created_at"):
        item = value[key]
        value[key] = item.isoformat(timespec="microseconds") if item else None
    request = value.get("request")
    if request is not None:
        request["mode"] = result.request.mode.value  # type: ignore[union-attr]
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _deserialize(raw: str) -> ShadowExecutionResult:
    value = json.loads(raw)
    request = value.get("request")
    if request is not None:
        request["mode"] = ExecutionMode(request["mode"])
        value["request"] = BrokerOrderRequest(**request)
    for key in ("trigger_time", "created_at"):
        value[key] = datetime.fromisoformat(value[key])
    if value.get("quote_time"):
        value["quote_time"] = datetime.fromisoformat(value["quote_time"])
    return ShadowExecutionResult(**value)


class SQLiteShadowExecutionRepository:
    """Shadow-only sink. Its schema has no live outbox foreign key or trigger."""

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
                CREATE TABLE IF NOT EXISTS live_shadow_results (
                    shadow_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    runtime_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    broker_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    execution_policy_version TEXT NOT NULL,
                    result TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(decision_id, broker_name, account_id, execution_policy_version),
                    CHECK(result IN ('would_submit','rejected'))
                );
                CREATE INDEX IF NOT EXISTS idx_live_shadow_owner_created
                    ON live_shadow_results(owner_user_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS live_shadow_reservations (
                    shadow_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    runtime_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    broker_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    signed_quantity INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY(shadow_id) REFERENCES live_shadow_results(shadow_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_reservation_exposure
                    ON live_shadow_reservations(
                        owner_user_id, symbol, contract, expires_at
                    );
                CREATE TABLE IF NOT EXISTS live_kill_switch_state (
                    scope TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    activated_at TEXT NOT NULL,
                    PRIMARY KEY(scope, scope_key)
                );
                """
            )
            self.connection.commit()

    def get(self, decision_id, broker_name, account_id, policy_version):
        with self.lock:
            row = self.connection.execute(
                "SELECT payload_json FROM live_shadow_results WHERE "
                "decision_id=? AND broker_name=? AND account_id=? "
                "AND execution_policy_version=?",
                (decision_id, broker_name, account_id, policy_version),
            ).fetchone()
        return _deserialize(str(row["payload_json"])) if row else None

    def save(self, result, reservation_expires_at):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO live_shadow_results VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    result.shadow_id,
                    result.owner_id,
                    result.runtime_id,
                    result.decision_id,
                    result.broker_name,
                    result.account_id,
                    result.execution_policy_version,
                    result.result,
                    result.reason,
                    _serialize(result),
                    result.created_at.isoformat(timespec="microseconds"),
                ),
            )
            created = cursor.rowcount == 1
            if created and reservation_expires_at is not None and result.request is not None:
                self.connection.execute(
                "INSERT INTO live_shadow_reservations VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        result.shadow_id,
                        result.owner_id,
                        result.runtime_id,
                        result.symbol,
                        result.contract,
                        result.broker_name,
                        result.account_id,
                        result.request.quantity if result.request.side == "buy" else -result.request.quantity,
                        reservation_expires_at.isoformat(timespec="microseconds"),
                    ),
                )
            row = self.connection.execute(
                "SELECT payload_json FROM live_shadow_results WHERE "
                "decision_id=? AND broker_name=? AND account_id=? "
                "AND execution_policy_version=?",
                (
                    result.decision_id,
                    result.broker_name,
                    result.account_id,
                    result.execution_policy_version,
                ),
            ).fetchone()
            self.connection.commit()
        return _deserialize(str(row["payload_json"])), created

    def active_reservation_quantity(
        self, owner_id, symbol, contract, now, broker_name=None, account_id=None
    ):
        with self.lock:
            self.connection.execute(
                "DELETE FROM live_shadow_reservations WHERE expires_at<=?",
                (now.isoformat(timespec="microseconds"),),
            )
            target_clause = (
                " AND broker_name=? AND account_id=?"
                if broker_name is not None and account_id is not None else ""
            )
            parameters = [owner_id, symbol, contract, now.isoformat(timespec="microseconds")]
            if target_clause:
                parameters.extend((broker_name, account_id))
            row = self.connection.execute(
                "SELECT COALESCE(SUM(signed_quantity),0) AS quantity "
                "FROM live_shadow_reservations WHERE owner_user_id=? "
                "AND symbol=? AND contract=? AND expires_at>?" + target_clause,
                tuple(parameters),
            ).fetchone()
            self.connection.commit()
        return int(row["quantity"])

    def release_runtime(self, runtime_id):
        with self.lock:
            cursor = self.connection.execute(
                "DELETE FROM live_shadow_reservations WHERE runtime_id=?",
                (runtime_id,),
            )
            self.connection.commit()
        return cursor.rowcount

    def list_owner(self, owner_id: str, limit: int = 100) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT payload_json FROM live_shadow_results "
                "WHERE owner_user_id=? ORDER BY created_at DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        return [_deserialize(str(row["payload_json"])).public_dict() for row in rows]

    def activate_kill_switch(self, state: LiveKillSwitchState) -> None:
        with self.lock:
            self.connection.execute(
                "INSERT INTO live_kill_switch_state VALUES (?,?,?,?,?) "
                "ON CONFLICT(scope,scope_key) DO UPDATE SET "
                "action=excluded.action,reason=excluded.reason,"
                "activated_at=excluded.activated_at",
                (
                    state.scope.value,
                    state.scope_key,
                    state.action.value,
                    state.reason,
                    state.activated_at.isoformat(timespec="microseconds"),
                ),
            )
            self.connection.commit()

    def kill_switches(self, scope_keys: set[tuple[str, str]]) -> tuple[LiveKillSwitchState, ...]:
        if not scope_keys:
            return ()
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM live_kill_switch_state"
            ).fetchall()
        return tuple(
            LiveKillSwitchState(
                action=LiveKillSwitchAction(str(row["action"])),
                scope=LiveKillSwitchScope(str(row["scope"])),
                scope_key=str(row["scope_key"]),
                reason=str(row["reason"]),
                activated_at=datetime.fromisoformat(str(row["activated_at"])),
            )
            for row in rows
            if (str(row["scope"]), str(row["scope_key"])) in scope_keys
        )

    def public_kill_switch_preview(
        self, owner_id: str, targets: Iterable[BrokerAccountRef]
    ) -> list[dict[str, object]]:
        """Expose would-actions without leaking account IDs or executing them."""
        target_by_scope = {
            f"{target.broker_name}:{target.account_id}": target
            for target in targets
        }
        allowed = {
            (LiveKillSwitchScope.GLOBAL.value, "global"),
            (LiveKillSwitchScope.OWNER.value, owner_id),
            *(
                (LiveKillSwitchScope.BROKER_ACCOUNT.value, key)
                for key in target_by_scope
            ),
        }
        states = self.kill_switches(allowed)
        result = []
        for state in states:
            target = target_by_scope.get(state.scope_key)
            result.append({
                "scope": state.scope.value,
                "target_id": target.public_id if target else None,
                "action": state.action.value,
                "would_cancel": state.action in {
                    LiveKillSwitchAction.CANCEL_WORKING,
                    LiveKillSwitchAction.FLATTEN,
                },
                "would_flatten": state.action is LiveKillSwitchAction.FLATTEN,
                "reason": state.reason,
                "activated_at": state.activated_at.isoformat(timespec="milliseconds"),
            })
        return result

    def count(self) -> int:
        with self.lock:
            row = self.connection.execute(
                "SELECT COUNT(*) AS count FROM live_shadow_results"
            ).fetchone()
        return int(row["count"])

    def close(self) -> None:
        with self.lock:
            self.connection.close()

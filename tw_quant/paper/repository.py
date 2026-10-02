from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import perf_counter

from ..events import DomainEvent, event_to_dict
from ..broker import canonical_paper_status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class SQLitePaperRepository:
    """Paper event store and rebuildable owner-scoped query projections."""

    def __init__(self, path: str | Path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(target, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = Lock()
        self.write_count = 0
        self.total_write_ms = 0.0
        self.max_write_ms = 0.0
        self.last_write_ms: float | None = None
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        with self.lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    owner_user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_events_owner_sequence
                    ON paper_events(owner_user_id, sequence DESC);

                CREATE TABLE IF NOT EXISTS paper_idempotency (
                    owner_user_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(owner_user_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS paper_controls (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_user_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_controls_owner_sequence
                    ON paper_controls(owner_user_id, sequence DESC);

                CREATE TABLE IF NOT EXISTS paper_order_read_model (
                    owner_user_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    client_order_id TEXT,
                    sequence INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    PRIMARY KEY(owner_user_id, order_id)
                );
                CREATE INDEX IF NOT EXISTS idx_paper_orders_owner_sequence
                    ON paper_order_read_model(owner_user_id, sequence DESC);
                CREATE INDEX IF NOT EXISTS idx_paper_orders_owner_client
                    ON paper_order_read_model(owner_user_id, client_order_id);

                CREATE TABLE IF NOT EXISTS paper_fill_read_model (
                    owner_user_id TEXT NOT NULL,
                    fill_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(owner_user_id, fill_id)
                );
                CREATE INDEX IF NOT EXISTS idx_paper_fills_owner_sequence
                    ON paper_fill_read_model(owner_user_id, sequence DESC);

                CREATE TABLE IF NOT EXISTS paper_position_read_model (
                    owner_user_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    strategy_version INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(
                        owner_user_id, strategy_id, strategy_version,
                        symbol, contract
                    )
                );

                CREATE TABLE IF NOT EXISTS paper_read_model_state (
                    projection TEXT PRIMARY KEY,
                    last_sequence INTEGER NOT NULL
                );
                """
            )
            self._sync_read_models_locked()
            self.connection.commit()

    def _sync_read_models_locked(self) -> None:
        row = self.connection.execute(
            "SELECT last_sequence FROM paper_read_model_state "
            "WHERE projection='paper_account'"
        ).fetchone()
        last_sequence = int(row["last_sequence"]) if row else 0
        rows = self.connection.execute(
            "SELECT sequence, owner_user_id, payload_json FROM paper_events "
            "WHERE sequence>? ORDER BY sequence",
            (last_sequence,),
        ).fetchall()
        for event_row in rows:
            sequence = int(event_row["sequence"])
            try:
                payload = json.loads(str(event_row["payload_json"]))
                if isinstance(payload, dict):
                    self._project_event_locked(
                        sequence, str(event_row["owner_user_id"]), payload
                    )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                # Recovery owns integrity decisions. A malformed legacy event must
                # not prevent the repository from opening or hide that evidence.
                pass
            last_sequence = sequence
        self.connection.execute(
            "INSERT INTO paper_read_model_state(projection, last_sequence) "
            "VALUES ('paper_account', ?) "
            "ON CONFLICT(projection) DO UPDATE SET "
            "last_sequence=excluded.last_sequence",
            (last_sequence,),
        )

    def _project_event_locked(
        self, sequence: int, owner_id: str, payload: dict[str, object]
    ) -> None:
        kind = str(payload.get("kind", ""))
        if kind == "order_intent":
            meta = payload["meta"]
            if not isinstance(meta, dict):
                raise ValueError("order intent metadata is missing")
            snapshot = {
                "order_id": payload["order_id"],
                "client_order_id": payload.get("client_order_id"),
                "submitted_at": datetime.fromisoformat(
                    str(meta["occurred_at"])
                ).isoformat(timespec="milliseconds"),
                "strategy_id": payload["strategy_id"],
                "strategy_version": payload["strategy_version"],
                "symbol": payload["symbol"],
                "contract": payload["contract"],
                "side": payload["side"],
                "quantity": payload["quantity"],
                "reduce_only": payload["reduce_only"],
                "reference_price": payload["reference_price"],
                "stop_loss_price": payload.get("stop_loss_price"),
                "stop_loss_pct": payload.get("stop_loss_pct"),
                "take_profit_pct": payload.get("take_profit_pct"),
                "strategy_snapshot": payload.get("strategy_snapshot"),
                "status": "pending_risk",
                "lifecycle_status": canonical_paper_status("pending_risk").value,
                "status_reason": "awaiting_risk",
                "approved_quantity": 0,
                "fill_id": None,
                "execution_timing": payload.get("execution_timing", "current_close"),
                "order_source": payload.get("order_source", "manual"),
                "runtime_id": payload.get("runtime_id"),
                "decision_id": payload.get("decision_id"),
            }
            self.connection.execute(
                "INSERT INTO paper_order_read_model("
                "owner_user_id, order_id, client_order_id, sequence, snapshot_json"
                ") VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(owner_user_id, order_id) DO UPDATE SET "
                "client_order_id=excluded.client_order_id, "
                "sequence=excluded.sequence, snapshot_json=excluded.snapshot_json",
                (
                    owner_id,
                    str(payload["order_id"]),
                    payload.get("client_order_id"),
                    sequence,
                    json.dumps(snapshot, sort_keys=True),
                ),
            )
            return
        if kind == "risk_decision":
            order_id = str(payload["order_id"])
            snapshot = self._order_snapshot_locked(owner_id, order_id)
            if snapshot is None:
                return
            approved = bool(payload.get("approved"))
            status = "approved" if approved else "rejected"
            snapshot.update(
                status=status,
                lifecycle_status=canonical_paper_status(status).value,
                status_reason=str(payload["reason"]),
                approved_quantity=payload.get("approved_quantity", 0),
            )
            self._update_order_locked(owner_id, order_id, sequence, snapshot)
            return
        if kind == "order_status":
            order_id = str(payload["order_id"])
            snapshot = self._order_snapshot_locked(owner_id, order_id)
            if snapshot is None:
                return
            status = str(payload["status"])
            snapshot.update(
                status=status,
                lifecycle_status=canonical_paper_status(status).value,
                status_reason=str(payload["reason"]),
            )
            self._update_order_locked(owner_id, order_id, sequence, snapshot)
            return
        if kind == "fill":
            order_id = str(payload["order_id"])
            fill_id = str(payload["fill_id"])
            self.connection.execute(
                "INSERT INTO paper_fill_read_model("
                "owner_user_id, fill_id, order_id, sequence, payload_json"
                ") VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(owner_user_id, fill_id) DO UPDATE SET "
                "order_id=excluded.order_id, sequence=excluded.sequence, "
                "payload_json=excluded.payload_json",
                (
                    owner_id, fill_id, order_id, sequence,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            snapshot = self._order_snapshot_locked(owner_id, order_id)
            if snapshot is not None:
                snapshot.update(
                    status="filled",
                    lifecycle_status=canonical_paper_status("filled").value,
                    status_reason="simulated_fill",
                    fill_id=fill_id,
                )
                self._update_order_locked(owner_id, order_id, sequence, snapshot)
            return
        if kind == "position":
            previous_row = self.connection.execute(
                "SELECT payload_json FROM paper_position_read_model WHERE "
                "owner_user_id=? AND strategy_id=? AND strategy_version=? "
                "AND symbol=? AND contract=?",
                (
                    owner_id,
                    str(payload["strategy_id"]),
                    int(payload["strategy_version"]),
                    str(payload["symbol"]),
                    str(payload["contract"]),
                ),
            ).fetchone()
            previous = (
                json.loads(str(previous_row["payload_json"]))
                if previous_row else None
            )
            quantity = int(payload["quantity"])
            previous_quantity = int(previous.get("quantity", 0)) if previous else 0
            same_open_position = (
                quantity != 0
                and previous_quantity != 0
                and (quantity > 0) == (previous_quantity > 0)
            )
            meta = payload["meta"]
            if not isinstance(meta, dict):
                raise ValueError("position metadata is missing")
            snapshot = {
                "owner_id": owner_id,
                "strategy_id": payload["strategy_id"],
                "strategy_version": payload["strategy_version"],
                "symbol": payload["symbol"],
                "contract": payload["contract"],
                "quantity": quantity,
                "average_price": payload["average_price"],
                "opened_at": (
                    previous.get("opened_at")
                    if same_open_position and previous is not None
                    else datetime.fromisoformat(
                        str(meta["occurred_at"])
                    ).isoformat(timespec="milliseconds")
                    if quantity != 0 else None
                ),
                "realized_pnl": payload["realized_pnl"],
                "unrealized_pnl": payload["unrealized_pnl"],
                "total_cost": payload.get("total_cost", 0.0),
                "order_source": payload.get("order_source", "manual"),
                "runtime_id": payload.get("runtime_id"),
                "decision_id": payload.get("decision_id"),
                "entry_fill_price": payload.get("entry_fill_price"),
                "stop_loss_price": payload.get("stop_loss_price"),
                "take_profit_price": payload.get("take_profit_price"),
                "strategy_snapshot": payload.get("strategy_snapshot"),
            }
            self.connection.execute(
                "INSERT INTO paper_position_read_model("
                "owner_user_id, strategy_id, strategy_version, symbol, contract, "
                "sequence, quantity, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT("
                "owner_user_id, strategy_id, strategy_version, symbol, contract"
                ") DO UPDATE SET sequence=excluded.sequence, "
                "quantity=excluded.quantity, "
                "payload_json=excluded.payload_json",
                (
                    owner_id,
                    str(payload["strategy_id"]),
                    int(payload["strategy_version"]),
                    str(payload["symbol"]),
                    str(payload["contract"]),
                    sequence,
                    quantity,
                    json.dumps(snapshot, sort_keys=True),
                ),
            )

    def _order_snapshot_locked(
        self, owner_id: str, order_id: str
    ) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT snapshot_json FROM paper_order_read_model "
            "WHERE owner_user_id=? AND order_id=?",
            (owner_id, order_id),
        ).fetchone()
        return json.loads(str(row["snapshot_json"])) if row else None

    def _update_order_locked(
        self,
        owner_id: str,
        order_id: str,
        sequence: int,
        snapshot: dict[str, object],
    ) -> None:
        self.connection.execute(
            "UPDATE paper_order_read_model SET sequence=?, snapshot_json=? "
            "WHERE owner_user_id=? AND order_id=?",
            (sequence, json.dumps(snapshot, sort_keys=True), owner_id, order_id),
        )

    def _record_write(self, started: float) -> None:
        elapsed_ms = (perf_counter() - started) * 1_000
        self.write_count += 1
        self.total_write_ms += elapsed_ms
        self.max_write_ms = max(self.max_write_ms, elapsed_ms)
        self.last_write_ms = elapsed_ms

    def order_for_key(self, owner_id: str, key: str) -> str | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT order_id FROM paper_idempotency "
                "WHERE owner_user_id=? AND idempotency_key=?",
                (owner_id, key),
            ).fetchone()
        return str(row["order_id"]) if row else None

    def reserve_key(self, owner_id: str, key: str, order_id: str) -> str:
        started = perf_counter()
        with self.lock:
            self.connection.execute(
                "INSERT OR IGNORE INTO paper_idempotency VALUES (?, ?, ?, ?)",
                (owner_id, key, order_id, _now()),
            )
            row = self.connection.execute(
                "SELECT order_id FROM paper_idempotency "
                "WHERE owner_user_id=? AND idempotency_key=?",
                (owner_id, key),
            ).fetchone()
            self.connection.commit()
        self._record_write(started)
        assert row is not None
        return str(row["order_id"])

    def append_events(self, events: list[DomainEvent]) -> None:
        rows = []
        for event in events:
            owner_id = event.meta.owner_id
            if not owner_id:
                continue
            rows.append(
                (
                    event.meta.event_id,
                    owner_id,
                    event.kind,
                    event.meta.occurred_at.isoformat(timespec="microseconds"),
                    json.dumps(event_to_dict(event), sort_keys=True),
                    _now(),
                )
            )
        if not rows:
            return
        started = perf_counter()
        with self.lock:
            self.connection.executemany(
                "INSERT OR IGNORE INTO paper_events(" 
                "event_id, owner_user_id, kind, occurred_at, payload_json, recorded_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._sync_read_models_locked()
            self.connection.commit()
        self._record_write(started)

    def events(self, owner_id: str, limit: int = 500) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT sequence, payload_json FROM paper_events "
                "WHERE owner_user_id=? ORDER BY sequence DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        return [
            {"sequence": int(row["sequence"]), **json.loads(row["payload_json"])}
            for row in rows
        ]

    def recovery_records(self) -> list[dict[str, object]]:
        """Return events and operator controls in durable write order."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT 'event' AS record_type, sequence, owner_user_id, "
                "payload_json, NULL AS action, NULL AS reason, occurred_at, "
                "recorded_at FROM paper_events "
                "UNION ALL "
                "SELECT 'control' AS record_type, sequence, owner_user_id, "
                "NULL AS payload_json, action, reason, occurred_at, recorded_at "
                "FROM paper_controls "
                "ORDER BY recorded_at, record_type, sequence"
            ).fetchall()
        records: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            payload = item.pop("payload_json")
            if payload is not None:
                try:
                    item["payload"] = json.loads(str(payload))
                except json.JSONDecodeError:
                    item["payload"] = None
                    item["parse_error"] = True
            records.append(item)
        return records

    def dangling_idempotency(self) -> list[dict[str, str]]:
        """Find reserved request keys whose order intent was never committed."""
        with self.lock:
            rows = self.connection.execute(
                "SELECT key_row.owner_user_id, key_row.idempotency_key, "
                "key_row.order_id FROM paper_idempotency AS key_row "
                "LEFT JOIN paper_events AS event_row "
                "ON event_row.owner_user_id=key_row.owner_user_id "
                "AND event_row.event_id=key_row.order_id "
                "AND event_row.kind='order_intent' "
                "WHERE event_row.event_id IS NULL"
            ).fetchall()
        return [
            {
                "owner_user_id": str(row["owner_user_id"]),
                "idempotency_key": str(row["idempotency_key"]),
                "order_id": str(row["order_id"]),
            }
            for row in rows
        ]

    def stats(self) -> dict[str, object]:
        with self.lock:
            row = self.connection.execute(
                "SELECT COUNT(*) AS events, "
                "COUNT(DISTINCT owner_user_id) AS owners FROM paper_events"
            ).fetchone()
            controls = self.connection.execute(
                "SELECT COUNT(*) AS total FROM paper_controls"
            ).fetchone()
            projections = self.connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM paper_order_read_model) AS orders, "
                "(SELECT COUNT(*) FROM paper_fill_read_model) AS fills, "
                "(SELECT COUNT(*) FROM paper_position_read_model "
                "WHERE quantity != 0) "
                "AS open_positions"
            ).fetchone()
            auto = self.connection.execute(
                "SELECT "
                "SUM(json_extract(snapshot_json, '$.status_reason')="
                "'gap_risk_exceeded') AS gap_rejected, "
                "MAX(json_extract(snapshot_json, '$.submitted_at')) "
                "AS last_auto_order_time "
                "FROM paper_order_read_model WHERE "
                "json_extract(snapshot_json, '$.order_source')='strategy_auto'"
            ).fetchone()
        assert (
            row is not None and controls is not None
            and projections is not None and auto is not None
        )
        return {
            "events": int(row["events"]),
            "owners": int(row["owners"]),
            "controls": int(controls["total"]),
            "read_model_orders": int(projections["orders"]),
            "read_model_fills": int(projections["fills"]),
            "read_model_open_positions": int(projections["open_positions"]),
            "gap_risk_rejected": int(auto["gap_rejected"] or 0),
            "last_auto_order_time": auto["last_auto_order_time"],
            "write_count": self.write_count,
            "average_write_ms": round(
                self.total_write_ms / self.write_count, 3
            ) if self.write_count else None,
            "max_write_ms": round(self.max_write_ms, 3),
            "last_write_ms": round(self.last_write_ms, 3)
            if self.last_write_ms is not None else None,
        }

    def order_snapshot(
        self, owner_id: str, order_id: str
    ) -> dict[str, object] | None:
        with self.lock:
            return self._order_snapshot_locked(owner_id, order_id)

    def order_for_client_id(
        self, owner_id: str, client_order_id: str
    ) -> dict[str, object] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT snapshot_json FROM paper_order_read_model "
                "WHERE owner_user_id=? AND client_order_id=?",
                (owner_id, client_order_id),
            ).fetchone()
        return json.loads(str(row["snapshot_json"])) if row else None

    def orders(self, owner_id: str) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT snapshot_json FROM paper_order_read_model "
                "WHERE owner_user_id=? ORDER BY sequence DESC",
                (owner_id,),
            ).fetchall()
        return [json.loads(str(row["snapshot_json"])) for row in rows]

    def strategy_auto_orders(self) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT owner_user_id,snapshot_json FROM paper_order_read_model "
                "WHERE json_extract(snapshot_json, '$.order_source')="
                "'strategy_auto' ORDER BY sequence"
            ).fetchall()
        return [
            {"owner_user_id": str(row["owner_user_id"]), **json.loads(
                str(row["snapshot_json"])
            )}
            for row in rows
        ]

    def fill_snapshot(
        self, owner_id: str, fill_id: str
    ) -> dict[str, object] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT payload_json FROM paper_fill_read_model "
                "WHERE owner_user_id=? AND fill_id=?",
                (owner_id, fill_id),
            ).fetchone()
        return json.loads(str(row["payload_json"])) if row else None

    def fills(
        self, owner_id: str, limit: int = 500
    ) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT payload_json FROM paper_fill_read_model "
                "WHERE owner_user_id=? ORDER BY sequence DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]

    def positions(self, owner_id: str) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT payload_json FROM paper_position_read_model "
                "WHERE owner_user_id=? ORDER BY sequence DESC",
                (owner_id,),
            ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            if int(payload.get("quantity", 0)) != 0:
                result.append(payload)
        return result

    def strategy_auto_positions(self) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT owner_user_id,payload_json FROM paper_position_read_model "
                "WHERE quantity != 0 AND "
                "json_extract(payload_json, '$.order_source')='strategy_auto' "
                "ORDER BY sequence"
            ).fetchall()
        return [
            {"owner_user_id": str(row["owner_user_id"]), **json.loads(
                str(row["payload_json"])
            )}
            for row in rows
        ]

    def append_control(
        self, owner_id: str, action: str, reason: str, occurred_at: datetime
    ) -> None:
        started = perf_counter()
        with self.lock:
            self.connection.execute(
                "INSERT INTO paper_controls(" 
                "owner_user_id, action, reason, occurred_at, recorded_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    owner_id,
                    action,
                    reason,
                    occurred_at.isoformat(timespec="microseconds"),
                    _now(),
                ),
            )
            self.connection.commit()
        self._record_write(started)

    def controls(self, owner_id: str, limit: int = 100) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT sequence, action, reason, occurred_at FROM paper_controls "
                "WHERE owner_user_id=? ORDER BY sequence DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self.connection.close()

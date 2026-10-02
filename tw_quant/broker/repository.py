from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from .models import (
    BrokerOrder,
    BrokerOrderRequest,
    BrokerOrderStatus,
    ExecutionMode,
)
from .identity import BrokerAccountRef
from .lifecycle import transition_order
from .routing import RoutedBrokerOrder, RoutedBrokerOrderRequest


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _request_json(request: BrokerOrderRequest) -> str:
    payload = asdict(request)
    payload["mode"] = request.mode.value
    return json.dumps(payload, sort_keys=True)


def _request(raw: str) -> BrokerOrderRequest:
    payload = json.loads(raw)
    payload["mode"] = ExecutionMode(payload["mode"])
    return BrokerOrderRequest(**payload)


class SQLiteLiveOrderRepository:
    """Durable order and outbox store. Unknown dispatches are never requeued."""

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
                CREATE TABLE IF NOT EXISTS live_orders (
                    client_order_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    broker_name TEXT,
                    account_id TEXT,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    broker_order_id TEXT,
                    filled_quantity INTEGER NOT NULL DEFAULT 0,
                    average_fill_price REAL,
                    status_reason TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_live_orders_owner_updated
                    ON live_orders(owner_user_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS live_order_outbox (
                    client_order_id TEXT PRIMARY KEY,
                    broker_name TEXT,
                    account_id TEXT,
                    state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(client_order_id)
                        REFERENCES live_orders(client_order_id)
                );
                CREATE TABLE IF NOT EXISTS live_cancel_outbox (
                    client_order_id TEXT PRIMARY KEY,
                    broker_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(client_order_id) REFERENCES live_orders(client_order_id)
                );
                CREATE TABLE IF NOT EXISTS live_fills (
                    broker_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    fill_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    broker_order_id TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY(broker_name, account_id, fill_id)
                );
                CREATE TABLE IF NOT EXISTS live_positions (
                    owner_user_id TEXT NOT NULL,
                    broker_name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_user_id, broker_name, account_id, contract)
                );
                """
            )
            order_columns = {
                str(row["name"])
                for row in self.connection.execute(
                    "PRAGMA table_info(live_orders)"
                ).fetchall()
            }
            outbox_columns = {
                str(row["name"])
                for row in self.connection.execute(
                    "PRAGMA table_info(live_order_outbox)"
                ).fetchall()
            }
            if "broker_name" not in order_columns:
                self.connection.execute(
                    "ALTER TABLE live_orders ADD COLUMN broker_name TEXT"
                )
            if "account_id" not in order_columns:
                self.connection.execute(
                    "ALTER TABLE live_orders ADD COLUMN account_id TEXT"
                )
            if "broker_name" not in outbox_columns:
                self.connection.execute(
                    "ALTER TABLE live_order_outbox ADD COLUMN broker_name TEXT"
                )
            if "account_id" not in outbox_columns:
                self.connection.execute(
                    "ALTER TABLE live_order_outbox ADD COLUMN account_id TEXT"
                )
            self.connection.executescript(
                """
                DROP INDEX IF EXISTS idx_live_orders_broker_order;
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_live_orders_target_broker_order
                    ON live_orders(broker_name, account_id, broker_order_id)
                    WHERE broker_name IS NOT NULL
                      AND account_id IS NOT NULL
                      AND broker_order_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_live_outbox_target_state
                    ON live_order_outbox(
                        broker_name, account_id, state, created_at
                    );
                CREATE INDEX IF NOT EXISTS idx_live_cancel_target_state
                    ON live_cancel_outbox(
                        broker_name, account_id, state, created_at
                    );
                UPDATE live_order_outbox
                   SET state='blocked'
                 WHERE state IN ('pending', 'processing')
                   AND (broker_name IS NULL OR account_id IS NULL);
                """
            )
            self.connection.commit()

    @staticmethod
    def _order(row: sqlite3.Row) -> BrokerOrder:
        return BrokerOrder(
            request=_request(str(row["request_json"])),
            status=BrokerOrderStatus(str(row["status"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            broker_order_id=(
                str(row["broker_order_id"]) if row["broker_order_id"] else None
            ),
            filled_quantity=int(row["filled_quantity"]),
            average_fill_price=(
                float(row["average_fill_price"])
                if row["average_fill_price"] is not None else None
            ),
            status_reason=(
                str(row["status_reason"]) if row["status_reason"] else None
            ),
        )

    def reserve(
        self,
        routed_request: RoutedBrokerOrderRequest,
        *,
        occurred_at: datetime | None = None,
    ) -> tuple[BrokerOrder, bool]:
        request = routed_request.request
        target = routed_request.target
        if request.mode is not ExecutionMode.LIVE:
            raise ValueError("live order repository only accepts live orders")
        now = occurred_at or _utc_now()
        order = BrokerOrder(
            request=request,
            status=BrokerOrderStatus.RISK_APPROVED,
            updated_at=now,
            status_reason="durably_reserved",
        )
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO live_orders "
                "(client_order_id, owner_user_id, broker_name, account_id, "
                "request_json, status, broker_order_id, filled_quantity, "
                "average_fill_price, status_reason, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request.client_order_id,
                    request.owner_id,
                    target.broker_name,
                    target.account_id,
                    _request_json(request),
                    order.status.value,
                    None,
                    0,
                    None,
                    order.status_reason,
                    now.isoformat(timespec="microseconds"),
                ),
            )
            created = cursor.rowcount == 1
            if created:
                self.connection.execute(
                    "INSERT INTO live_order_outbox "
                    "(client_order_id, broker_name, account_id, state, "
                    "attempt_count, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'pending', 0, ?, ?)",
                    (
                        request.client_order_id,
                        target.broker_name,
                        target.account_id,
                        now.isoformat(timespec="microseconds"),
                        now.isoformat(timespec="microseconds"),
                    ),
                )
            row = self.connection.execute(
                "SELECT * FROM live_orders WHERE client_order_id=?",
                (request.client_order_id,),
            ).fetchone()
            self.connection.commit()
        assert row is not None
        existing = self._order(row)
        if existing.request.owner_id != request.owner_id:
            raise ValueError("client_order_id already belongs to another owner")
        existing_target = self._target(row)
        if existing_target != target:
            raise ValueError("client_order_id already belongs to another target")
        return existing, created

    @staticmethod
    def _target(row: sqlite3.Row) -> BrokerAccountRef | None:
        broker_name = row["broker_name"]
        account_id = row["account_id"]
        if broker_name is None or account_id is None:
            return None
        return BrokerAccountRef(str(broker_name), str(account_id))

    def get(self, owner_id: str, client_order_id: str) -> BrokerOrder | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM live_orders WHERE owner_user_id=? AND client_order_id=?",
                (owner_id, client_order_id),
            ).fetchone()
        return self._order(row) if row else None

    def get_routed(
        self, owner_id: str, client_order_id: str
    ) -> RoutedBrokerOrder | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM live_orders "
                "WHERE owner_user_id=? AND client_order_id=?",
                (owner_id, client_order_id),
            ).fetchone()
        if row is None:
            return None
        target = self._target(row)
        if target is None:
            return None
        return RoutedBrokerOrder(target, self._order(row))

    def get_by_broker_order_id(
        self, target: BrokerAccountRef, broker_order_id: str
    ) -> BrokerOrder | None:
        if not broker_order_id.strip():
            raise ValueError("broker_order_id is required")
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM live_orders WHERE broker_name=? AND account_id=? "
                "AND broker_order_id=?",
                (target.broker_name, target.account_id, broker_order_id),
            ).fetchone()
        return self._order(row) if row else None

    def orders(
        self,
        owner_id: str | None = None,
        *,
        target: BrokerAccountRef | None = None,
    ) -> list[BrokerOrder]:
        query = "SELECT * FROM live_orders"
        clauses: list[str] = []
        parameters: tuple[object, ...] = ()
        if owner_id is not None:
            clauses.append("owner_user_id=?")
            parameters = (owner_id,)
        if target is not None:
            clauses.extend(("broker_name=?", "account_id=?"))
            parameters += (target.broker_name, target.account_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at, client_order_id"
        with self.lock:
            rows = self.connection.execute(query, parameters).fetchall()
        return [self._order(row) for row in rows]

    def reconciliation_candidates(
        self, target: BrokerAccountRef, owner_id: str | None = None
    ) -> list[BrokerOrder]:
        statuses = (
            BrokerOrderStatus.SUBMITTING,
            BrokerOrderStatus.ACCEPTED,
            BrokerOrderStatus.PARTIALLY_FILLED,
            BrokerOrderStatus.CANCEL_PENDING,
            BrokerOrderStatus.UNKNOWN,
        )
        placeholders = ", ".join("?" for _ in statuses)
        query = (
            f"SELECT * FROM live_orders WHERE status IN ({placeholders}) "
            "AND broker_name=? AND account_id=?"
        )
        parameters: tuple[object, ...] = (
            *tuple(status.value for status in statuses),
            target.broker_name,
            target.account_id,
        )
        if owner_id is not None:
            query += " AND owner_user_id=?"
            parameters += (owner_id,)
        query += " ORDER BY updated_at, client_order_id"
        with self.lock:
            rows = self.connection.execute(query, parameters).fetchall()
        return [self._order(row) for row in rows]

    def claim_next(self, target: BrokerAccountRef) -> RoutedBrokerOrder | None:
        now = _utc_now().isoformat(timespec="microseconds")
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT orders.* FROM live_order_outbox AS outbox "
                "JOIN live_orders AS orders USING(client_order_id) "
                "WHERE outbox.state='pending' "
                "AND outbox.broker_name IS NOT NULL "
                "AND outbox.account_id IS NOT NULL "
                "AND orders.broker_name=outbox.broker_name "
                "AND orders.account_id=outbox.account_id "
                "AND outbox.broker_name=? AND outbox.account_id=? "
                "ORDER BY outbox.created_at LIMIT 1",
                (target.broker_name, target.account_id),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            self.connection.execute(
                "UPDATE live_order_outbox SET state='processing', "
                "attempt_count=attempt_count+1, updated_at=? WHERE client_order_id=?",
                (now, row["client_order_id"]),
            )
            self.connection.commit()
        stored_target = self._target(row)
        assert stored_target is not None
        return RoutedBrokerOrder(stored_target, self._order(row))

    def finish_dispatch(self, order: BrokerOrder) -> None:
        outbox_state = (
            "blocked" if order.status is BrokerOrderStatus.UNKNOWN else "completed"
        )
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            self._update_order(order)
            self.connection.execute(
                "UPDATE live_order_outbox SET state=?, updated_at=? "
                "WHERE client_order_id=?",
                (
                    outbox_state,
                    order.updated_at.isoformat(timespec="microseconds"),
                    order.request.client_order_id,
                ),
            )
            self.connection.commit()

    def block_dispatch(self, order: BrokerOrder, reason: str) -> None:
        """Block pre-submit work without claiming an external broker outcome."""

        blocked = replace(order, status_reason=reason, updated_at=_utc_now())
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            self._update_order(blocked)
            self.connection.execute(
                "UPDATE live_order_outbox SET state='blocked', updated_at=? "
                "WHERE client_order_id=?",
                (
                    blocked.updated_at.isoformat(timespec="microseconds"),
                    blocked.request.client_order_id,
                ),
            )
            self.connection.commit()

    def block_pending_dispatches(
        self, target: BrokerAccountRef, reason: str
    ) -> int:
        """Restart safety: pending external work requires a fresh manual action."""

        now = _utc_now()
        with self.lock:
            rows = self.connection.execute(
                "SELECT orders.* FROM live_order_outbox AS outbox "
                "JOIN live_orders AS orders USING(client_order_id) "
                "WHERE outbox.state='pending' AND outbox.broker_name=? "
                "AND outbox.account_id=?",
                (target.broker_name, target.account_id),
            ).fetchall()
            cancel_rows = self.connection.execute(
                "SELECT orders.* FROM live_cancel_outbox AS outbox "
                "JOIN live_orders AS orders USING(client_order_id) "
                "WHERE outbox.state='pending' AND outbox.broker_name=? "
                "AND outbox.account_id=?",
                (target.broker_name, target.account_id),
            ).fetchall()
            for row in (*rows, *cancel_rows):
                self._update_order(replace(
                    self._order(row), status_reason=reason, updated_at=now
                ))
            self.connection.execute(
                "UPDATE live_order_outbox SET state='blocked', updated_at=? "
                "WHERE state='pending' AND broker_name=? AND account_id=?",
                (
                    now.isoformat(timespec="microseconds"),
                    target.broker_name,
                    target.account_id,
                ),
            )
            self.connection.execute(
                "UPDATE live_cancel_outbox SET state='blocked', updated_at=? "
                "WHERE state='pending' AND broker_name=? AND account_id=?",
                (
                    now.isoformat(timespec="microseconds"),
                    target.broker_name,
                    target.account_id,
                ),
            )
            self.connection.commit()
        return len(rows) + len(cancel_rows)

    def reserve_cancel(
        self, target: BrokerAccountRef, owner_id: str, client_order_id: str
    ) -> tuple[BrokerOrder, bool]:
        now = _utc_now()
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT * FROM live_orders WHERE owner_user_id=? "
                "AND client_order_id=? AND broker_name=? AND account_id=?",
                (owner_id, client_order_id, target.broker_name, target.account_id),
            ).fetchone()
            if row is None:
                self.connection.rollback()
                raise KeyError("unknown platform live order")
            order = self._order(row)
            if order.status.terminal:
                self.connection.commit()
                return order, False
            if not order.broker_order_id:
                self.connection.rollback()
                raise RuntimeError("live_order_requires_reconciliation_before_cancel")
            existing = self.connection.execute(
                "SELECT state FROM live_cancel_outbox WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
            if existing is not None:
                self.connection.commit()
                return order, False
            pending = transition_order(
                order,
                BrokerOrderStatus.CANCEL_PENDING,
                updated_at=max(now, order.updated_at),
                status_reason="cancel_durably_reserved",
            )
            self._update_order(pending)
            self.connection.execute(
                "INSERT INTO live_cancel_outbox VALUES (?,?,?,'pending',0,?,?)",
                (
                    client_order_id,
                    target.broker_name,
                    target.account_id,
                    now.isoformat(timespec="microseconds"),
                    now.isoformat(timespec="microseconds"),
                ),
            )
            self.connection.commit()
        return pending, True

    def claim_next_cancel(
        self, target: BrokerAccountRef
    ) -> RoutedBrokerOrder | None:
        now = _utc_now()
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT orders.* FROM live_cancel_outbox AS outbox "
                "JOIN live_orders AS orders USING(client_order_id) "
                "WHERE outbox.state='pending' AND outbox.broker_name=? "
                "AND outbox.account_id=? ORDER BY outbox.created_at LIMIT 1",
                (target.broker_name, target.account_id),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            self.connection.execute(
                "UPDATE live_cancel_outbox SET state='processing', "
                "attempt_count=attempt_count+1,updated_at=? WHERE client_order_id=?",
                (now.isoformat(timespec="microseconds"), row["client_order_id"]),
            )
            self.connection.commit()
        stored_target = self._target(row)
        assert stored_target is not None
        return RoutedBrokerOrder(stored_target, self._order(row))

    def finish_cancel(self, order: BrokerOrder) -> None:
        state = "blocked" if order.status is BrokerOrderStatus.UNKNOWN else "completed"
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            self._update_order(order)
            self.connection.execute(
                "UPDATE live_cancel_outbox SET state=?,updated_at=? "
                "WHERE client_order_id=?",
                (
                    state,
                    order.updated_at.isoformat(timespec="microseconds"),
                    order.request.client_order_id,
                ),
            )
            self.connection.commit()

    def block_cancel(self, order: BrokerOrder, reason: str) -> None:
        blocked = replace(order, status_reason=reason, updated_at=_utc_now())
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            self._update_order(blocked)
            self.connection.execute(
                "UPDATE live_cancel_outbox SET state='blocked',updated_at=? "
                "WHERE client_order_id=?",
                (
                    blocked.updated_at.isoformat(timespec="microseconds"),
                    blocked.request.client_order_id,
                ),
            )
            self.connection.commit()

    def save_reconciliation(self, order: BrokerOrder) -> None:
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            self._update_order(order)
            if order.status is not BrokerOrderStatus.UNKNOWN:
                self.connection.execute(
                    "UPDATE live_order_outbox SET state='resolved', updated_at=? "
                    "WHERE client_order_id=? AND state='blocked'",
                    (
                        order.updated_at.isoformat(timespec="microseconds"),
                        order.request.client_order_id,
                    ),
                )
            self.connection.commit()

    def apply_reconciled_snapshot(self, target, snapshot) -> None:
        """Persist only broker fills, then derive the isolated Live position ledger."""

        if snapshot.account_ref != target:
            raise ValueError("broker snapshot target mismatch")
        now = snapshot.captured_at.isoformat(timespec="microseconds")
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            for fill in snapshot.fills:
                row = self.connection.execute(
                    "SELECT owner_user_id FROM live_orders WHERE broker_name=? "
                    "AND account_id=? AND broker_order_id=?",
                    (target.broker_name, target.account_id, fill.broker_order_id),
                ).fetchone()
                if row is None:
                    self.connection.rollback()
                    raise RuntimeError("orphan_broker_fill")
                self.connection.execute(
                    "INSERT OR IGNORE INTO live_fills VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        target.broker_name,
                        target.account_id,
                        fill.fill_id,
                        str(row["owner_user_id"]),
                        fill.broker_order_id,
                        fill.contract,
                        fill.side,
                        fill.quantity,
                        fill.price,
                        fill.occurred_at.isoformat(timespec="microseconds"),
                    ),
                )
            owners = self.connection.execute(
                "SELECT DISTINCT owner_user_id,contract FROM live_fills "
                "WHERE broker_name=? AND account_id=?",
                (target.broker_name, target.account_id),
            ).fetchall()
            for item in owners:
                total = self.connection.execute(
                    "SELECT COALESCE(SUM(CASE side WHEN 'buy' THEN quantity "
                    "ELSE -quantity END),0) FROM live_fills WHERE owner_user_id=? "
                    "AND broker_name=? AND account_id=? AND contract=?",
                    (
                        item["owner_user_id"],
                        target.broker_name,
                        target.account_id,
                        item["contract"],
                    ),
                ).fetchone()[0]
                self.connection.execute(
                    "INSERT INTO live_positions VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(owner_user_id,broker_name,account_id,contract) "
                    "DO UPDATE SET quantity=excluded.quantity,updated_at=excluded.updated_at",
                    (
                        item["owner_user_id"],
                        target.broker_name,
                        target.account_id,
                        item["contract"],
                        int(total),
                        now,
                    ),
                )
            self.connection.commit()

    def live_positions(
        self, owner_id: str, target: BrokerAccountRef
    ) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT contract,quantity,updated_at FROM live_positions "
                "WHERE owner_user_id=? AND broker_name=? AND account_id=? "
                "ORDER BY contract",
                (owner_id, target.broker_name, target.account_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def _update_order(self, order: BrokerOrder) -> None:
        self.connection.execute(
            "UPDATE live_orders SET status=?, broker_order_id=?, "
            "filled_quantity=?, average_fill_price=?, status_reason=?, updated_at=? "
            "WHERE client_order_id=? AND owner_user_id=?",
            (
                order.status.value,
                order.broker_order_id,
                order.filled_quantity,
                order.average_fill_price,
                order.status_reason,
                order.updated_at.isoformat(timespec="microseconds"),
                order.request.client_order_id,
                order.request.owner_id,
            ),
        )

    def recover_interrupted_dispatches(self) -> int:
        """Block crash-interrupted work; a broker lookup must resolve it."""
        now = _utc_now()
        with self.lock:
            rows = self.connection.execute(
                "SELECT orders.* FROM live_order_outbox AS outbox "
                "JOIN live_orders AS orders USING(client_order_id) "
                "WHERE outbox.state='processing'"
            ).fetchall()
            cancel_rows = self.connection.execute(
                "SELECT orders.* FROM live_cancel_outbox AS outbox "
                "JOIN live_orders AS orders USING(client_order_id) "
                "WHERE outbox.state='processing'"
            ).fetchall()
            for table, interrupted in (
                ("live_order_outbox", rows),
                ("live_cancel_outbox", cancel_rows),
            ):
                for row in interrupted:
                    order = self._order(row)
                    unknown = BrokerOrder(
                        request=order.request,
                        status=BrokerOrderStatus.UNKNOWN,
                        updated_at=now,
                        broker_order_id=order.broker_order_id,
                        filled_quantity=order.filled_quantity,
                        average_fill_price=order.average_fill_price,
                        status_reason="dispatch_interrupted_reconciliation_required",
                    )
                    self._update_order(unknown)
                    self.connection.execute(
                        f"UPDATE {table} SET state='blocked', updated_at=? "
                        "WHERE client_order_id=?",
                        (
                            now.isoformat(timespec="microseconds"),
                            order.request.client_order_id,
                        ),
                    )
            self.connection.commit()
        return len(rows) + len(cancel_rows)

    def outbox_state(self, client_order_id: str) -> str | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT state FROM live_order_outbox WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
        return str(row["state"]) if row else None

    def cancel_outbox_state(self, client_order_id: str) -> str | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT state FROM live_cancel_outbox WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
        return str(row["state"]) if row else None

    def close(self) -> None:
        self.connection.close()

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Protocol

from .identity import BrokerAccountRef
from .models import BrokerOrderStatus
from .reconciliation import (
    BrokerFillSnapshot,
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
    BrokerReconciliationSnapshot,
)


class BrokerTruthStore(Protocol):
    def save(
        self, target: BrokerAccountRef, snapshot: BrokerReconciliationSnapshot
    ) -> None: ...

    def get(self, target: BrokerAccountRef) -> BrokerReconciliationSnapshot | None: ...

    def targets(self) -> tuple[BrokerAccountRef, ...]: ...


def _payload(snapshot: BrokerReconciliationSnapshot) -> str:
    return json.dumps({
        "broker_name": snapshot.broker_name,
        "account_id": snapshot.account_id,
        "captured_at": snapshot.captured_at.isoformat(timespec="microseconds"),
        "orders": [
            {
                "broker_order_id": item.broker_order_id,
                "status": item.status.value,
                "filled_quantity": item.filled_quantity,
            }
            for item in snapshot.orders
        ],
        "fills": [
            {
                "fill_id": item.fill_id,
                "broker_order_id": item.broker_order_id,
                "contract": item.contract,
                "side": item.side,
                "quantity": item.quantity,
                "price": item.price,
                "occurred_at": item.occurred_at.isoformat(timespec="microseconds"),
            }
            for item in snapshot.fills
        ],
        "positions": [
            {"contract": item.contract, "quantity": item.quantity}
            for item in snapshot.positions
        ],
    }, sort_keys=True, separators=(",", ":"))


def _snapshot(raw: str) -> BrokerReconciliationSnapshot:
    value = json.loads(raw)
    return BrokerReconciliationSnapshot(
        broker_name=str(value["broker_name"]),
        account_id=str(value["account_id"]),
        captured_at=datetime.fromisoformat(str(value["captured_at"])),
        orders=tuple(
            BrokerOrderSnapshot(
                broker_order_id=item.get("broker_order_id"),
                status=BrokerOrderStatus(str(item["status"])),
                filled_quantity=int(item["filled_quantity"]),
            )
            for item in value["orders"]
        ),
        fills=tuple(
            BrokerFillSnapshot(
                fill_id=str(item["fill_id"]),
                broker_order_id=str(item["broker_order_id"]),
                contract=str(item["contract"]),
                side=str(item["side"]),  # type: ignore[arg-type]
                quantity=int(item["quantity"]),
                price=float(item["price"]),
                occurred_at=datetime.fromisoformat(str(item["occurred_at"])),
            )
            for item in value["fills"]
        ),
        positions=tuple(
            BrokerPositionSnapshot(str(item["contract"]), int(item["quantity"]))
            for item in value["positions"]
        ),
    )


class SQLiteBrokerTruthRepository:
    """Durable, sanitized broker truth shared across the process boundary."""

    def __init__(self, path: str | Path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(target, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = Lock()
        with self.lock:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS live_broker_truth_snapshots ("
                "broker_name TEXT NOT NULL, account_id TEXT NOT NULL, "
                "captured_at TEXT NOT NULL, snapshot_json TEXT NOT NULL, "
                "PRIMARY KEY(broker_name, account_id))"
            )
            self.connection.commit()

    def save(self, target, snapshot):
        if snapshot.account_ref != target:
            raise ValueError("broker truth identity mismatch")
        with self.lock:
            self.connection.execute(
                "INSERT INTO live_broker_truth_snapshots VALUES (?,?,?,?) "
                "ON CONFLICT(broker_name,account_id) DO UPDATE SET "
                "captured_at=excluded.captured_at,snapshot_json=excluded.snapshot_json",
                (
                    target.broker_name,
                    target.account_id,
                    snapshot.captured_at.isoformat(timespec="microseconds"),
                    _payload(snapshot),
                ),
            )
            self.connection.commit()

    def get(self, target):
        with self.lock:
            row = self.connection.execute(
                "SELECT snapshot_json FROM live_broker_truth_snapshots "
                "WHERE broker_name=? AND account_id=?",
                (target.broker_name, target.account_id),
            ).fetchone()
        return _snapshot(str(row["snapshot_json"])) if row else None

    def targets(self):
        with self.lock:
            rows = self.connection.execute(
                "SELECT broker_name,account_id FROM live_broker_truth_snapshots "
                "ORDER BY broker_name,account_id"
            ).fetchall()
        return tuple(BrokerAccountRef(str(row[0]), str(row[1])) for row in rows)

    def close(self) -> None:
        with self.lock:
            self.connection.close()

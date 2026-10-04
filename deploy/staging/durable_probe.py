#!/usr/bin/env python3
"""Synthetic locked target continuity only; never enable a broker or live recovery."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

TARGET = "exec_P8SyntheticLocked0001"
PATH = "/data/staging.sqlite3"


def snapshot(path=None):
    path = PATH if path is None else path
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("sqlite integrity failure")
        rows = connection.execute("SELECT * FROM execution_targets ORDER BY target_id").fetchall()
        columns = [r[1] for r in connection.execute("PRAGMA table_info(execution_targets)")]
        if len(rows) != 1:
            raise ValueError("expected only the synthetic locked target")
        row = dict(zip(columns, rows[0]))
        expected = {"target_id": TARGET, "owner_user_id": "p8-synthetic-owner",
                    "broker_name": "disabled", "account_id": "P8-SYNTHETIC",
                    "secret_ref": "unavailable:p8-synthetic", "status": "locked"}
        if any(row.get(k) != v for k, v in expected.items()):
            raise ValueError("synthetic target drift")
        return {"locked_target_sha256": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest(),
                "sqlite_integrity": "ok", "target_count": 1, "active_targets": 0}
    finally:
        connection.close()


def seed():
    # Called only with the staging volume in an isolated network-none container.
    if os.environ.get("BROKER_PROVIDER") != "disabled" or os.environ.get("LIVE_TRADING_ENABLED") != "false":
        raise ValueError("synthetic fixture requires disabled execution")
    from tw_quant.broker.execution_target_repository import SQLiteExecutionTargetRepository
    from tw_quant.broker.execution_targets import ExecutionTarget, ExecutionTargetStatus, mask_account_id
    repository = SQLiteExecutionTargetRepository(PATH)
    try:
        if not repository.list_all():
            repository.create(ExecutionTarget(target_id=TARGET, owner_user_id="p8-synthetic-owner",
                broker_name="disabled", account_id="P8-SYNTHETIC", masked_account_id=mask_account_id("P8-SYNTHETIC"),
                secret_ref="unavailable:p8-synthetic", status=ExecutionTargetStatus.LOCKED))
    finally:
        repository.close()
    return snapshot()


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ("seed", "snapshot"):
            raise ValueError("unknown operation")
        print(json.dumps(seed() if sys.argv[1] == "seed" else snapshot(), sort_keys=True))
    except Exception:
        raise SystemExit("P8_DURABLE_STATE=FAIL")

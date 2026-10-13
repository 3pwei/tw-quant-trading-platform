#!/usr/bin/env python3
"""Synthetic locked target continuity only; never enable a broker or live recovery."""
import sys
from time import monotonic_ns

DIAGNOSTICS = __name__ == "__main__" and sys.argv[1:] == ["snapshot"]


def diagnostic(status, step, elapsed_ms, category="none", sqlite_errorcode=None):
    if DIAGNOSTICS:
        code = "" if sqlite_errorcode is None else " sqlite_errorcode=" + str(sqlite_errorcode)
        print("P8_DURABLE_STEP=" + status + " step=" + step +
              " elapsed_ms=" + str(elapsed_ms) + " category=" + category + code,
              file=sys.stderr, flush=True)


_imports_started = monotonic_ns()
diagnostic("START", "python-imports", 0)
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
diagnostic("PASS", "python-imports", (monotonic_ns() - _imports_started) // 1_000_000)

if DIAGNOSTICS:
    try:
        # Private collector handshake, not a PASS marker. Never emit raw /proc text.
        _start_ticks = int(Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19])
        print('P8_PROBE_ID pid=' + str(os.getpid()) + ' start_ticks=' + str(_start_ticks),
              file=sys.stderr, flush=True)
    except (OSError, ValueError, IndexError):
        pass

TARGET = "exec_P8SyntheticLocked0001"
PATH = "/data/staging.sqlite3"


@contextmanager
def probe_step(step):
    started = monotonic_ns()
    diagnostic("START", step, 0)
    try:
        yield
    except Exception as error:
        # Never emit exception text, database paths, SQL or row contents.
        category = "operation-failed"
        sqlite_errorcode = None
        if isinstance(error, sqlite3.Error):
            raw_code = getattr(error, "sqlite_errorcode", None)
            sqlite_errorcode = raw_code if type(raw_code) is int and 0 <= raw_code <= 0x7fffffff else "unknown"
            code = sqlite_errorcode & 0xff if type(sqlite_errorcode) is int else 0
            category = {sqlite3.SQLITE_BUSY: "sqlite-busy", sqlite3.SQLITE_LOCKED: "sqlite-locked"}.get(
                code, "sqlite-error")
        elif isinstance(error, ValueError):
            category = "validation-failed"
        diagnostic("FAIL", step, (monotonic_ns() - started) // 1_000_000, category, sqlite_errorcode)
        raise
    else:
        diagnostic("PASS", step, (monotonic_ns() - started) // 1_000_000)


def snapshot(path=None, *, full_integrity=True):
    path = PATH if path is None else path
    with probe_step("sqlite-connect"):
        connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if full_integrity:
            with probe_step("sqlite-integrity"):
                with probe_step("sqlite-integrity-execute"):
                    cursor = connection.execute("PRAGMA integrity_check")
                with probe_step("sqlite-integrity-fetchone"):
                    integrity = cursor.fetchone()
                if integrity != ("ok",):
                    raise ValueError("sqlite integrity failure")
        with probe_step("targets-query"):
            rows = connection.execute("SELECT * FROM execution_targets ORDER BY target_id").fetchall()
            columns = [r[1] for r in connection.execute("PRAGMA table_info(execution_targets)")]
        with probe_step("targets-validate"):
            if len(rows) != 1:
                raise ValueError("expected only the synthetic locked target")
            row = dict(zip(columns, rows[0]))
            expected = {"target_id": TARGET, "owner_user_id": "p8-synthetic-owner",
                        "broker_name": "disabled", "account_id": "P8-SYNTHETIC",
                        "secret_ref": "unavailable:p8-synthetic", "status": "locked"}
            if any(row.get(k) != v for k, v in expected.items()):
                raise ValueError("synthetic target drift")
        with probe_step("targets-digest"):
            digest = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
        return {"locked_target_sha256": digest,
                "target_count": 1, "active_targets": 0,
                **({"sqlite_integrity": "ok"} if full_integrity else {})}
    finally:
        with probe_step("sqlite-close"):
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
        if len(sys.argv) != 2 or sys.argv[1] not in ("seed", "snapshot", "targets"):
            raise ValueError("unknown operation")
        result = seed() if sys.argv[1] == "seed" else snapshot(full_integrity=sys.argv[1] == "snapshot")
        with probe_step("result"):
            print(json.dumps(result, sort_keys=True))
    except Exception:
        raise SystemExit("P8_DURABLE_STATE=FAIL")

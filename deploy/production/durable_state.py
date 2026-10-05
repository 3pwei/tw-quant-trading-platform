"""Read-only SQLite identity and locked execution continuity; no synthetic seed."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys


def snapshot(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        con.execute('BEGIN')
        if con.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('sqlite-integrity')
        tables = sorted(row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        hashes = {}
        for table in tables:
            if table.startswith('sqlite_'):
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            rows = con.execute('SELECT * FROM ' + quoted).fetchall()
            encoded = sorted(json.dumps(row, default=lambda v: {'bytes': v.hex()}, separators=(',', ':')) for row in rows)
            schema = con.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            hashes[table] = hashlib.sha256(json.dumps([schema, encoded], separators=(',', ':')).encode()).hexdigest()
        if 'execution_targets' not in hashes:
            raise ValueError('durable-lock-missing')
        if con.execute("SELECT count(*) FROM execution_targets WHERE status != 'locked'").fetchone()[0] != 0:
            raise ValueError('target-not-locked')
        durable_names = ('execution_targets', 'live_orders', 'live_order_outbox', 'live_cancel_outbox', 'live_fills',
                         'live_positions', 'live_recovery_lock', 'live_managed_positions', 'live_kill_switch_state')
        return {'sqlite_integrity': 'ok', 'tables': hashes,
                'durable': {k: hashes[k] for k in durable_names if k in hashes}, 'active_targets': 0}
    finally:
        con.close()


if __name__ == '__main__':
    try:
        path = sys.argv[1] if len(sys.argv) == 2 else os.environ['LIVE_EXECUTION_DB_PATH']
        print(json.dumps(snapshot(path), sort_keys=True))
    except Exception:
        raise SystemExit('P9_DURABLE_STATE=FAIL')

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def verify_sqlite(path: str | Path) -> None:
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError(f"SQLite database is missing or empty: {path}")
    connection = sqlite3.connect(path)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if result != ("ok",):
        raise RuntimeError(f"SQLite integrity check failed for {path}: {result!r}")


def backup_sqlite(source_path: str | Path, backup_path: str | Path) -> Path:
    source_path = Path(source_path)
    backup_path = Path(backup_path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if source_path.resolve() == backup_path.resolve():
        raise ValueError("source and backup paths must differ")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_path)
    backup = sqlite3.connect(backup_path)
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
    verify_sqlite(backup_path)
    return backup_path


def restore_sqlite(backup_path: str | Path, target_path: str | Path) -> Path | None:
    """Restore an offline SQLite database and keep a recoverable pre-restore copy."""

    backup_path = Path(backup_path)
    target_path = Path(target_path)
    if backup_path.resolve() == target_path.resolve():
        raise ValueError("backup and target paths must differ")
    verify_sqlite(backup_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    rollback_path: Path | None = None
    if target_path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        rollback_path = target_path.with_name(
            f"{target_path.stem}.pre-restore-{stamp}{target_path.suffix}"
        )
        backup_sqlite(target_path, rollback_path)

    temporary = target_path.with_name(f".{target_path.name}.restore-tmp")
    try:
        backup_sqlite(backup_path, temporary)
        for suffix in ("-wal", "-shm"):
            Path(f"{target_path}{suffix}").unlink(missing_ok=True)
        os.replace(temporary, target_path)
        verify_sqlite(target_path)
    finally:
        temporary.unlink(missing_ok=True)
    return rollback_path

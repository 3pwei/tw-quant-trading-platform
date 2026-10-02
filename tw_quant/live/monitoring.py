from __future__ import annotations

import os
import json
from pathlib import Path
from threading import Lock
from typing import Callable
from datetime import datetime, timezone


_ACCOUNT_HEALTH_FIELDS = frozenset({
    "broker_name", "target_id", "masked_account_id", "status", "client_state",
    "broker_connected", "ca_ready", "read_only", "canary", "callback_registered",
    "ordering_enabled", "locked",
    "recovery_status", "recovery_generation", "issue_codes", "started_at",
    "heartbeat_at", "last_reconciliation_started_at",
    "last_reconciliation_success_at", "last_reconciliation_failure_at",
    "last_reconciliation_duration_ms", "broker_snapshot_age_seconds",
    "last_broker_read_time", "last_local_order_count", "last_broker_order_count",
    "last_broker_fill_count", "last_broker_position_count",
    "callback_queue_size", "callback_queue_capacity",
    "callback_queue_high_watermark", "callbacks_received_total",
    "callbacks_dropped_total", "callback_normalization_failed_total",
    "callbacks_reconciled_total", "callbacks_unmatched_total",
    "callbacks_failed_total", "callback_shutdown_abandoned_total",
    "last_callback_time", "reconciliation_started_total",
    "reconciliation_success_total", "reconciliation_failure_total",
    "reconciliation_skipped_overlap_total", "average_reconciliation_ms",
    "max_reconciliation_ms", "last_error_code", "dispatches",
    "external_order_calls", "external_cancel_calls", "cancel_requests_total",
    "average_broker_response_ms", "max_broker_response_ms",
    "position_guardian",
})


class ExecutionHealthFileMonitor:
    """Read the isolated worker's cached, sanitized health document only."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_age_seconds: float = 30.0,
        now: Callable[[], datetime] | None = None,
    ):
        if max_age_seconds <= 0:
            raise ValueError("execution health max age must be positive")
        self.path = Path(path)
        self.max_age_seconds = max_age_seconds
        self.now = now or (lambda: datetime.now(timezone.utc))

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def snapshot(self) -> dict[str, object]:
        disabled = {
            "state": "disabled",
            "ordering_enabled": False,
            "locked": True,
            "recovery_status": "locked",
            "broker_accounts": [],
            "dispatches": 0,
            "external_order_calls": 0,
            "external_cancel_calls": 0,
        }
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return disabled
        raw_accounts = document.get("broker_accounts", [])
        if not isinstance(raw_accounts, list):
            return disabled
        accounts = [
            {key: value for key, value in item.items() if key in _ACCOUNT_HEALTH_FIELDS}
            for item in raw_accounts
            if isinstance(item, dict)
        ]
        try:
            heartbeat = datetime.fromisoformat(str(document["heartbeat_at"]))
            stale = (self.now() - heartbeat).total_seconds() > self.max_age_seconds
        except (KeyError, TypeError, ValueError):
            stale = True
        if stale:
            accounts = [
                {
                    **item,
                    "status": "locked",
                    "broker_connected": False,
                    "recovery_status": "locked",
                    "issue_codes": list(dict.fromkeys((
                        *(
                            item.get("issue_codes", [])
                            if isinstance(item.get("issue_codes"), list)
                            else []
                        ),
                        "execution_health_stale",
                    ))),
                }
                for item in accounts
            ]
        return {
            "state": (
                "locked" if stale else str(document.get("execution_state") or "locked")
            ),
            "ordering_enabled": bool(document.get("ordering_enabled", False)) and not stale,
            "locked": bool(document.get("locked", True)) or stale,
            "recovery_status": (
                "locked" if stale else str(document.get("recovery_status") or "locked")
            ),
            "broker_accounts": accounts,
            "dispatches": sum(int(item.get("dispatches", 0) or 0) for item in accounts),
            "external_order_calls": sum(int(item.get("external_order_calls", 0) or 0) for item in accounts),
            "external_cancel_calls": sum(int(item.get("external_cancel_calls", 0) or 0) for item in accounts),
        }


class HostResourceMonitor:
    """Read a small, dependency-free host/container resource snapshot.

    CPU is calculated from the delta between two ``/proc/stat`` samples.  The
    first sample intentionally reports ``None`` instead of inventing a rate.
    Memory reflects the container-visible Linux host and disk usage is scoped
    to the filesystem containing the SQLite database.
    """

    def __init__(
        self,
        disk_path: str | Path,
        *,
        read_text: Callable[[str], str] | None = None,
    ) -> None:
        target = Path(disk_path)
        self.disk_path = target if target.is_dir() else target.parent
        self._read_text = read_text or self._default_read_text
        self._previous_cpu: tuple[int, int] | None = None
        self._lock = Lock()

    @staticmethod
    def _default_read_text(path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def _cpu_usage(self) -> float | None:
        fields = self._read_text("/proc/stat").splitlines()[0].split()
        if not fields or fields[0] != "cpu":
            raise ValueError("/proc/stat does not contain aggregate CPU data")
        values = [int(value) for value in fields[1:]]
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        previous = self._previous_cpu
        self._previous_cpu = (total, idle)
        if previous is None:
            return None
        total_delta = total - previous[0]
        idle_delta = idle - previous[1]
        if total_delta <= 0:
            return None
        return round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1)

    def _memory(self) -> tuple[int, int, float]:
        values: dict[str, int] = {}
        for line in self._read_text("/proc/meminfo").splitlines():
            key, _, raw = line.partition(":")
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(raw.strip().split()[0]) * 1024
        total = values["MemTotal"]
        available = values["MemAvailable"]
        used = max(0, total - available)
        percent = round(used / total * 100, 1) if total else 0.0
        return total, used, percent

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            try:
                cpu_percent = self._cpu_usage()
            except (OSError, ValueError, IndexError):
                cpu_percent = None
            try:
                memory_total, memory_used, memory_percent = self._memory()
            except (OSError, ValueError, KeyError):
                memory_total = memory_used = 0
                memory_percent = None
            try:
                disk = os.statvfs(self.disk_path)
                disk_total = disk.f_frsize * disk.f_blocks
                disk_available = disk.f_frsize * disk.f_bavail
                disk_used = max(0, disk_total - disk_available)
                disk_percent = (
                    round(disk_used / disk_total * 100, 1)
                    if disk_total else 0.0
                )
            except OSError:
                disk_total = disk_used = 0
                disk_percent = None
        return {
            "cpu_percent": cpu_percent,
            "cpu_count": os.cpu_count(),
            "memory_total_bytes": memory_total,
            "memory_used_bytes": memory_used,
            "memory_percent": memory_percent,
            "disk_total_bytes": disk_total,
            "disk_used_bytes": disk_used,
            "disk_percent": disk_percent,
            "disk_path": str(self.disk_path),
        }

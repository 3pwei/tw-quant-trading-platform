from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Mapping, Protocol, Sequence

from .audit import BrokerEventAuditStatus, SQLiteBrokerEventAuditRepository
from .callback_consumer import BrokerCallbackConsumer
from .events import BrokerEvent
from .identity import BrokerAccountRef
from .capabilities import BrokerCapabilities
from .instruments import BrokerInstrumentMapper, LockedInstrumentMapper
from .manager import LiveOrderManager
from .ports import BrokerPort, CompositeOrderAdmissionGate, OrderAdmissionGate
from .reconciliation import (
    BrokerReconciliationSource,
    LiveReconciliationService,
    ReconciliationReport,
)
from .recovery import (
    RecoveryOrderGate,
    RecoveryLockStore,
    RecoveryStatus,
    SQLiteRecoveryLockRepository,
)
from .repository import SQLiteLiveOrderRepository
from tw_quant_core.broker.registry import BrokerRegistration, BrokerRegistry


WorkerState = Literal["disabled", "starting", "running", "degraded", "stopped"]


@dataclass(frozen=True)
class ExecutionWorkerSettings:
    dispatch_poll_seconds: float = 0.1
    reconciliation_interval_seconds: float = 30.0
    reconciliation_timeout_seconds: float = 20.0
    snapshot_stale_seconds: float = 120.0
    heartbeat_seconds: float = 5.0
    callback_queue_size: int = 10_000
    shutdown_drain_seconds: float = 5.0

    def __post_init__(self) -> None:
        if min(
            self.dispatch_poll_seconds,
            self.reconciliation_interval_seconds,
            self.reconciliation_timeout_seconds,
            self.snapshot_stale_seconds,
            self.heartbeat_seconds,
            self.shutdown_drain_seconds,
        ) <= 0:
            raise ValueError("execution worker intervals must be positive")
        if not 1 <= self.callback_queue_size <= 100_000:
            raise ValueError("callback_queue_size must be between 1 and 100000")


class ExecutionWorkerMonitor(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def snapshot(self) -> dict[str, object]: ...


class PositionGuardianRuntime(Protocol):
    config: object

    def synchronize(self) -> list[object]: ...
    def evaluate_latest_quotes(self) -> list[BrokerOrder]: ...
    def process_kill_switches(self) -> list[BrokerOrder]: ...
    def mark_order_unknown(self, client_order_id: str) -> bool: ...
    def public_state(self) -> dict[str, object]: ...


class ReadOnlyBrokerClient(Protocol):
    """Lifecycle and cached-state seam owned by one broker account worker."""

    callback_queue: asyncio.Queue[BrokerEvent]

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    def health_state(self) -> Mapping[str, object]: ...


class DisabledExecutionWorker:
    """Production-safe monitor used until execution is explicitly composed."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def snapshot(self) -> dict[str, object]:
        return {
            "state": "disabled",
            "recovery_status": "locked",
            "recovery_issues": ["execution_disabled"],
            "started_at": None,
            "heartbeat_at": None,
            "last_dispatch_at": None,
            "last_reconciliation_at": None,
            "callback_queue_depth": 0,
            "callback_queue_capacity": 0,
            "callback_events": 0,
            "callback_failures": 0,
            "unmatched_callbacks": 0,
            "dropped_callbacks": 0,
            "dispatches": 0,
            "reconciliations": 0,
            "failures": 0,
            "last_error": None,
        }


ReadOnlyWorkerState = Literal[
    "starting",
    "locked",
    "broker_connecting",
    "broker_read_only_ready",
    "reconciling",
    "ready_read_only",
    "degraded",
    "stopped",
]


def _operational_code(exc: Exception, fallback: str) -> str:
    code = getattr(exc, "code", None)
    return str(code) if isinstance(code, str) and code else fallback


class BrokerAccountWorker:
    """Recover and monitor one broker account without an order dispatch path.

    Callback handling and periodic snapshots both delegate broker truth changes
    to the existing callback consumer and ``LiveReconciliationService``. This
    worker only owns lifecycle, scheduling, health and fail-closed transitions.
    """

    def __init__(
        self,
        *,
        account_ref: BrokerAccountRef,
        client: ReadOnlyBrokerClient,
        reconciliation: LiveReconciliationService,
        callback_consumer: BrokerCallbackConsumer,
        recovery_lock: RecoveryLockStore,
        settings: ExecutionWorkerSettings | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if client.callback_queue.maxsize <= 0:
            raise ValueError("broker callback queue must be bounded")
        self.account_ref = account_ref
        self.client = client
        self.reconciliation = reconciliation
        self.callback_consumer = callback_consumer
        self.recovery_lock = recovery_lock
        self.settings = settings or ExecutionWorkerSettings(
            callback_queue_size=client.callback_queue.maxsize
        )
        if self.settings.callback_queue_size != client.callback_queue.maxsize:
            raise ValueError("callback queue capacity does not match worker settings")
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.state: ReadOnlyWorkerState = "stopped"
        self.started_at: datetime | None = None
        self.heartbeat_at: datetime | None = None
        self.last_reconciliation_started_at: datetime | None = None
        self.last_reconciliation_success_at: datetime | None = None
        self.last_reconciliation_failure_at: datetime | None = None
        self.last_reconciliation_duration_ms: float | None = None
        self.last_snapshot_at: datetime | None = None
        self.last_local_order_count: int | None = None
        self.last_broker_order_count: int | None = None
        self.last_broker_fill_count: int | None = None
        self.last_broker_position_count: int | None = None
        self.last_callback_time: datetime | None = None
        self.reconciliation_started_total = 0
        self.reconciliation_success_total = 0
        self.reconciliation_failure_total = 0
        self.reconciliation_skipped_overlap_total = 0
        self._reconciliation_duration_total_ms = 0.0
        self.max_reconciliation_ms = 0.0
        self.callbacks_received_total = 0
        self.callbacks_reconciled_total = 0
        self.callbacks_unmatched_total = 0
        self.callbacks_failed_total = 0
        self.callback_shutdown_abandoned_total = 0
        self._observed_callback_dropped = 0
        self._observed_callback_normalization_failed = 0
        self.last_error_code: str | None = None
        self._stop_event = asyncio.Event()
        self._reconciliation_lock = asyncio.Lock()
        self._callback_task: asyncio.Task[None] | None = None
        self._periodic_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

    @property
    def _task_prefix(self) -> str:
        return f"{self.account_ref.broker_name}-****{self.account_ref.account_id[-4:]}"

    def _persist_lock(self, *issue_codes: str) -> None:
        state = self.recovery_lock.force_lock(
            self.account_ref.broker_name,
            self.account_ref.account_id,
            issue_codes,
            updated_at=self.now(),
        )
        self.last_error_code = issue_codes[0] if issue_codes else None
        self.state = "locked"
        if state.status is not RecoveryStatus.LOCKED:
            raise RuntimeError("recovery store did not fail closed")

    def _client_health(self) -> Mapping[str, object]:
        try:
            return self.client.health_state()
        except Exception:
            return {}

    def _client_ready(self) -> bool:
        health = self._client_health()
        access_mode = health.get("read_only") is True or health.get("canary") is True
        return access_mode and all(
            health.get(key) is True
            for key in ("connected", "ca_ready", "callback_registered")
        )

    async def start(self) -> None:
        if self.state != "stopped":
            return
        self.state = "starting"
        self.started_at = self.now()
        self.heartbeat_at = self.started_at
        self._stop_event.clear()
        # A persisted lock is the first side effect. A previous process's READY
        # state is never trusted across a restart.
        self._persist_lock("startup_reconciliation_required")
        self.state = "broker_connecting"
        try:
            await self.client.start()
        except Exception as exc:
            self._persist_lock(_operational_code(exc, "broker_login_failed"))
            self._periodic_task = asyncio.create_task(
                self._run_periodic_reconciliation(),
                name=f"{self._task_prefix}-reconciliation",
            )
            self._heartbeat_task = asyncio.create_task(
                self._run_heartbeat(), name=f"{self._task_prefix}-heartbeat"
            )
            return
        if not self._client_ready():
            self._persist_lock("broker_readiness_failed")
            self._periodic_task = asyncio.create_task(
                self._run_periodic_reconciliation(),
                name=f"{self._task_prefix}-reconciliation",
            )
            self._heartbeat_task = asyncio.create_task(
                self._run_heartbeat(), name=f"{self._task_prefix}-heartbeat"
            )
            return
        self.state = "broker_read_only_ready"
        self._callback_task = asyncio.create_task(
            self._run_callbacks(), name=f"{self._task_prefix}-callbacks"
        )
        await self.reconcile_now()
        self._periodic_task = asyncio.create_task(
            self._run_periodic_reconciliation(),
            name=f"{self._task_prefix}-reconciliation",
        )
        self._heartbeat_task = asyncio.create_task(
            self._run_heartbeat(), name=f"{self._task_prefix}-heartbeat"
        )

    async def stop(self) -> None:
        was_running = self.state != "stopped"
        self._stop_event.set()
        for task in (self._periodic_task, self._heartbeat_task):
            if task is not None:
                task.cancel()
        background = [
            task for task in (self._periodic_task, self._heartbeat_task)
            if task is not None
        ]
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        queue = self.client.callback_queue
        if self._callback_task is not None:
            try:
                await asyncio.wait_for(
                    queue.join(), timeout=self.settings.shutdown_drain_seconds
                )
            except asyncio.TimeoutError:
                self.callback_shutdown_abandoned_total += queue.qsize()
            self._callback_task.cancel()
            await asyncio.gather(self._callback_task, return_exceptions=True)
        await self.client.close()
        self._callback_task = self._periodic_task = self._heartbeat_task = None
        if was_running:
            self._persist_lock("worker_stopped")
        self.state = "stopped"
        self.heartbeat_at = self.now()

    async def reconcile_now(self) -> bool:
        """Run one account-scoped reconciliation, skipping overlap."""

        if self._reconciliation_lock.locked():
            self.reconciliation_skipped_overlap_total += 1
            return False
        async with self._reconciliation_lock:
            self.state = "reconciling"
            started = self.now()
            self.last_reconciliation_started_at = started
            self.reconciliation_started_total += 1
            try:
                report = await asyncio.wait_for(
                    self.reconciliation.reconcile(),
                    timeout=self.settings.reconciliation_timeout_seconds,
                )
            except asyncio.TimeoutError:
                self.reconciliation_failure_total += 1
                self.last_reconciliation_failure_at = self.now()
                self._persist_lock("reconciliation_timeout")
                return False
            except Exception as exc:
                self.reconciliation_failure_total += 1
                self.last_reconciliation_failure_at = self.now()
                self._persist_lock(
                    _operational_code(exc, "reconciliation_failed")
                )
                return False
            finally:
                ended = self.now()
                duration = max(0.0, (ended - started).total_seconds() * 1_000)
                self.last_reconciliation_duration_ms = duration
                self._reconciliation_duration_total_ms += duration
                self.max_reconciliation_ms = max(self.max_reconciliation_ms, duration)
            self.last_snapshot_at = report.captured_at
            self.last_local_order_count = report.local_order_count
            self.last_broker_order_count = report.broker_order_count
            self.last_broker_fill_count = report.broker_fill_count
            self.last_broker_position_count = report.broker_position_count
            if report.state.ready and self._client_ready():
                self.reconciliation_success_total += 1
                self.last_reconciliation_success_at = self.now()
                self.last_error_code = None
                self.state = "ready_read_only"
                return True
            self.reconciliation_failure_total += 1
            self.last_reconciliation_failure_at = self.now()
            self.last_error_code = (
                report.state.issue_codes[0]
                if report.state.issue_codes
                else "broker_readiness_failed"
            )
            self.state = "locked"
            if report.state.ready:
                self._persist_lock("broker_readiness_failed")
            return False

    async def _wait(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return False
        return True

    async def _run_periodic_reconciliation(self) -> None:
        while not await self._wait(self.settings.reconciliation_interval_seconds):
            if not self._client_ready() and not await self._reconnect():
                continue
            await self.reconcile_now()

    async def _reconnect(self) -> bool:
        self._persist_lock("broker_reconnecting")
        self.state = "broker_connecting"
        try:
            await self.client.close()
            await self.client.start()
        except Exception as exc:
            self._persist_lock(_operational_code(exc, "broker_login_failed"))
            return False
        if not self._client_ready():
            self._persist_lock("broker_readiness_failed")
            return False
        self.state = "broker_read_only_ready"
        if self._callback_task is None:
            self._callback_task = asyncio.create_task(
                self._run_callbacks(), name=f"{self._task_prefix}-callbacks"
            )
        return True

    async def _run_callbacks(self) -> None:
        queue = self.client.callback_queue
        while not self._stop_event.is_set() or not queue.empty():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            try:
                if event.account_ref != self.account_ref:
                    raise ValueError("callback_execution_target_mismatch")
                self.callbacks_received_total += 1
                self.last_callback_time = event.received_at
                record = await self.callback_consumer.consume_one(event)
                if record.status is BrokerEventAuditStatus.RECONCILED:
                    self.callbacks_reconciled_total += 1
                elif record.status is BrokerEventAuditStatus.UNMATCHED:
                    self.callbacks_unmatched_total += 1
                    self._persist_lock("unmatched_broker_callback")
                elif record.status is BrokerEventAuditStatus.FAILED:
                    self.callbacks_failed_total += 1
                    self._persist_lock(record.error or "broker_callback_failed")
            except Exception as exc:
                self.callbacks_failed_total += 1
                self._persist_lock(
                    _operational_code(exc, "broker_callback_failed")
                )
            finally:
                queue.task_done()

    async def _run_heartbeat(self) -> None:
        while not await self._wait(self.settings.heartbeat_seconds):
            self.heartbeat_at = self.now()
            health = self._client_health()
            if self.state == "ready_read_only" and not self._client_ready():
                self._persist_lock("broker_disconnected")
            dropped = int(health.get("callbacks_dropped_total", 0) or 0)
            normalization_failed = int(
                health.get("callback_normalization_failed_total", 0) or 0
            )
            callback_loss = (
                dropped > self._observed_callback_dropped
                or normalization_failed
                > self._observed_callback_normalization_failed
            )
            self._observed_callback_dropped = dropped
            self._observed_callback_normalization_failed = normalization_failed
            if callback_loss and self.state == "ready_read_only":
                self._persist_lock("callback_delivery_degraded")
            if self.last_snapshot_at is not None:
                age = (self.now() - self.last_snapshot_at).total_seconds()
                if age > self.settings.snapshot_stale_seconds and self.state == "ready_read_only":
                    self._persist_lock("broker_snapshot_stale")

    @staticmethod
    def _time(value: datetime | None) -> str | None:
        return value.isoformat(timespec="milliseconds") if value else None

    def snapshot(self) -> dict[str, object]:
        recovery = self.recovery_lock.state(
            self.account_ref.broker_name, self.account_ref.account_id
        )
        health = self._client_health()
        snapshot_age = (
            max(0.0, (self.now() - self.last_snapshot_at).total_seconds())
            if self.last_snapshot_at is not None
            else None
        )
        completed = self.reconciliation_success_total + self.reconciliation_failure_total
        average = (
            self._reconciliation_duration_total_ms / completed if completed else None
        )
        return {
            "broker_name": self.account_ref.broker_name,
            "target_id": self.account_ref.public_id,
            "masked_account_id": "****" + self.account_ref.account_id[-4:],
            "status": self.state,
            "client_state": str(health.get("execution_state") or "disconnected"),
            "broker_connected": bool(health.get("connected", False)),
            "ca_ready": bool(health.get("ca_ready", False)),
            "read_only": bool(health.get("read_only", True)),
            "canary": bool(health.get("canary", False)),
            "callback_registered": bool(
                health.get("callback_registered", False)
            ),
            "ordering_enabled": False,
            "locked": not recovery.ready,
            "recovery_status": recovery.status.value,
            "recovery_generation": recovery.generation,
            "issue_codes": list(recovery.issue_codes),
            "started_at": self._time(self.started_at),
            "heartbeat_at": self._time(self.heartbeat_at),
            "last_reconciliation_started_at": self._time(
                self.last_reconciliation_started_at
            ),
            "last_reconciliation_success_at": self._time(
                self.last_reconciliation_success_at
            ),
            "last_reconciliation_failure_at": self._time(
                self.last_reconciliation_failure_at
            ),
            "last_reconciliation_duration_ms": self.last_reconciliation_duration_ms,
            "broker_snapshot_age_seconds": snapshot_age,
            "last_broker_read_time": health.get("last_broker_read_time"),
            "last_local_order_count": self.last_local_order_count,
            "last_broker_order_count": self.last_broker_order_count,
            "last_broker_fill_count": self.last_broker_fill_count,
            "last_broker_position_count": self.last_broker_position_count,
            "callback_queue_size": self.client.callback_queue.qsize(),
            "callback_queue_capacity": self.client.callback_queue.maxsize,
            "callback_queue_high_watermark": int(
                health.get("callback_queue_high_watermark", 0) or 0
            ),
            "callbacks_received_total": max(
                self.callbacks_received_total,
                int(health.get("callbacks_received_total", 0) or 0),
            ),
            "callbacks_dropped_total": int(
                health.get("callbacks_dropped_total", 0) or 0
            ),
            "callback_normalization_failed_total": int(
                health.get("callback_normalization_failed_total", 0) or 0
            ),
            "callbacks_reconciled_total": self.callbacks_reconciled_total,
            "callbacks_unmatched_total": self.callbacks_unmatched_total,
            "callbacks_failed_total": self.callbacks_failed_total,
            "callback_shutdown_abandoned_total": self.callback_shutdown_abandoned_total,
            "last_callback_time": self._time(self.last_callback_time) or health.get(
                "last_callback_time"
            ),
            "reconciliation_started_total": self.reconciliation_started_total,
            "reconciliation_success_total": self.reconciliation_success_total,
            "reconciliation_failure_total": self.reconciliation_failure_total,
            "reconciliation_skipped_overlap_total": (
                self.reconciliation_skipped_overlap_total
            ),
            "average_reconciliation_ms": average,
            "max_reconciliation_ms": self.max_reconciliation_ms,
            "last_error_code": self.last_error_code,
            "dispatches": 0,
            "cancel_requests_total": 0,
            "external_order_calls": int(health.get("external_order_calls", 0) or 0),
            "external_cancel_calls": int(health.get("external_cancel_calls", 0) or 0),
            "average_broker_response_ms": health.get("average_broker_response_ms"),
            "max_broker_response_ms": health.get("max_broker_response_ms"),
        }


class ExecutionSupervisor:
    """Start, stop and report isolated broker-account workers."""

    def __init__(self, workers: Sequence[BrokerAccountWorker]) -> None:
        self._workers = {worker.account_ref: worker for worker in workers}
        if len(self._workers) != len(workers):
            raise ValueError("duplicate broker account worker")

    async def start(self) -> None:
        await asyncio.gather(*(worker.start() for worker in self._workers.values()))

    async def stop(self) -> None:
        await asyncio.gather(
            *(worker.stop() for worker in self._workers.values()),
            return_exceptions=True,
        )

    def worker(self, account_ref: BrokerAccountRef) -> BrokerAccountWorker:
        try:
            return self._workers[account_ref]
        except KeyError as exc:
            raise LookupError("unknown broker account worker") from exc

    def snapshot(self) -> dict[str, object]:
        accounts = [worker.snapshot() for worker in self._workers.values()]
        ready = sum(item["status"] == "ready_read_only" for item in accounts)
        return {
            "state": "ready_read_only" if accounts and ready == len(accounts) else "locked",
            "ordering_enabled": any(item.get("ordering_enabled") for item in accounts),
            "locked": ready != len(accounts),
            "broker_accounts": accounts,
            "ready_accounts": ready,
            "locked_accounts": len(accounts) - ready,
            "dispatches": sum(int(item.get("dispatches", 0) or 0) for item in accounts),
            "external_order_calls": sum(int(item.get("external_order_calls", 0) or 0) for item in accounts),
            "external_cancel_calls": sum(int(item.get("external_cancel_calls", 0) or 0) for item in accounts),
        }


class CanaryBrokerAccountWorker(BrokerAccountWorker):
    """Opt-in manual canary dispatch layered on the read/recovery worker."""

    def __init__(
        self,
        *,
        order_manager: LiveOrderManager,
        position_guardian: PositionGuardianRuntime | None = None,
        guardian_poll_seconds: float = 0.25,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.order_manager = order_manager
        self.position_guardian = position_guardian
        self.guardian_poll_seconds = guardian_poll_seconds
        self.live_order_dispatched_total = 0
        self.cancel_requests_total = 0
        self._dispatch_task: asyncio.Task[None] | None = None
        self._guardian_task: asyncio.Task[None] | None = None

    async def reconcile_now(self) -> bool:
        ready = await super().reconcile_now()
        if ready and self.position_guardian is not None:
            try:
                await asyncio.to_thread(self.position_guardian.synchronize)
            except Exception as exc:
                self._persist_lock(_operational_code(exc, str(exc) or "guardian_recovery_failed"))
                return False
        return ready

    async def start(self) -> None:
        await super().start()
        if self._dispatch_task is None:
            self._dispatch_task = asyncio.create_task(
                self._run_canary_dispatch(), name=f"{self._task_prefix}-canary-dispatch"
            )
        if self.position_guardian is not None and self._guardian_task is None:
            self._guardian_task = asyncio.create_task(
                self._run_position_guardian(), name=f"{self._task_prefix}-position-guardian"
            )

    async def stop(self) -> None:
        for task in (self._guardian_task, self._dispatch_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._guardian_task, self._dispatch_task) if task),
            return_exceptions=True,
        )
        self._guardian_task = self._dispatch_task = None
        await super().stop()

    async def _run_position_guardian(self) -> None:
        assert self.position_guardian is not None
        while not self._stop_event.is_set():
            recovery = self.recovery_lock.state(
                self.account_ref.broker_name, self.account_ref.account_id
            )
            if recovery.ready and self._client_ready():
                try:
                    await asyncio.to_thread(self.position_guardian.process_kill_switches)
                    await asyncio.to_thread(self.position_guardian.evaluate_latest_quotes)
                except Exception as exc:
                    self._persist_lock(_operational_code(exc, str(exc) or "position_guardian_failed"))
            if await self._wait(self.guardian_poll_seconds):
                return

    async def _run_canary_dispatch(self) -> None:
        while not self._stop_event.is_set():
            recovery = self.recovery_lock.state(
                self.account_ref.broker_name, self.account_ref.account_id
            )
            if recovery.ready and self._client_ready():
                try:
                    order = await self.order_manager.dispatch_cancel_once(self.account_ref)
                    if order is not None:
                        self.cancel_requests_total += 1
                        if order.status is BrokerOrderStatus.UNKNOWN:
                            self._persist_lock("unknown_cancel_requires_reconciliation")
                        continue
                    order = await self.order_manager.dispatch_once(self.account_ref)
                    if order is not None:
                        self.live_order_dispatched_total += 1
                        if order.status is BrokerOrderStatus.UNKNOWN:
                            if self.position_guardian is not None:
                                await asyncio.to_thread(
                                    self.position_guardian.mark_order_unknown,
                                    order.request.client_order_id,
                                )
                            self._persist_lock("unknown_order_requires_reconciliation")
                        continue
                except Exception as exc:
                    self._persist_lock(_operational_code(exc, "canary_dispatch_failed"))
            if await self._wait(self.settings.dispatch_poll_seconds):
                return

    def snapshot(self) -> dict[str, object]:
        value = super().snapshot()
        health = self._client_health()
        value.update({
            "ordering_enabled": True,
            "dispatches": self.live_order_dispatched_total,
            "cancel_requests_total": self.cancel_requests_total,
            "external_order_calls": int(health.get("external_order_calls", 0) or 0),
            "external_cancel_calls": int(health.get("external_cancel_calls", 0) or 0),
            "average_broker_response_ms": health.get("average_broker_response_ms"),
            "max_broker_response_ms": health.get("max_broker_response_ms"),
            "position_guardian": (
                self.position_guardian.public_state()
                if self.position_guardian is not None
                else {"enabled": False}
            ),
        })
        return value


class ExecutionWorker:
    """Supervise durable dispatch, callback audit, and reconciliation loops."""

    def __init__(
        self,
        manager: LiveOrderManager,
        reconciliation: LiveReconciliationService,
        callback_consumer: BrokerCallbackConsumer,
        callback_queue: asyncio.Queue[BrokerEvent],
        account_ref: BrokerAccountRef,
        *,
        settings: ExecutionWorkerSettings | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        if callback_queue.maxsize <= 0:
            raise ValueError("execution callback queue must be bounded")
        self.manager = manager
        self.reconciliation = reconciliation
        self.callback_consumer = callback_consumer
        self.callback_queue = callback_queue
        self.account_ref = account_ref
        self.settings = settings or ExecutionWorkerSettings(
            callback_queue_size=callback_queue.maxsize
        )
        if self.settings.callback_queue_size != callback_queue.maxsize:
            raise ValueError("callback queue capacity does not match worker settings")
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.state: WorkerState = "stopped"
        self.started_at: datetime | None = None
        self.heartbeat_at: datetime | None = None
        self.last_dispatch_at: datetime | None = None
        self.last_reconciliation_at: datetime | None = None
        self.recovery_status = RecoveryStatus.LOCKED
        self.recovery_issues: tuple[str, ...] = ("reconciliation_required",)
        self.callback_events = 0
        self.callback_failures = 0
        self.unmatched_callbacks = 0
        self.dropped_callbacks = 0
        self.dispatches = 0
        self.reconciliations = 0
        self.failures = 0
        self.last_error: str | None = None
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self.state = "starting"
        self.started_at = self.now()
        self.heartbeat_at = self.started_at
        self._stop_event.clear()
        await self._reconcile()
        self._tasks = [
            asyncio.create_task(self._run_callbacks(), name="broker-callbacks"),
            asyncio.create_task(self._run_dispatch(), name="order-dispatch"),
            asyncio.create_task(
                self._run_reconciliation(), name="broker-reconciliation"
            ),
            asyncio.create_task(self._run_heartbeat(), name="execution-heartbeat"),
        ]

    async def stop(self) -> None:
        self._stop_event.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.state = "stopped"
        self.heartbeat_at = self.now()

    def enqueue_callback(self, event: BrokerEvent) -> bool:
        try:
            callback_target = event.account_ref
        except ValueError as exc:
            self.dropped_callbacks += 1
            self._record_failure(exc)
            return False
        if callback_target != self.account_ref:
            self.dropped_callbacks += 1
            self._record_failure(
                RuntimeError("broker callback belongs to another execution target")
            )
            return False
        try:
            self.callback_queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_callbacks += 1
            self._record_failure(RuntimeError("broker callback queue is full"))
            return False
        return True

    async def _wait(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return False
        return True

    async def _reconcile(self) -> None:
        self.recovery_status = RecoveryStatus.RECONCILING
        self.recovery_issues = ("reconciliation_in_progress",)
        try:
            report = await self.reconciliation.reconcile()
            self._record_reconciliation(report)
        except Exception as exc:
            self.recovery_status = RecoveryStatus.LOCKED
            self.recovery_issues = ("reconciliation_worker_failed",)
            self._record_failure(exc)

    def _record_reconciliation(self, report: ReconciliationReport) -> None:
        self.reconciliations += 1
        self.last_reconciliation_at = self.now()
        self.recovery_status = report.state.status
        self.recovery_issues = report.state.issue_codes
        self.state = "running" if report.state.ready else "degraded"

    def _record_failure(self, exc: Exception) -> None:
        self.failures += 1
        self.last_error = _operational_code(exc, "execution_worker_failed")
        self.recovery_status = RecoveryStatus.LOCKED
        self.recovery_issues = ("execution_worker_failed",)
        self.state = "degraded"

    def _lock_for_unmatched_callback(self) -> None:
        self.recovery_status = RecoveryStatus.LOCKED
        self.recovery_issues = ("unmatched_broker_callback",)
        self.state = "degraded"

    async def _run_callbacks(self) -> None:
        while not self._stop_event.is_set():
            try:
                event = await asyncio.wait_for(
                    self.callback_queue.get(), timeout=0.5
                )
            except asyncio.TimeoutError:
                continue
            try:
                record = await self.callback_consumer.consume_one(event)
                self.callback_events += 1
                if record.status is BrokerEventAuditStatus.FAILED:
                    self.callback_failures += 1
                    self._record_failure(
                        RuntimeError(record.error or "broker callback failed")
                    )
                elif record.status is BrokerEventAuditStatus.UNMATCHED:
                    self.unmatched_callbacks += 1
                    self._lock_for_unmatched_callback()
            except Exception as exc:
                self.callback_failures += 1
                self._record_failure(exc)
            finally:
                self.callback_queue.task_done()

    async def _run_dispatch(self) -> None:
        while not self._stop_event.is_set():
            if self.recovery_status is RecoveryStatus.READY:
                try:
                    result = await self.manager.dispatch_once(self.account_ref)
                    if result is not None:
                        self.dispatches += 1
                        self.last_dispatch_at = self.now()
                        continue
                except Exception as exc:
                    self._record_failure(exc)
            if await self._wait(self.settings.dispatch_poll_seconds):
                return

    async def _run_reconciliation(self) -> None:
        while not await self._wait(
            self.settings.reconciliation_interval_seconds
        ):
            await self._reconcile()

    async def _run_heartbeat(self) -> None:
        while not await self._wait(self.settings.heartbeat_seconds):
            self.heartbeat_at = self.now()

    @staticmethod
    def _time(value: datetime | None) -> str | None:
        return value.isoformat(timespec="milliseconds") if value else None

    def snapshot(self) -> dict[str, object]:
        return {
            "state": self.state,
            "broker_name": self.account_ref.broker_name,
            "masked_account_id": (
                "****" + self.account_ref.account_id[-4:]
                if self.account_ref.account_id
                else None
            ),
            "recovery_status": self.recovery_status.value,
            "recovery_issues": list(self.recovery_issues),
            "started_at": self._time(self.started_at),
            "heartbeat_at": self._time(self.heartbeat_at),
            "last_dispatch_at": self._time(self.last_dispatch_at),
            "last_reconciliation_at": self._time(
                self.last_reconciliation_at
            ),
            "callback_queue_depth": self.callback_queue.qsize(),
            "callback_queue_capacity": self.callback_queue.maxsize,
            "callback_events": self.callback_events,
            "callback_failures": self.callback_failures,
            "unmatched_callbacks": self.unmatched_callbacks,
            "dropped_callbacks": self.dropped_callbacks,
            "dispatches": self.dispatches,
            "reconciliations": self.reconciliations,
            "failures": self.failures,
            "last_error": self.last_error,
        }


@dataclass
class ExecutionRuntime:
    worker: ExecutionWorker
    manager: LiveOrderManager
    callback_queue: asyncio.Queue[BrokerEvent]
    order_repository: SQLiteLiveOrderRepository
    audit_repository: SQLiteBrokerEventAuditRepository
    recovery_repository: SQLiteRecoveryLockRepository
    registry: BrokerRegistry

    async def close(self) -> None:
        await self.worker.stop()
        self.audit_repository.close()
        self.recovery_repository.close()
        self.order_repository.close()


def build_execution_runtime(
    path: str | Path,
    *,
    broker_name: str,
    account_id: str,
    broker: BrokerPort,
    reconciliation_source: BrokerReconciliationSource,
    capabilities: BrokerCapabilities | None = None,
    instrument_mapper: BrokerInstrumentMapper | None = None,
    settings: ExecutionWorkerSettings | None = None,
    admission_gates: Sequence[OrderAdmissionGate] = (),
) -> ExecutionRuntime:
    """Compose the persistent worker without constructing credentials or SDKs."""

    broker_name = broker_name.strip().lower()
    if broker_name == "disabled":
        raise ValueError("disabled execution does not create a persistent runtime")
    account_ref = BrokerAccountRef(broker_name, account_id)
    if broker.broker_name != account_ref.broker_name:
        raise ValueError("broker identity does not match execution runtime")
    worker_settings = settings or ExecutionWorkerSettings()
    order_repository = SQLiteLiveOrderRepository(path)
    audit_repository = SQLiteBrokerEventAuditRepository(path)
    recovery_repository = SQLiteRecoveryLockRepository(path)
    registry = BrokerRegistry()
    registry.register(BrokerRegistration(
        account_ref=account_ref,
        port=broker,
        capabilities=capabilities or BrokerCapabilities(),
        instrument_mapper=instrument_mapper or LockedInstrumentMapper(),
    ))
    registry.freeze()
    gate = CompositeOrderAdmissionGate((
        RecoveryOrderGate(recovery_repository, account_ref),
        *admission_gates,
    ))
    manager = LiveOrderManager(order_repository, registry, {account_ref: gate})
    reconciliation = LiveReconciliationService(
        account_ref=account_ref,
        order_store=order_repository,
        order_manager=manager,
        source=reconciliation_source,
        recovery_lock=recovery_repository,
    )
    consumer = BrokerCallbackConsumer(audit_repository, manager)
    callback_queue: asyncio.Queue[BrokerEvent] = asyncio.Queue(
        maxsize=worker_settings.callback_queue_size
    )
    worker = ExecutionWorker(
        manager,
        reconciliation,
        consumer,
        callback_queue,
        account_ref,
        settings=worker_settings,
    )
    return ExecutionRuntime(
        worker=worker,
        manager=manager,
        callback_queue=callback_queue,
        order_repository=order_repository,
        audit_repository=audit_repository,
        recovery_repository=recovery_repository,
        registry=registry,
    )

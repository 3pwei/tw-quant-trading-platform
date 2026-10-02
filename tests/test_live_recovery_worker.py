from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
import json

from tw_quant.broker import (
    BrokerAccountRef,
    BrokerAccountWorker,
    BrokerCallbackConsumer,
    BrokerCapabilities,
    BrokerEvent,
    BrokerEventAuditStatus,
    BrokerReconciliationSnapshot,
    BrokerRegistration,
    BrokerRegistry,
    BrokerRuntimeState,
    CanonicalInstrument,
    ExecutionSupervisor,
    ExecutionWorkerSettings,
    LiveOrderManager,
    LiveReconciliationService,
    SQLiteBrokerEventAuditRepository,
    SQLiteLiveOrderRepository,
    SQLiteRecoveryLockRepository,
    broker_event_id,
)
from tw_quant.live.monitoring import ExecutionHealthFileMonitor


NOW = datetime(2026, 9, 13, 8, tzinfo=timezone.utc)


class FakeMapper:
    def to_broker_contract(self, instrument):
        return instrument.contract

    def to_canonical_instrument(self, contract):
        return CanonicalInstrument("TMF", contract)


class ReadOnlyPort:
    def __init__(self, broker_name: str):
        self.broker_name = broker_name
        self.submits = 0
        self.cancels = 0

    async def account_state(self):
        return {}

    async def positions(self):
        return []

    async def refresh_order(self, order):
        return order

    async def submit_order(self, request):
        self.submits += 1
        raise AssertionError("read-only recovery must never submit")

    async def cancel_order(self, order):
        self.cancels += 1
        raise AssertionError("read-only recovery must never cancel")


class FakeReadOnlyClient:
    def __init__(
        self,
        account_ref: BrokerAccountRef,
        *,
        snapshot: BrokerReconciliationSnapshot | None = None,
        start_error: Exception | None = None,
        start_check=None,
    ):
        self.account_ref = account_ref
        self.callback_queue: asyncio.Queue[BrokerEvent] = asyncio.Queue(4)
        self.snapshot = snapshot or BrokerReconciliationSnapshot(
            account_ref.broker_name, account_ref.account_id, NOW, (), (), ()
        )
        self.start_error = start_error
        self.start_check = start_check
        self.connected = False
        self.closed = False
        self.snapshot_calls = 0
        self.snapshot_gate: asyncio.Event | None = None

    async def start(self):
        if self.start_check:
            self.start_check()
        if self.start_error:
            raise self.start_error
        self.connected = True

    async def close(self):
        self.connected = False
        self.closed = True

    def health_state(self):
        return {
            "execution_state": "read_only_ready" if self.connected else "locked",
            "connected": self.connected,
            "ca_ready": self.connected,
            "read_only": True,
            "callback_registered": self.connected,
            "callbacks_received_total": 0,
            "callbacks_dropped_total": 0,
            "callback_normalization_failed_total": 0,
            "callback_queue_high_watermark": 0,
        }

    async def reconciliation_snapshot(self):
        self.snapshot_calls += 1
        if self.snapshot_gate is not None:
            await self.snapshot_gate.wait()
        return self.snapshot


class WorkerFixture:
    def __init__(
        self,
        root: Path,
        target: BrokerAccountRef,
        *,
        client: FakeReadOnlyClient | None = None,
        settings: ExecutionWorkerSettings | None = None,
    ):
        self.target = target
        self.path = root / f"{target.broker_name}.sqlite3"
        self.orders = SQLiteLiveOrderRepository(self.path)
        self.audit = SQLiteBrokerEventAuditRepository(self.path)
        self.recovery = SQLiteRecoveryLockRepository(self.path)
        self.port = ReadOnlyPort(target.broker_name)
        self.client = client or FakeReadOnlyClient(target)
        registry = BrokerRegistry()
        registry.register(BrokerRegistration(
            target,
            self.port,
            BrokerCapabilities(),
            FakeMapper(),
            BrokerRuntimeState.READY,
        ))
        registry.freeze()
        manager = LiveOrderManager(self.orders, registry)
        reconciliation = LiveReconciliationService(
            account_ref=target,
            order_store=self.orders,
            order_manager=manager,
            source=self.client,
            recovery_lock=self.recovery,
            now=lambda: NOW,
        )
        consumer = BrokerCallbackConsumer(self.audit, manager, now=lambda: NOW)
        self.worker = BrokerAccountWorker(
            account_ref=target,
            client=self.client,
            reconciliation=reconciliation,
            callback_consumer=consumer,
            recovery_lock=self.recovery,
            settings=settings or ExecutionWorkerSettings(
                reconciliation_interval_seconds=30,
                reconciliation_timeout_seconds=1,
                snapshot_stale_seconds=120,
                heartbeat_seconds=0.01,
                callback_queue_size=4,
                shutdown_drain_seconds=0.1,
            ),
            now=lambda: NOW,
        )

    async def close(self):
        await self.worker.stop()
        self.audit.close()
        self.recovery.close()
        self.orders.close()


class LiveRecoveryWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.target = BrokerAccountRef("broker-a", "account-1")

    def tearDown(self):
        self.temp.cleanup()

    async def test_startup_persists_lock_before_connect_then_reconciles(self):
        observed = []
        fixture = WorkerFixture(self.root, self.target)
        fixture.client.start_check = lambda: observed.append(
            fixture.recovery.state("broker-a", "account-1").status.value
        )
        try:
            await fixture.worker.start()
            health = fixture.worker.snapshot()
            self.assertEqual(observed, ["locked"])
            self.assertEqual(health["status"], "ready_read_only")
            self.assertEqual(health["recovery_status"], "ready")
            self.assertFalse(health["ordering_enabled"])
            self.assertEqual(health["dispatches"], 0)
            self.assertEqual(fixture.port.submits, 0)
            self.assertEqual(fixture.port.cancels, 0)
        finally:
            await fixture.close()

    async def test_restart_never_trusts_previous_ready_generation(self):
        first = WorkerFixture(self.root, self.target)
        await first.worker.start()
        generation = first.worker.snapshot()["recovery_generation"]
        await first.close()

        observed = []
        second = WorkerFixture(self.root, self.target)
        second.client.start_check = lambda: observed.append(
            second.recovery.state("broker-a", "account-1")
        )
        try:
            await second.worker.start()
            self.assertEqual(observed[0].status.value, "locked")
            self.assertGreater(observed[0].generation, generation)
            self.assertEqual(second.worker.snapshot()["status"], "ready_read_only")
        finally:
            await second.close()

    async def test_login_failure_and_identity_mismatch_stay_locked(self):
        failed_client = FakeReadOnlyClient(
            self.target, start_error=RuntimeError("credential-secret")
        )
        failed = WorkerFixture(self.root, self.target, client=failed_client)
        try:
            await failed.worker.start()
            health = failed.worker.snapshot()
            self.assertEqual(health["status"], "locked")
            self.assertEqual(health["issue_codes"], ["broker_login_failed"])
            self.assertNotIn("credential-secret", str(health))
        finally:
            await failed.close()

        mismatched = BrokerReconciliationSnapshot(
            "broker-b", "account-1", NOW, (), (), ()
        )
        fixture = WorkerFixture(
            self.root, self.target, client=FakeReadOnlyClient(
                self.target, snapshot=mismatched
            )
        )
        try:
            await fixture.worker.start()
            self.assertIn("broker_mismatch", fixture.worker.snapshot()["issue_codes"])
            self.assertEqual(fixture.worker.snapshot()["status"], "locked")
        finally:
            await fixture.close()

    async def test_unmatched_callback_is_audited_locks_and_is_recoverable(self):
        fixture = WorkerFixture(self.root, self.target)
        payload = {"status": "Submitted"}
        callback = BrokerEvent(
            broker_event_id("broker-a", "account-1", "ORDER", "external", payload),
            "broker-a", "account-1", "ORDER", "external", NOW, payload,
        )
        try:
            await fixture.worker.start()
            await fixture.client.callback_queue.put(callback)
            await asyncio.wait_for(fixture.client.callback_queue.join(), 1)
            record = fixture.audit.get(callback.event_id)
            self.assertEqual(record.status, BrokerEventAuditStatus.UNMATCHED)
            self.assertEqual(fixture.worker.snapshot()["recovery_status"], "locked")
            self.assertTrue(await fixture.worker.reconcile_now())
            self.assertEqual(fixture.worker.snapshot()["status"], "ready_read_only")
        finally:
            await fixture.close()

    async def test_reconciliation_overlap_is_skipped_and_timeout_locks(self):
        settings = ExecutionWorkerSettings(
            reconciliation_interval_seconds=30,
            reconciliation_timeout_seconds=0.03,
            snapshot_stale_seconds=120,
            heartbeat_seconds=1,
            callback_queue_size=4,
            shutdown_drain_seconds=0.1,
        )
        fixture = WorkerFixture(self.root, self.target, settings=settings)
        try:
            await fixture.worker.start()
            fixture.client.snapshot_gate = asyncio.Event()
            running = asyncio.create_task(fixture.worker.reconcile_now())
            await asyncio.sleep(0)
            self.assertFalse(await fixture.worker.reconcile_now())
            self.assertFalse(await running)
            health = fixture.worker.snapshot()
            self.assertEqual(health["issue_codes"], ["reconciliation_timeout"])
            self.assertEqual(health["reconciliation_skipped_overlap_total"], 1)
            self.assertGreaterEqual(health["reconciliation_failure_total"], 1)
        finally:
            await fixture.close()

    async def test_disconnect_and_stale_snapshot_fail_closed(self):
        disconnected = WorkerFixture(self.root, self.target)
        try:
            await disconnected.worker.start()
            disconnected.client.connected = False
            await asyncio.sleep(0.03)
            self.assertEqual(
                disconnected.worker.snapshot()["issue_codes"],
                ["broker_disconnected"],
            )
        finally:
            await disconnected.close()

        stale_target = BrokerAccountRef("broker-stale", "account-1")
        stale = WorkerFixture(self.root, stale_target)
        try:
            await stale.worker.start()
            stale.worker.now = lambda: NOW + timedelta(seconds=121)
            await asyncio.sleep(0.03)
            self.assertEqual(
                stale.worker.snapshot()["issue_codes"],
                ["broker_snapshot_stale"],
            )
        finally:
            await stale.close()

    async def test_reconnect_requires_reconciliation_before_ready(self):
        client = FakeReadOnlyClient(
            self.target, start_error=RuntimeError("offline")
        )
        settings = ExecutionWorkerSettings(
            reconciliation_interval_seconds=0.02,
            reconciliation_timeout_seconds=1,
            snapshot_stale_seconds=120,
            heartbeat_seconds=0.01,
            callback_queue_size=4,
            shutdown_drain_seconds=0.1,
        )
        fixture = WorkerFixture(
            self.root, self.target, client=client, settings=settings
        )
        try:
            await fixture.worker.start()
            self.assertEqual(fixture.worker.snapshot()["status"], "locked")
            client.start_error = None
            deadline = asyncio.get_running_loop().time() + 1
            while fixture.worker.snapshot()["status"] != "ready_read_only":
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("reconnect did not complete reconciliation")
                await asyncio.sleep(0.01)
            self.assertGreaterEqual(client.snapshot_calls, 1)
            self.assertEqual(fixture.worker.snapshot()["recovery_status"], "ready")
        finally:
            await fixture.close()

    async def test_cross_account_callback_never_reconciles_this_worker(self):
        fixture = WorkerFixture(self.root, self.target)
        other = BrokerAccountRef("broker-b", "account-1")
        payload = {"status": "Submitted"}
        callback = BrokerEvent(
            broker_event_id("broker-b", "account-1", "ORDER", "other", payload),
            "broker-b", "account-1", "ORDER", "other", NOW, payload,
        )
        try:
            await fixture.worker.start()
            await fixture.client.callback_queue.put(callback)
            await asyncio.wait_for(fixture.client.callback_queue.join(), 1)
            self.assertEqual(fixture.worker.snapshot()["recovery_status"], "locked")
            self.assertEqual(fixture.worker.snapshot()["callbacks_reconciled_total"], 0)
            self.assertIsNone(fixture.audit.get(callback.event_id))
            self.assertNotEqual(other, self.target)
        finally:
            await fixture.close()

    async def test_supervisor_keeps_accounts_isolated(self):
        first = WorkerFixture(self.root, self.target)
        second_target = BrokerAccountRef("broker-b", "account-1")
        second = WorkerFixture(
            self.root,
            second_target,
            client=FakeReadOnlyClient(
                second_target, start_error=RuntimeError("offline")
            ),
        )
        supervisor = ExecutionSupervisor((first.worker, second.worker))
        try:
            await supervisor.start()
            health = supervisor.snapshot()
            self.assertEqual(len(health["broker_accounts"]), 2)
            self.assertEqual(first.worker.snapshot()["status"], "ready_read_only")
            self.assertEqual(second.worker.snapshot()["status"], "locked")
            self.assertEqual(first.worker.snapshot()["recovery_status"], "ready")
            self.assertEqual(second.worker.snapshot()["recovery_status"], "locked")
            self.assertEqual(health["external_order_calls"], 0)
            self.assertEqual(health["external_cancel_calls"], 0)
        finally:
            await supervisor.stop()
            first.audit.close()
            first.recovery.close()
            first.orders.close()
            second.audit.close()
            second.recovery.close()
            second.orders.close()

    async def test_shutdown_closes_client_and_leaves_no_worker_tasks(self):
        fixture = WorkerFixture(self.root, self.target)
        await fixture.worker.start()
        await fixture.worker.stop()
        self.assertTrue(fixture.client.closed)
        self.assertEqual(fixture.worker.snapshot()["status"], "stopped")
        self.assertIsNone(fixture.worker._callback_task)
        self.assertIsNone(fixture.worker._periodic_task)
        self.assertIsNone(fixture.worker._heartbeat_task)
        stopped_recovery = fixture.recovery.state("broker-a", "account-1")
        self.assertEqual(stopped_recovery.status.value, "locked")
        self.assertEqual(stopped_recovery.issue_codes, ("worker_stopped",))
        fixture.audit.close()
        fixture.recovery.close()
        fixture.orders.close()


class CachedHealthBoundaryTests(unittest.TestCase):
    def test_public_process_reads_allowlisted_cached_fields_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            path.write_text(json.dumps({
                "execution_state": "ready_read_only",
                "recovery_status": "ready",
                "heartbeat_at": NOW.isoformat(),
                "api_key": "API_KEY_TEST_SECRET",
                "broker_accounts": [{
                    "broker_name": "shioaji",
                    "masked_account_id": "****0001",
                    "status": "ready_read_only",
                    "ordering_enabled": False,
                    "issue_codes": [],
                    "secret": "CA_PASSWORD_TEST_SECRET",
                }],
            }), encoding="utf-8")
            health = ExecutionHealthFileMonitor(
                path, now=lambda: NOW
            ).snapshot()
            payload = json.dumps(health)
            self.assertEqual(health["state"], "ready_read_only")
            self.assertEqual(len(health["broker_accounts"]), 1)
            self.assertNotIn("API_KEY_TEST_SECRET", payload)
            self.assertNotIn("CA_PASSWORD_TEST_SECRET", payload)

    def test_missing_cached_health_fails_closed_without_broker_io(self):
        health = ExecutionHealthFileMonitor("/definitely/missing/health.json").snapshot()
        self.assertEqual(health["state"], "disabled")
        self.assertTrue(health["locked"])
        self.assertFalse(health["ordering_enabled"])

    def test_stale_cached_health_cannot_report_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            path.write_text(json.dumps({
                "execution_state": "ready_read_only",
                "recovery_status": "ready",
                "heartbeat_at": NOW.isoformat(),
                "broker_accounts": [{
                    "broker_name": "shioaji",
                    "masked_account_id": "****0001",
                    "status": "ready_read_only",
                    "recovery_status": "ready",
                    "issue_codes": [],
                }],
            }), encoding="utf-8")
            health = ExecutionHealthFileMonitor(
                path, max_age_seconds=30, now=lambda: NOW + timedelta(seconds=31)
            ).snapshot()
            self.assertEqual(health["state"], "locked")
            self.assertIn(
                "execution_health_stale",
                health["broker_accounts"][0]["issue_codes"],
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from tw_quant.broker import (
    BrokerAccountRef,
    BrokerCapabilities,
    BrokerEvent,
    BrokerEventAuditStatus,
    BrokerOrder,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerReconciliationSnapshot,
    CanonicalInstrument,
    DisabledExecutionWorker,
    ExecutionMode,
    ExecutionWorkerSettings,
    RecoveryStatus,
    RoutedBrokerOrderRequest,
    broker_event_id,
    build_execution_runtime,
)


NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)
TARGET = BrokerAccountRef("shioaji", "sim-1")


class FakeMapper:
    def to_broker_contract(self, instrument):
        return instrument.contract

    def to_canonical_instrument(self, broker_contract):
        return CanonicalInstrument("TMF", broker_contract)


MAPPER = FakeMapper()
CAPABILITIES = BrokerCapabilities(
    supports_market_orders=True,
    supports_cancel=True,
)


def routed(order_request=None):
    return RoutedBrokerOrderRequest(TARGET, order_request or request())


def request(client_order_id: str = "worker-order") -> BrokerOrderRequest:
    return BrokerOrderRequest(
        client_order_id=client_order_id,
        owner_id="owner-1",
        strategy_id="dow-channel",
        strategy_version=1,
        symbol="TMF",
        contract="TMF202609",
        side="buy",
        quantity=1,
        mode=ExecutionMode.LIVE,
    )


class FakeBroker:
    broker_name = "shioaji"

    def __init__(self):
        self.submissions = 0
        self.refreshes = 0

    async def account_state(self):
        return {}

    async def positions(self):
        return []

    async def submit_order(self, order_request):
        self.submissions += 1
        return BrokerOrder(
            request=order_request,
            status=BrokerOrderStatus.ACCEPTED,
            updated_at=NOW,
            broker_order_id=f"broker-{order_request.client_order_id}",
        )

    async def cancel_order(self, order):
        return order

    async def refresh_order(self, order):
        self.refreshes += 1
        return order


class SnapshotSource:
    def __init__(self):
        self.error: Exception | None = None

    async def reconciliation_snapshot(self):
        if self.error is not None:
            raise self.error
        return BrokerReconciliationSnapshot(
            broker_name="shioaji",
            account_id="sim-1",
            captured_at=NOW,
            orders=(),
            fills=(),
            positions=(),
        )


async def wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.005)


class ExecutionWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "execution.sqlite3"
        self.settings = ExecutionWorkerSettings(
            dispatch_poll_seconds=0.01,
            reconciliation_interval_seconds=10,
            heartbeat_seconds=0.01,
            callback_queue_size=2,
        )

    def tearDown(self):
        self.temp.cleanup()

    def runtime(self, broker: FakeBroker, source: SnapshotSource):
        return build_execution_runtime(
            self.path,
            broker_name="shioaji",
            account_id="sim-1",
            broker=broker,
            capabilities=CAPABILITIES,
            instrument_mapper=MAPPER,
            reconciliation_source=source,
            settings=self.settings,
        )

    async def test_startup_reconciliation_unlocks_durable_dispatch(self):
        broker = FakeBroker()
        runtime = self.runtime(broker, SnapshotSource())
        try:
            await runtime.worker.start()
            self.assertEqual(
                runtime.worker.snapshot()["recovery_status"], "ready"
            )
            self.assertEqual(runtime.worker.snapshot()["broker_name"], "shioaji")
            self.assertEqual(
                runtime.worker.snapshot()["masked_account_id"], "****im-1"
            )
            runtime.manager.create(routed())
            await wait_until(lambda: broker.submissions == 1)
            self.assertEqual(
                runtime.order_repository.outbox_state("worker-order"),
                "completed",
            )
            snapshot = runtime.worker.snapshot()
            self.assertEqual(snapshot["state"], "running")
            self.assertEqual(snapshot["dispatches"], 1)
            await wait_until(
                lambda: runtime.worker.snapshot()["heartbeat_at"]
                != snapshot["heartbeat_at"]
            )
        finally:
            await runtime.close()

    async def test_failed_startup_reconciliation_keeps_outbox_pending(self):
        broker = FakeBroker()
        source = SnapshotSource()
        source.error = TimeoutError("broker unavailable")
        runtime = self.runtime(broker, source)
        runtime.order_repository.reserve(routed(), occurred_at=NOW)
        try:
            await runtime.worker.start()
            await asyncio.sleep(0.05)
            snapshot = runtime.worker.snapshot()
            self.assertEqual(snapshot["state"], "degraded")
            self.assertEqual(snapshot["recovery_status"], "locked")
            self.assertIn("reconciliation_failed", snapshot["recovery_issues"])
            self.assertEqual(broker.submissions, 0)
            self.assertEqual(
                runtime.order_repository.outbox_state("worker-order"), "pending"
            )
        finally:
            await runtime.close()

    async def test_callback_is_audited_then_refreshes_the_local_order(self):
        broker = FakeBroker()
        runtime = self.runtime(broker, SnapshotSource())
        try:
            await runtime.worker.start()
            runtime.manager.create(routed())
            await wait_until(lambda: broker.submissions == 1)
            payload = {"status": "Submitted"}
            event = BrokerEvent(
                event_id=broker_event_id(
                    "shioaji", "sim-1", "FORDER", "broker-worker-order", payload
                ),
                broker_name="shioaji",
                account_id="sim-1",
                event_type="FORDER",
                broker_order_id="broker-worker-order",
                received_at=NOW,
                payload=payload,
            )
            self.assertTrue(runtime.worker.enqueue_callback(event))
            await wait_until(
                lambda: runtime.worker.snapshot()["callback_events"] == 1
            )
            record = runtime.audit_repository.get(event.event_id)
            self.assertIsNotNone(record)
            self.assertEqual(record.status, BrokerEventAuditStatus.RECONCILED)
            self.assertGreaterEqual(broker.refreshes, 1)
        finally:
            await runtime.close()

    async def test_queue_overflow_is_visible_and_never_blocks_callback(self):
        settings = ExecutionWorkerSettings(callback_queue_size=1)
        runtime = build_execution_runtime(
            self.path,
            broker_name="shioaji",
            account_id="sim-1",
            broker=FakeBroker(),
            capabilities=CAPABILITIES,
            instrument_mapper=MAPPER,
            reconciliation_source=SnapshotSource(),
            settings=settings,
        )
        payload = {"sequence": 1}
        first = BrokerEvent(
            broker_event_id("shioaji", "sim-1", "FORDER", "broker-1", payload),
            "shioaji",
            "sim-1",
            "FORDER",
            "broker-1",
            NOW,
            payload,
        )
        try:
            self.assertTrue(runtime.worker.enqueue_callback(first))
            self.assertFalse(runtime.worker.enqueue_callback(first))
            snapshot = runtime.worker.snapshot()
            self.assertEqual(snapshot["callback_queue_depth"], 1)
            self.assertEqual(snapshot["dropped_callbacks"], 1)
            self.assertEqual(snapshot["state"], "degraded")
            self.assertEqual(snapshot["recovery_status"], "locked")
        finally:
            await runtime.close()

    async def test_unmatched_callback_locks_dispatch_until_reconciliation(self):
        runtime = self.runtime(FakeBroker(), SnapshotSource())
        payload = {"status": "Submitted"}
        event = BrokerEvent(
            broker_event_id(
                "shioaji", "sim-1", "FORDER", "unknown-order", payload
            ),
            "shioaji",
            "sim-1",
            "FORDER",
            "unknown-order",
            NOW,
            payload,
        )
        try:
            await runtime.worker.start()
            self.assertTrue(runtime.worker.enqueue_callback(event))
            await wait_until(
                lambda: runtime.worker.snapshot()["unmatched_callbacks"] == 1
            )
            snapshot = runtime.worker.snapshot()
            self.assertEqual(snapshot["state"], "degraded")
            self.assertEqual(snapshot["recovery_status"], "locked")
            self.assertEqual(
                snapshot["recovery_issues"], ["unmatched_broker_callback"]
            )
        finally:
            await runtime.close()

    async def test_disabled_worker_has_no_execution_lifecycle(self):
        worker = DisabledExecutionWorker()
        await worker.start()
        snapshot = worker.snapshot()
        self.assertEqual(snapshot["state"], "disabled")
        self.assertEqual(snapshot["recovery_status"], "locked")
        self.assertIsNone(snapshot["started_at"])
        self.assertIsNone(snapshot["last_dispatch_at"])
        self.assertIsNone(snapshot["last_reconciliation_at"])
        await worker.stop()
        self.assertEqual(worker.snapshot()["dispatches"], 0)

    def test_disabled_provider_cannot_build_persistent_runtime(self):
        with self.assertRaisesRegex(ValueError, "disabled execution"):
            build_execution_runtime(
                self.path,
                broker_name="disabled",
                account_id="",
                broker=FakeBroker(),
                capabilities=CAPABILITIES,
                instrument_mapper=MAPPER,
                reconciliation_source=SnapshotSource(),
            )


if __name__ == "__main__":
    unittest.main()

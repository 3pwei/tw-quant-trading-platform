from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from tw_quant.broker import (
    BrokerAccountRef,
    BrokerCapabilities,
    BrokerCallbackConsumer,
    BrokerEvent,
    BrokerEventAuditStatus,
    BrokerOrder,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerRegistration,
    BrokerRegistry,
    CanonicalInstrument,
    ExecutionMode,
    LiveOrderManager,
    RoutedBrokerOrderRequest,
    SQLiteBrokerEventAuditRepository,
    SQLiteLiveOrderRepository,
    broker_event_id,
    normalize_callback,
)


NOW = datetime(2026, 9, 9, 13, tzinfo=timezone.utc)
TARGET = BrokerAccountRef("shioaji", "sim-1")


class FakeMapper:
    def to_broker_contract(self, instrument):
        return instrument.contract

    def to_canonical_instrument(self, broker_contract):
        return CanonicalInstrument("TMF", broker_contract)


def request() -> BrokerOrderRequest:
    return BrokerOrderRequest(
        client_order_id="client-1",
        owner_id="owner-1",
        strategy_id="dow-channel",
        strategy_version=3,
        symbol="TMF",
        contract="TMF202609",
        side="buy",
        quantity=1,
        mode=ExecutionMode.LIVE,
    )


def event(broker_order_id: str | None = "broker-1") -> BrokerEvent:
    payload = {"order_id": broker_order_id, "status": "Submitted"}
    return BrokerEvent(
        event_id=broker_event_id(
            "shioaji", "sim-1", "FORDER", broker_order_id, payload
        ),
        broker_name="shioaji",
        account_id="sim-1",
        event_type="FORDER",
        broker_order_id=broker_order_id,
        received_at=NOW,
        payload=payload,
    )


class FakeBroker:
    broker_name = "shioaji"

    def __init__(self):
        self.refreshes = 0
        self.error: Exception | None = None

    async def account_state(self):
        return {}

    async def positions(self):
        return []

    async def submit_order(self, order_request):
        raise AssertionError("callback reconciliation must never submit")

    async def cancel_order(self, order):
        raise AssertionError("callback reconciliation must never cancel")

    async def refresh_order(self, order):
        self.refreshes += 1
        if self.error:
            raise self.error
        return order


class BrokerEventTests(unittest.TestCase):
    def test_shioaji_callback_id_is_stable_and_payload_is_json_safe(self):
        first = normalize_callback(
            "FORDER",
            {"order_id": "broker-1", "nested": {"at": NOW}},
            account_ref=TARGET,
            now=lambda: NOW,
        )
        repeated = normalize_callback(
            "FORDER",
            {"nested": {"at": NOW}, "order_id": "broker-1"},
            account_ref=TARGET,
            now=lambda: NOW + timedelta(seconds=1),
        )
        self.assertEqual(first.event_id, repeated.event_id)
        self.assertEqual(first.payload["nested"], {"at": NOW.isoformat()})
        self.assertNotEqual(first.received_at, repeated.received_at)

    def test_event_rejects_an_id_that_does_not_match_content(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            BrokerEvent(
                event_id="invented",
                broker_name="shioaji",
                account_id="sim-1",
                event_type="FORDER",
                broker_order_id="broker-1",
                received_at=NOW,
                payload={},
            )


class BrokerAuditRepositoryTests(unittest.TestCase):
    def test_event_survives_restart_and_duplicate_insert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.sqlite3"
            first = SQLiteBrokerEventAuditRepository(path)
            stored, created = first.record_received(event())
            self.assertTrue(created)
            first.close()

            restarted = SQLiteBrokerEventAuditRepository(path)
            repeated, repeated_created = restarted.record_received(event())
            self.assertFalse(repeated_created)
            self.assertEqual(repeated, stored)
            restarted.close()


class BrokerCallbackConsumerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "live.sqlite3"
        self.orders = SQLiteLiveOrderRepository(path)
        self.audit = SQLiteBrokerEventAuditRepository(path)
        self.broker = FakeBroker()
        self.registry = BrokerRegistry()
        self.registry.register(BrokerRegistration(
            TARGET, self.broker, BrokerCapabilities(), FakeMapper()
        ))
        self.registry.freeze()
        self.manager = LiveOrderManager(self.orders, self.registry)
        self.consumer = BrokerCallbackConsumer(
            self.audit, self.manager, now=lambda: NOW
        )

    def tearDown(self):
        self.audit.close()
        self.orders.close()
        self.temp.cleanup()

    def save_accepted_order(self) -> BrokerOrder:
        reserved, _created = self.orders.reserve(
            RoutedBrokerOrderRequest(TARGET, request()), occurred_at=NOW
        )
        claimed = self.orders.claim_next(TARGET)
        self.assertIsNotNone(claimed)
        accepted = BrokerOrder(
            request=reserved.request,
            status=BrokerOrderStatus.ACCEPTED,
            updated_at=NOW,
            broker_order_id="broker-1",
        )
        self.orders.finish_dispatch(accepted)
        return accepted

    async def test_persists_then_reconciles_callback_linked_order(self):
        self.save_accepted_order()
        record = await self.consumer.consume_one(event())
        self.assertEqual(record.status, BrokerEventAuditStatus.RECONCILED)
        self.assertEqual(record.owner_id, "owner-1")
        self.assertEqual(record.client_order_id, "client-1")
        self.assertEqual(self.broker.refreshes, 1)

        repeated = await self.consumer.consume_one(event())
        self.assertEqual(repeated.status, BrokerEventAuditStatus.RECONCILED)
        self.assertEqual(self.broker.refreshes, 1)

    async def test_unmatched_callback_can_be_retried_after_order_is_saved(self):
        callback = event()
        first = await self.consumer.consume_one(callback)
        self.assertEqual(first.status, BrokerEventAuditStatus.UNMATCHED)

        self.save_accepted_order()
        second = await self.consumer.consume_one(callback)
        self.assertEqual(second.status, BrokerEventAuditStatus.RECONCILED)
        self.assertEqual(self.broker.refreshes, 1)

    async def test_missing_order_id_and_broker_error_are_audited(self):
        unmatched = await self.consumer.consume_one(event(None))
        self.assertEqual(unmatched.status, BrokerEventAuditStatus.UNMATCHED)
        self.assertEqual(unmatched.error, "callback_has_no_broker_order_id")

        self.save_accepted_order()
        self.broker.error = TimeoutError("broker unavailable")
        failed = await self.consumer.consume_one(event())
        self.assertEqual(failed.status, BrokerEventAuditStatus.FAILED)
        self.assertEqual(failed.error, "broker_refresh_failed")

    async def test_run_acknowledges_queue_items_after_processing(self):
        queue: asyncio.Queue[BrokerEvent] = asyncio.Queue()
        stop = asyncio.Event()
        await queue.put(event(None))
        task = asyncio.create_task(self.consumer.run(queue, stop))
        await asyncio.wait_for(queue.join(), timeout=1)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        record = self.audit.get(event(None).event_id)
        self.assertIsNotNone(record)
        self.assertEqual(record.status, BrokerEventAuditStatus.UNMATCHED)


if __name__ == "__main__":
    unittest.main()

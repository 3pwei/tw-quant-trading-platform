from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from tw_quant.broker import (
    BrokerAccountRef,
    BrokerCapabilities,
    BrokerOrder,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerRegistration,
    BrokerRegistry,
    BrokerSettings,
    CanonicalInstrument,
    ExecutionMode,
    ExternalOrderReport,
    InvalidOrderTransition,
    LiveTradingSafety,
    LiveOrderManager,
    RoutedBrokerOrderRequest,
    ShioajiBrokerAdapter,
    SQLiteLiveOrderRepository,
    transition_order,
    build_broker,
)


NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
TARGET = BrokerAccountRef("shioaji", "acct-1")


class FakeMapper:
    def to_broker_contract(self, instrument):
        return instrument.contract

    def to_canonical_instrument(self, broker_contract):
        return CanonicalInstrument("TMF", broker_contract)


def manager_for(repository, broker):
    registry = BrokerRegistry()
    registry.register(BrokerRegistration(
        TARGET, broker, BrokerCapabilities(), FakeMapper()
    ))
    registry.freeze()
    return LiveOrderManager(repository, registry)


def routed(order_request=None):
    return RoutedBrokerOrderRequest(TARGET, order_request or request())


def request(*, mode: ExecutionMode = ExecutionMode.LIVE) -> BrokerOrderRequest:
    return BrokerOrderRequest(
        client_order_id="client-1",
        owner_id="owner-1",
        strategy_id="dow-channel",
        strategy_version=1,
        symbol="TMF",
        contract="TMF202609",
        side="buy",
        quantity=2,
        mode=mode,
    )


class FakeShioajiClient:
    def __init__(self):
        self.submissions = 0
        self.report = ExternalOrderReport("broker-1", "accepted", NOW)

    async def account_state(self):
        return {"account_id": "acct-1"}

    async def positions(self):
        return []

    async def submit(self, _request):
        self.submissions += 1
        if isinstance(self.report, Exception):
            raise self.report
        return self.report

    async def cancel(self, broker_order_id):
        return ExternalOrderReport(broker_order_id, "cancelled", NOW)

    async def order(self, broker_order_id):
        return self.report

    async def order_by_client_id(self, client_order_id):
        return self.report


class BrokerLifecycleTests(unittest.TestCase):
    def test_validates_transition_sequence_and_fill_invariants(self):
        order = BrokerOrder(request(), BrokerOrderStatus.CREATED, NOW)
        order = transition_order(order, BrokerOrderStatus.RISK_APPROVED, updated_at=NOW)
        order = transition_order(order, BrokerOrderStatus.SUBMITTING, updated_at=NOW)
        order = transition_order(
            order,
            BrokerOrderStatus.PARTIALLY_FILLED,
            updated_at=NOW,
            broker_order_id="broker-1",
            filled_quantity=1,
            average_fill_price=20_000,
        )
        order = transition_order(
            order,
            BrokerOrderStatus.FILLED,
            updated_at=NOW,
            filled_quantity=2,
            average_fill_price=20_001,
        )
        self.assertTrue(order.status.terminal)
        self.assertEqual(order.filled_quantity, 2)

    def test_rejects_impossible_jump(self):
        order = BrokerOrder(request(), BrokerOrderStatus.CREATED, NOW)
        with self.assertRaisesRegex(InvalidOrderTransition, "created -> filled"):
            transition_order(order, BrokerOrderStatus.FILLED, updated_at=NOW)

    def test_partial_fill_quantity_cannot_move_backwards(self):
        order = BrokerOrder(
            request(),
            BrokerOrderStatus.PARTIALLY_FILLED,
            NOW,
            broker_order_id="broker-1",
            filled_quantity=1,
            average_fill_price=20_000,
        )
        with self.assertRaisesRegex(InvalidOrderTransition, "cannot decrease"):
            transition_order(
                order,
                BrokerOrderStatus.CANCELLED,
                updated_at=NOW,
                filled_quantity=0,
            )

    def test_reconciliation_can_observe_external_cancellation(self):
        order = BrokerOrder(
            request(),
            BrokerOrderStatus.ACCEPTED,
            NOW,
            broker_order_id="broker-1",
        )
        cancelled = transition_order(
            order, BrokerOrderStatus.CANCELLED, updated_at=NOW
        )
        self.assertEqual(cancelled.status, BrokerOrderStatus.CANCELLED)


class ShioajiBrokerAdapterTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def safety(**changes):
        values = {
            "account_id": "acct-1",
            "enabled": True,
            "confirmation": "I_UNDERSTAND_LIVE_ORDERS",
            "allowed_account_ids": frozenset({"acct-1"}),
        }
        values.update(changes)
        return LiveTradingSafety(**values)

    async def test_is_fail_closed_until_all_three_live_gates_pass(self):
        cases = (
            self.safety(enabled=False),
            self.safety(confirmation="wrong"),
            self.safety(allowed_account_ids=frozenset()),
        )
        for safety in cases:
            with self.subTest(safety=safety):
                adapter = ShioajiBrokerAdapter(FakeShioajiClient(), safety)
                with self.assertRaises(RuntimeError):
                    await adapter.submit_order(request())

    async def test_durable_manager_prevents_duplicate_submission(self):
        client = FakeShioajiClient()
        adapter = ShioajiBrokerAdapter(client, self.safety())
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteLiveOrderRepository(Path(directory) / "orders.sqlite3")
            manager = manager_for(repository, adapter)
            first, created = manager.create(routed())
            repeated, repeated_created = manager.create(routed())
            self.assertTrue(created)
            self.assertFalse(repeated_created)
            self.assertEqual(first, repeated)
            submitted = await manager.dispatch_once(TARGET)
            self.assertIsNotNone(submitted)
            self.assertEqual(submitted.status, BrokerOrderStatus.ACCEPTED)
            self.assertIsNone(await manager.dispatch_once(TARGET))
            self.assertEqual(client.submissions, 1)
            self.assertEqual(repository.outbox_state("client-1"), "completed")
            repository.close()

    async def test_ambiguous_failure_blocks_outbox_and_is_not_retried(self):
        client = FakeShioajiClient()
        client.report = TimeoutError("result unknown")
        adapter = ShioajiBrokerAdapter(client, self.safety())
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteLiveOrderRepository(Path(directory) / "orders.sqlite3")
            manager = manager_for(repository, adapter)
            manager.create(routed())
            first = await manager.dispatch_once(TARGET)
            repeated = await manager.dispatch_once(TARGET)
            self.assertEqual(first.status, BrokerOrderStatus.UNKNOWN)
            self.assertIsNone(repeated)
            self.assertEqual(client.submissions, 1)
            self.assertEqual(repository.outbox_state("client-1"), "blocked")
            repository.close()

    async def test_rejects_paper_request_at_live_boundary(self):
        adapter = ShioajiBrokerAdapter(FakeShioajiClient(), self.safety())
        with self.assertRaisesRegex(ValueError, "only accepts live"):
            await adapter.submit_order(request(mode=ExecutionMode.PAPER))

    async def test_restart_blocks_interrupted_dispatch_until_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite3"
            repository = SQLiteLiveOrderRepository(path)
            repository.reserve(routed())
            self.assertIsNotNone(repository.claim_next(TARGET))
            repository.close()

            recovered = SQLiteLiveOrderRepository(path)
            manager = manager_for(
                recovered,
                ShioajiBrokerAdapter(FakeShioajiClient(), self.safety()),
            )
            self.assertEqual(manager.interrupted_dispatches, 1)
            order = recovered.get("owner-1", "client-1")
            self.assertIsNotNone(order)
            self.assertEqual(order.status, BrokerOrderStatus.UNKNOWN)
            self.assertEqual(recovered.outbox_state("client-1"), "blocked")
            self.assertIsNone(await manager.dispatch_once(TARGET))
            reconciled = await manager.reconcile("owner-1", "client-1")
            self.assertEqual(reconciled.status, BrokerOrderStatus.ACCEPTED)
            self.assertEqual(recovered.outbox_state("client-1"), "resolved")
            recovered.close()


class BrokerCompositionTests(unittest.TestCase):
    def test_execution_is_disabled_by_default(self):
        settings = BrokerSettings.from_env({})
        broker = build_broker(settings)
        self.assertEqual(broker.broker_name, "disabled")

    def test_enabled_provider_still_requires_client_and_safety_values(self):
        settings = BrokerSettings.from_env({
            "BROKER_PROVIDER": "shioaji",
            "LIVE_TRADING_ENABLED": "true",
            "LIVE_BROKER_ACCOUNT_ID": "acct-1",
            "LIVE_ALLOWED_ACCOUNT_IDS": "acct-1",
            "LIVE_TRADING_CONFIRMATION": "wrong",
        })
        with self.assertRaisesRegex(ValueError, "requires a client"):
            build_broker(settings)
        broker = build_broker(settings, shioaji_client=FakeShioajiClient())
        with self.assertRaisesRegex(RuntimeError, "confirmation"):
            import asyncio
            asyncio.run(broker.submit_order(request()))


if __name__ == "__main__":
    unittest.main()

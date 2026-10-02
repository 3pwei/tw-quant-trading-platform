from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tw_quant.broker import (
    BrokerAccountRef,
    BrokerAdapterFactory,
    BrokerCapabilities,
    BrokerCallbackConsumer,
    BrokerEvent,
    BrokerEventAuditStatus,
    BrokerOrder,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerRegistration,
    BrokerRegistry,
    BrokerRuntimeState,
    CanonicalInstrument,
    ExecutionMode,
    LiveOrderManager,
    RoutedBrokerOrderRequest,
    SQLiteBrokerEventAuditRepository,
    SQLiteLiveOrderRepository,
    SQLiteRecoveryLockRepository,
    broker_event_id,
)


NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)
TARGET_A = BrokerAccountRef("broker-a", "account-1")
TARGET_B = BrokerAccountRef("broker-b", "account-1")


def request(client_order_id: str) -> BrokerOrderRequest:
    return BrokerOrderRequest(
        client_order_id=client_order_id,
        owner_id="owner-1",
        strategy_id="strategy-1",
        strategy_version=1,
        symbol="TMF",
        contract="TMF202609",
        side="buy",
        quantity=1,
        mode=ExecutionMode.LIVE,
    )


class FakeInstrumentMapper:
    def __init__(self, prefix: str):
        self.prefix = prefix

    def to_broker_contract(self, instrument: CanonicalInstrument) -> str:
        if instrument.symbol != "TMF":
            raise KeyError("instrument is not mapped")
        return f"{self.prefix}:{instrument.contract}"

    def to_canonical_instrument(
        self, broker_contract: str
    ) -> CanonicalInstrument:
        prefix = f"{self.prefix}:"
        if not broker_contract.startswith(prefix):
            raise KeyError("broker contract is not mapped")
        return CanonicalInstrument("TMF", broker_contract.removeprefix(prefix))


class FakeBroker:
    def __init__(self, broker_name: str):
        self.broker_name = broker_name
        self.submissions: list[str] = []
        self.cancellations: list[str] = []
        self.refreshes: list[str] = []

    async def account_state(self):
        return {"broker": self.broker_name}

    async def positions(self):
        return [{"contract": "TMF202609", "quantity": 0}]

    async def submit_order(self, order_request):
        self.submissions.append(order_request.client_order_id)
        return BrokerOrder(
            request=order_request,
            status=BrokerOrderStatus.ACCEPTED,
            updated_at=NOW,
            broker_order_id=f"shared-{order_request.client_order_id}",
        )

    async def cancel_order(self, order):
        self.cancellations.append(order.request.client_order_id)
        return BrokerOrder(
            request=order.request,
            status=BrokerOrderStatus.CANCELLED,
            updated_at=NOW,
            broker_order_id=order.broker_order_id,
        )

    async def refresh_order(self, order):
        self.refreshes.append(order.request.client_order_id)
        return order


CAPABILITIES_A = BrokerCapabilities(
    supports_market_orders=True,
    supports_limit_orders=True,
    supports_cancel=True,
    supports_client_order_id=True,
    supports_order_callback=True,
)
CAPABILITIES_B = BrokerCapabilities(
    supports_limit_orders=True,
    supports_ioc=True,
    supports_fill_callback=True,
    supports_partial_fills=True,
)


def registration(
    target: BrokerAccountRef,
    broker: FakeBroker,
    capabilities: BrokerCapabilities,
    *,
    state: BrokerRuntimeState = BrokerRuntimeState.READY,
) -> BrokerRegistration:
    return BrokerRegistration(
        account_ref=target,
        port=broker,
        capabilities=capabilities,
        instrument_mapper=FakeInstrumentMapper(target.broker_name),
        state=state,
    )


def registry_with(
    *registrations: BrokerRegistration,
) -> BrokerRegistry:
    registry = BrokerRegistry()
    for item in registrations:
        registry.register(item)
    registry.freeze()
    return registry


class BrokerRegistryTests(unittest.TestCase):
    def setUp(self):
        self.broker_a = FakeBroker("broker-a")
        self.broker_b = FakeBroker("broker-b")

    def test_registers_and_resolves_same_account_at_two_brokers(self):
        registry = registry_with(
            registration(TARGET_A, self.broker_a, CAPABILITIES_A),
            registration(TARGET_B, self.broker_b, CAPABILITIES_B),
        )

        self.assertIs(registry.resolve(TARGET_A), self.broker_a)
        self.assertIs(registry.resolve(TARGET_B), self.broker_b)
        self.assertEqual(len(registry.registrations), 2)

    def test_duplicate_unknown_and_post_freeze_changes_fail_closed(self):
        registry = BrokerRegistry()
        item = registration(TARGET_A, self.broker_a, CAPABILITIES_A)
        registry.register(item)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            registry.register(item)
        with self.assertRaisesRegex(KeyError, "unknown"):
            registry.resolve(TARGET_B)
        registry.freeze()
        with self.assertRaisesRegex(RuntimeError, "frozen"):
            registry.register(
                registration(TARGET_B, self.broker_b, CAPABILITIES_B)
            )

    def test_unavailable_target_never_falls_back(self):
        registry = registry_with(
            registration(
                TARGET_A,
                self.broker_a,
                CAPABILITIES_A,
                state=BrokerRuntimeState.UNAVAILABLE,
            ),
            registration(TARGET_B, self.broker_b, CAPABILITIES_B),
        )
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            registry.resolve(TARGET_A)
        self.assertEqual(self.broker_b.submissions, [])


class BrokerAdapterFactoryTests(unittest.TestCase):
    def test_builders_are_registered_without_core_broker_switches(self):
        factory = BrokerAdapterFactory()
        broker_a = FakeBroker("broker-a")
        factory.register(
            "broker-a",
            lambda context: registration(
                context, broker_a, CAPABILITIES_A
            ),
        )

        built = factory.build("broker-a", TARGET_A)
        self.assertEqual(built.account_ref, TARGET_A)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            factory.build("broker-b", TARGET_B)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            factory.register("broker-a", lambda _context: built)


class BrokerCapabilityAndMappingTests(unittest.TestCase):
    def test_capabilities_are_isolated_and_unsupported_use_fails(self):
        self.assertTrue(CAPABILITIES_A.supports_market_orders)
        self.assertFalse(CAPABILITIES_B.supports_market_orders)
        self.assertTrue(CAPABILITIES_B.supports_ioc)
        CAPABILITIES_A.require("supports_cancel")
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            CAPABILITIES_B.require("supports_cancel")
        with self.assertRaisesRegex(ValueError, "unknown"):
            CAPABILITIES_A.require("supports_magic_order")

    def test_instrument_mapping_is_per_broker_and_fails_closed(self):
        instrument = CanonicalInstrument("TMF", "TMF202609")
        mapper_a = FakeInstrumentMapper("A")
        mapper_b = FakeInstrumentMapper("B")
        self.assertEqual(mapper_a.to_broker_contract(instrument), "A:TMF202609")
        self.assertEqual(mapper_b.to_broker_contract(instrument), "B:TMF202609")
        self.assertEqual(
            mapper_a.to_canonical_instrument("A:TMF202609"), instrument
        )
        with self.assertRaisesRegex(KeyError, "not mapped"):
            mapper_a.to_broker_contract(CanonicalInstrument("TXF", "TXF202609"))

    def test_strategy_layer_does_not_consume_broker_capabilities(self):
        root = Path(__file__).resolve().parents[1] / "tw_quant/strategy"
        source = "\n".join(
            path.read_text(encoding="utf-8") for path in root.rglob("*.py")
        )
        self.assertNotIn("BrokerCapabilities", source)
        self.assertNotIn("BrokerRegistry", source)


class DurableRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "live.sqlite3"
        self.broker_a = FakeBroker("broker-a")
        self.broker_b = FakeBroker("broker-b")
        self.registry = registry_with(
            registration(TARGET_A, self.broker_a, CAPABILITIES_A),
            registration(TARGET_B, self.broker_b, CAPABILITIES_B),
        )
        self.repository = SQLiteLiveOrderRepository(self.path)
        self.manager = LiveOrderManager(self.repository, self.registry)

    def tearDown(self):
        self.repository.close()
        self.temp.cleanup()

    async def test_each_order_routes_only_to_its_durable_target(self):
        self.manager.create(RoutedBrokerOrderRequest(TARGET_A, request("A-1")))
        self.manager.create(RoutedBrokerOrderRequest(TARGET_B, request("B-1")))

        await self.manager.dispatch_once(TARGET_B)
        self.assertEqual(self.broker_b.submissions, ["B-1"])
        self.assertEqual(self.broker_a.submissions, [])
        await self.manager.dispatch_once(TARGET_A)
        self.assertEqual(self.broker_a.submissions, ["A-1"])

    async def test_unknown_target_is_rejected_before_persistence(self):
        target = BrokerAccountRef("broker-c", "account-1")
        with self.assertRaisesRegex(KeyError, "unknown"):
            self.manager.create(
                RoutedBrokerOrderRequest(target, request("unknown"))
            )
        self.assertIsNone(self.repository.get("owner-1", "unknown"))

    async def test_restart_preserves_target_and_never_uses_current_default(self):
        self.manager.create(RoutedBrokerOrderRequest(TARGET_B, request("B-2")))
        self.repository.close()
        self.repository = SQLiteLiveOrderRepository(self.path)
        restarted = LiveOrderManager(self.repository, self.registry)

        self.assertIsNone(self.repository.claim_next(TARGET_A))
        routed = self.repository.claim_next(TARGET_B)
        self.assertIsNotNone(routed)
        self.assertEqual(routed.target, TARGET_B)
        self.assertEqual(routed.order.request.client_order_id, "B-2")
        self.assertEqual(restarted.interrupted_dispatches, 0)

    async def test_same_client_id_cannot_be_retargeted(self):
        self.manager.create(RoutedBrokerOrderRequest(TARGET_A, request("same")))
        with self.assertRaisesRegex(ValueError, "another target"):
            self.repository.reserve(
                RoutedBrokerOrderRequest(TARGET_B, request("same"))
            )

    async def test_broker_order_ids_are_unique_per_target(self):
        for target, client_id in ((TARGET_A, "A-3"), (TARGET_B, "B-3")):
            order_request = request(client_id)
            self.repository.reserve(
                RoutedBrokerOrderRequest(target, order_request),
                occurred_at=NOW,
            )
            self.assertIsNotNone(self.repository.claim_next(target))
            self.repository.finish_dispatch(BrokerOrder(
                request=order_request,
                status=BrokerOrderStatus.ACCEPTED,
                updated_at=NOW,
                broker_order_id="same-broker-order-id",
            ))
        self.assertEqual(
            self.repository.get_by_broker_order_id(
                TARGET_A, "same-broker-order-id"
            ).request.client_order_id,
            "A-3",
        )
        self.assertEqual(
            self.repository.get_by_broker_order_id(
                TARGET_B, "same-broker-order-id"
            ).request.client_order_id,
            "B-3",
        )

    async def test_legacy_unrouted_outbox_is_blocked_by_additive_migration(self):
        self.repository.close()
        legacy = sqlite3.connect(self.path)
        legacy.execute("DROP TABLE live_order_outbox")
        legacy.execute(
            "CREATE TABLE live_order_outbox ("
            "client_order_id TEXT PRIMARY KEY, state TEXT NOT NULL, "
            "attempt_count INTEGER NOT NULL, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        legacy.execute(
            "INSERT INTO live_order_outbox VALUES "
            "('legacy', 'pending', 0, ?, ?)",
            (NOW.isoformat(), NOW.isoformat()),
        )
        legacy.commit()
        legacy.close()

        self.repository = SQLiteLiveOrderRepository(self.path)
        self.assertEqual(self.repository.outbox_state("legacy"), "blocked")


class RecoveryAndCallbackIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_state_isolated_by_full_target(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRecoveryLockRepository(Path(directory) / "live.sqlite3")
            attempt = store.begin(
                TARGET_A.broker_name, TARGET_A.account_id, updated_at=NOW
            )
            store.complete(
                TARGET_A.broker_name,
                TARGET_A.account_id,
                (),
                expected_generation=attempt.generation,
                updated_at=NOW,
            )
            self.assertTrue(
                store.state(TARGET_A.broker_name, TARGET_A.account_id).ready
            )
            self.assertFalse(
                store.state(TARGET_B.broker_name, TARGET_B.account_id).ready
            )
            store.close()

    async def test_callback_identity_and_lookup_cannot_cross_targets(self):
        payload = {"status": "Submitted"}
        event_a = BrokerEvent(
            broker_event_id(
                TARGET_A.broker_name,
                TARGET_A.account_id,
                "ORDER",
                "same-order",
                payload,
            ),
            TARGET_A.broker_name,
            TARGET_A.account_id,
            "ORDER",
            "same-order",
            NOW,
            payload,
        )
        event_b = BrokerEvent(
            broker_event_id(
                TARGET_B.broker_name,
                TARGET_B.account_id,
                "ORDER",
                "same-order",
                payload,
            ),
            TARGET_B.broker_name,
            TARGET_B.account_id,
            "ORDER",
            "same-order",
            NOW,
            payload,
        )
        self.assertNotEqual(event_a.event_id, event_b.event_id)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live.sqlite3"
            orders = SQLiteLiveOrderRepository(path)
            audit = SQLiteBrokerEventAuditRepository(path)
            broker_a = FakeBroker("broker-a")
            broker_b = FakeBroker("broker-b")
            registry = registry_with(
                registration(TARGET_A, broker_a, CAPABILITIES_A),
                registration(TARGET_B, broker_b, CAPABILITIES_B),
            )
            manager = LiveOrderManager(orders, registry)
            for target, client_id in ((TARGET_A, "A-cb"), (TARGET_B, "B-cb")):
                order_request = request(client_id)
                orders.reserve(
                    RoutedBrokerOrderRequest(target, order_request),
                    occurred_at=NOW,
                )
                orders.claim_next(target)
                orders.finish_dispatch(BrokerOrder(
                    order_request,
                    BrokerOrderStatus.ACCEPTED,
                    NOW,
                    broker_order_id="same-order",
                ))

            consumer = BrokerCallbackConsumer(audit, manager, now=lambda: NOW)
            first = await consumer.consume_one(event_a)
            repeated = await consumer.consume_one(event_a)
            self.assertEqual(first.status, BrokerEventAuditStatus.RECONCILED)
            self.assertEqual(repeated.status, BrokerEventAuditStatus.RECONCILED)
            self.assertEqual(broker_a.refreshes, ["A-cb"])
            self.assertEqual(broker_b.refreshes, [])
            audit.close()
            orders.close()


class BrokerPortContractTest(unittest.IsolatedAsyncioTestCase):
    async def _assert_contract(self, broker: FakeBroker) -> None:
        order_request = request(f"{broker.broker_name}-contract")
        account = await broker.account_state()
        positions = await broker.positions()
        accepted = await broker.submit_order(order_request)
        refreshed = await broker.refresh_order(accepted)
        cancelled = await broker.cancel_order(refreshed)

        self.assertEqual(account["broker"], broker.broker_name)
        self.assertIsInstance(positions, list)
        self.assertEqual(accepted.status, BrokerOrderStatus.ACCEPTED)
        self.assertEqual(refreshed, accepted)
        self.assertEqual(cancelled.status, BrokerOrderStatus.CANCELLED)
        self.assertEqual(cancelled.broker_order_id, accepted.broker_order_id)

    async def test_fake_broker_a_contract(self):
        await self._assert_contract(FakeBroker("broker-a"))

    async def test_fake_broker_b_contract(self):
        await self._assert_contract(FakeBroker("broker-b"))


if __name__ == "__main__":
    unittest.main()

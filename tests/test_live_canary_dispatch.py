from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tw_quant.broker import (
    BrokerAccountRef,
    BrokerCapabilities,
    BrokerOrder,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerRegistration,
    BrokerRegistry,
    BrokerRuntimeState,
    CanaryArmSession,
    CanaryOrderAdmissionGate,
    CompositeOrderAdmissionGate,
    ExecutionMode,
    LiveCanaryConfig,
    LiveOrderManager,
    LockedInstrumentMapper,
    SQLiteCanaryArmRepository,
    SQLiteLiveOrderRepository,
)
from tw_quant.execution.canary import LiveExecutionSink


TARGET = BrokerAccountRef("fake-broker", "account-1")
NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


class FakeCanaryBroker:
    broker_name = TARGET.broker_name

    def __init__(self) -> None:
        self.submit_calls = 0
        self.cancel_calls = 0
        self.fail_submit = False

    async def account_state(self): return {}
    async def positions(self): return []
    async def refresh_order(self, order): return order

    async def submit_order(self, request):
        self.submit_calls += 1
        if self.fail_submit:
            raise TimeoutError("ambiguous broker result")
        return BrokerOrder(
            request,
            BrokerOrderStatus.ACCEPTED,
            NOW + timedelta(seconds=1),
            broker_order_id=f"broker-{self.submit_calls}",
        )

    async def cancel_order(self, order):
        self.cancel_calls += 1
        return replace(
            order,
            status=BrokerOrderStatus.CANCELLED,
            updated_at=NOW + timedelta(seconds=2),
        )


class LiveCanaryDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "live.sqlite3"
        self.orders = SQLiteLiveOrderRepository(path)
        self.arms = SQLiteCanaryArmRepository(path)
        self.clock = [NOW]
        self.arms.arm(CanaryArmSession(
            "arm-1", "owner-1", TARGET, NOW,
            NOW + timedelta(minutes=10), "owner-1",
        ))
        config = LiveCanaryConfig(
            enabled=True,
            allowed_owner_ids=frozenset({"owner-1"}),
            allowed_broker_accounts=frozenset({TARGET}),
            allowed_symbols=frozenset({"TMF"}),
            allowed_contracts=frozenset({"TMF202612"}),
        )
        self.broker = FakeCanaryBroker()
        registry = BrokerRegistry()
        registry.register(BrokerRegistration(
            TARGET,
            self.broker,
            BrokerCapabilities(supports_limit_orders=True, supports_ioc=True),
            LockedInstrumentMapper(),
            state=BrokerRuntimeState.READY,
        ))
        registry.freeze()
        gate = CanaryOrderAdmissionGate(
            config, self.arms, TARGET, lambda: None,
            lambda: {"connected": True, "ca_ready": True},
            lambda _owner, _reduce_only: False,
            lambda: self.clock[0],
        )
        self.manager = LiveOrderManager(
            self.orders,
            registry,
            {TARGET: CompositeOrderAdmissionGate((gate,))},
        )
        self.sink = LiveExecutionSink(self.manager)

    def tearDown(self) -> None:
        self.orders.close()
        self.arms.close()
        self.temp.cleanup()

    def reserve(self, client_order_id: str):
        return self.sink.reserve(TARGET, BrokerOrderRequest(
            client_order_id, "owner-1", "manual-live-canary", 1,
            "TMF", "TMF202612", "buy", 1, ExecutionMode.LIVE,
            order_type="limit", time_in_force="ioc", limit_price=101,
            source="manual_live_canary", arm_id="arm-1",
        ))[0]

    async def test_submit_is_claimed_once_after_durable_reservation(self) -> None:
        order = self.reserve("canary-1")
        self.assertEqual(self.orders.outbox_state(order.request.client_order_id), "pending")
        accepted = await self.manager.dispatch_once(TARGET)
        self.assertEqual(accepted.status, BrokerOrderStatus.ACCEPTED)
        self.assertEqual(self.broker.submit_calls, 1)
        self.assertIsNone(await self.manager.dispatch_once(TARGET))

    async def test_ambiguous_submit_becomes_unknown_and_never_retries(self) -> None:
        order = self.reserve("canary-unknown")
        self.broker.fail_submit = True
        unknown = await self.manager.dispatch_once(TARGET)
        self.assertEqual(unknown.status, BrokerOrderStatus.UNKNOWN)
        self.assertEqual(self.orders.outbox_state(order.request.client_order_id), "blocked")
        self.assertIsNone(await self.manager.dispatch_once(TARGET))
        self.assertEqual(self.broker.submit_calls, 1)

    async def test_cancel_is_durable_and_owner_scoped(self) -> None:
        self.reserve("canary-cancel")
        accepted = await self.manager.dispatch_once(TARGET)
        with self.assertRaises(KeyError):
            self.manager.request_cancel(TARGET, "other-owner", "canary-cancel")
        pending, created = self.manager.request_cancel(
            TARGET, "owner-1", "canary-cancel"
        )
        self.assertTrue(created)
        self.assertEqual(pending.status, BrokerOrderStatus.CANCEL_PENDING)
        self.assertEqual(self.broker.cancel_calls, 0)
        cancelled = await self.manager.dispatch_cancel_once(TARGET)
        self.assertEqual(cancelled.status, BrokerOrderStatus.CANCELLED)
        self.assertEqual(self.broker.cancel_calls, 1)


if __name__ == "__main__":
    unittest.main()

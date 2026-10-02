from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tw_quant.broker import (
    BrokerAccountRef,
    BrokerCapabilities,
    BrokerFillSnapshot,
    BrokerOrder,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerPositionSnapshot,
    BrokerReconciliationSnapshot,
    BrokerRegistration,
    BrokerRegistry,
    BrokerRuntimeState,
    ExecutionMode,
    LockedInstrumentMapper,
    LiveOrderManager,
    SQLiteBrokerTruthRepository,
    SQLiteLiveOrderRepository,
    SQLitePositionGuardianRepository,
    SQLiteRecoveryLockRepository,
    RoutedBrokerOrderRequest,
)
from tw_quant.execution import (
    GuardianExecutionSink,
    GuardianExitReason,
    LivePositionGuardian,
    LivePositionGuardianConfig,
)
from tw_quant.execution.live_models import InstrumentSpec
from tw_quant.execution.live_policy import EmergencyExitPolicy, MarketableLimitIOCPolicy
from tw_quant.market import ExecutionQuote, ExecutionQuoteCache
from tw_quant.risk.live import LiveKillSwitchAction
from tw_quant.execution_service.config import ExecutionServiceSettings


NOW = datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc)
TARGET = BrokerAccountRef("broker-a", "account-1")
OTHER = BrokerAccountRef("broker-b", "account-1")


class FakeBroker:
    def __init__(self, broker_name="broker-a") -> None:
        self.broker_name = broker_name
        self.submit_calls = 0
        self.fail_submit = False

    async def account_state(self): return {}
    async def positions(self): return []
    async def cancel_order(self, order):
        return replace(order, status=BrokerOrderStatus.CANCELLED, updated_at=NOW)
    async def refresh_order(self, order): return order

    async def submit_order(self, request):
        self.submit_calls += 1
        if self.fail_submit:
            raise TimeoutError("ambiguous")
        return BrokerOrder(
            request, BrokerOrderStatus.ACCEPTED, NOW,
            broker_order_id=f"broker-{self.submit_calls}",
        )


class KillSwitches:
    def __init__(self) -> None:
        self.values = ()

    def actions(self, owner_id, target):
        return self.values if target == TARGET and owner_id == "owner-1" else ()


class GuardianTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "live.sqlite3"
        self.orders = SQLiteLiveOrderRepository(self.path)
        self.truth = SQLiteBrokerTruthRepository(self.path)
        self.recovery = SQLiteRecoveryLockRepository(self.path)
        self.positions = SQLitePositionGuardianRepository(self.path)
        self.quotes = ExecutionQuoteCache()
        self.quotes.update(ExecutionQuote(
            "TMF", "TMF202612", 100, 101, 100.5, NOW, NOW, "fake"
        ))
        self.broker = FakeBroker()
        self.registry = BrokerRegistry()
        capabilities = BrokerCapabilities(
            supports_market_orders=True, supports_limit_orders=True,
            supports_ioc=True, supports_cancel=True,
        )
        self.registry.register(BrokerRegistration(
            TARGET, self.broker, capabilities, LockedInstrumentMapper(),
            BrokerRuntimeState.READY,
        ))
        self.registry.register(BrokerRegistration(
            OTHER, FakeBroker("broker-b"), capabilities, LockedInstrumentMapper(),
            BrokerRuntimeState.READY,
        ))
        self.registry.freeze()
        self.manager = LiveOrderManager(self.orders, self.registry, recover_interrupted=False)
        attempt = self.recovery.begin(TARGET.broker_name, TARGET.account_id, updated_at=NOW)
        self.recovery.complete(
            TARGET.broker_name, TARGET.account_id, (),
            expected_generation=attempt.generation, updated_at=NOW,
        )
        self.kill = KillSwitches()
        self.guardian = LivePositionGuardian(
            account_ref=TARGET,
            config=LivePositionGuardianConfig(enabled=True, stop_loss_ticks=2, take_profit_ticks=4),
            store=self.positions,
            order_store=self.orders,
            truth_store=self.truth,
            recovery=self.recovery,
            registry=self.registry,
            sink=GuardianExecutionSink(self.manager),
            quotes=self.quotes,
            instrument=InstrumentSpec("TMF", "TMF202612", 1, 10),
            normal_policy=MarketableLimitIOCPolicy(2, 4),
            emergency_policy=EmergencyExitPolicy(),
            readiness=lambda: {"connected": True, "ca_ready": True},
            kill_switches=self.kill,
            now=lambda: NOW,
        )

    def tearDown(self) -> None:
        self.positions.close(); self.truth.close(); self.recovery.close(); self.orders.close()
        self.temp.cleanup()

    async def entry(self, quantity=1):
        request = BrokerOrderRequest(
            f"entry-{quantity}", "owner-1", "strategy-a", 1, "TMF", "TMF202612",
            "buy", quantity, ExecutionMode.LIVE, order_type="limit", time_in_force="ioc",
            limit_price=101, source="manual_live_canary", arm_id="test-arm",
        )
        self.manager.create(RoutedBrokerOrderRequest(TARGET, request))
        return await self.manager.dispatch_once(TARGET)

    def snapshot(self, order, fills, quantity):
        snap = BrokerReconciliationSnapshot(
            TARGET.broker_name, TARGET.account_id, NOW,
            (), tuple(fills),
            (BrokerPositionSnapshot("TMF202612", quantity),) if quantity else (),
        )
        self.truth.save(TARGET, snap)
        return snap

    async def test_full_fill_creates_managed_position_from_actual_fill(self):
        order = await self.entry()
        self.snapshot(order, (BrokerFillSnapshot(
            "fill-1", order.broker_order_id, "TMF202612", "buy", 1, 105, NOW
        ),), 1)
        position = self.guardian.synchronize()[0]
        self.assertEqual((position.average_fill_price, position.stop_loss_price, position.take_profit_price), (105, 103, 109))
        self.assertEqual(position.protection_quantity, 1)

    async def test_partial_fill_and_later_fill_update_protection(self):
        order = await self.entry(2)
        first = BrokerFillSnapshot("fill-1", order.broker_order_id, "TMF202612", "buy", 1, 100, NOW)
        self.snapshot(order, (first,), 1)
        position = self.guardian.synchronize()[0]
        self.assertEqual(position.protection_quantity, 1)
        second = BrokerFillSnapshot("fill-2", order.broker_order_id, "TMF202612", "buy", 1, 104, NOW + timedelta(seconds=1))
        self.snapshot(order, (first, second), 2)
        position = self.guardian.synchronize()[0]
        self.assertEqual((position.protection_quantity, position.average_fill_price), (2, 102))

    async def test_long_stop_and_take_profit_create_one_reduce_only_exit(self):
        order = await self.entry()
        self.snapshot(order, (BrokerFillSnapshot("fill-1", order.broker_order_id, "TMF202612", "buy", 1, 105, NOW),), 1)
        position = self.guardian.synchronize()[0]
        stop_quote = ExecutionQuote("TMF", "TMF202612", 103, 104, 103, NOW, NOW, "fake")
        created = self.guardian.on_quote(stop_quote)
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].request.reduce_only)
        self.assertEqual(created[0].request.side, "sell")
        _same, was_created = self.guardian.request_exit(position.position_id, GuardianExitReason.STRATEGY_EXIT, stop_quote)
        self.assertFalse(was_created)

    async def test_short_take_profit_is_reduce_only_buy(self):
        request = BrokerOrderRequest(
            "short", "owner-1", "strategy-a", 1, "TMF", "TMF202612", "sell", 1,
            ExecutionMode.LIVE, order_type="limit", time_in_force="ioc", limit_price=100,
            source="manual_live_canary", arm_id="test-arm",
        )
        self.manager.create(RoutedBrokerOrderRequest(TARGET, request))
        order = await self.manager.dispatch_once(TARGET)
        self.snapshot(order, (BrokerFillSnapshot("fill-s", order.broker_order_id, "TMF202612", "sell", 1, 100, NOW),), -1)
        self.guardian.synchronize()
        created = self.guardian.on_quote(ExecutionQuote("TMF", "TMF202612", 95, 96, 95, NOW, NOW, "fake"))
        self.assertEqual(created[0].request.side, "buy")
        self.assertTrue(created[0].request.reduce_only)

    async def test_position_mismatch_and_broker_disconnect_fail_closed(self):
        order = await self.entry()
        self.snapshot(order, (BrokerFillSnapshot("fill-1", order.broker_order_id, "TMF202612", "buy", 1, 100, NOW),), 2)
        with self.assertRaisesRegex(RuntimeError, "position_mismatch"):
            self.guardian.synchronize()
        self.guardian.readiness = lambda: {"connected": False, "ca_ready": True}
        with self.assertRaisesRegex(RuntimeError, "broker_unavailable"):
            self.guardian.synchronize()

    async def test_restart_store_and_multi_broker_identity_are_isolated(self):
        order = await self.entry()
        self.snapshot(order, (BrokerFillSnapshot("fill-1", order.broker_order_id, "TMF202612", "buy", 1, 100, NOW),), 1)
        self.guardian.synchronize()
        self.positions.close()
        self.positions = SQLitePositionGuardianRepository(self.path)
        self.guardian.store = self.positions
        self.assertEqual(len(self.guardian.synchronize()), 1)
        self.assertEqual(self.positions.positions(OTHER), [])

    async def test_emergency_flatten_ignores_missing_quote_but_not_recovery(self):
        order = await self.entry()
        self.snapshot(order, (BrokerFillSnapshot("fill-1", order.broker_order_id, "TMF202612", "buy", 1, 100, NOW),), 1)
        position = self.guardian.synchronize()[0]
        exit_order, created = self.guardian.request_exit(
            position.position_id, GuardianExitReason.EMERGENCY_FLATTEN, None
        )
        self.assertTrue(created)
        self.assertEqual(exit_order.request.order_type, "market")
        self.assertTrue(exit_order.request.reduce_only)
        self.recovery.force_lock(TARGET.broker_name, TARGET.account_id, ("mismatch",), updated_at=NOW)
        with self.assertRaises(RuntimeError):
            self.guardian.request_exit(position.position_id, GuardianExitReason.EMERGENCY_FLATTEN, None)

    async def test_flatten_kill_switch_creates_guardian_exit(self):
        order = await self.entry()
        self.snapshot(order, (BrokerFillSnapshot("fill-1", order.broker_order_id, "TMF202612", "buy", 1, 100, NOW),), 1)
        self.guardian.synchronize()
        self.kill.values = (LiveKillSwitchAction.FLATTEN,)
        self.assertEqual(self.guardian.process_kill_switches(), [])
        await self.manager.dispatch_cancel_once(TARGET)
        created = self.guardian.process_kill_switches()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].request.source, "live_position_guardian")

    async def test_exit_priority_and_unknown_never_create_second_exit(self):
        order = await self.entry()
        self.snapshot(order, (BrokerFillSnapshot("fill-1", order.broker_order_id, "TMF202612", "buy", 1, 100, NOW),), 1)
        position = self.guardian.synchronize()[0]
        strategy, created = self.guardian.request_strategy_exit(position.position_id)
        self.assertTrue(created)
        _same, created = self.guardian.request_exit(
            position.position_id, GuardianExitReason.STOP_LOSS,
            self.quotes.get("TMF", "TMF202612"),
        )
        self.assertFalse(created)
        self.assertEqual(
            self.positions.get(position.position_id).pending_exit_reason,
            GuardianExitReason.STOP_LOSS,
        )
        self.broker.fail_submit = True
        unknown = await self.manager.dispatch_once(TARGET)
        self.assertEqual(unknown.request.client_order_id, strategy.request.client_order_id)
        self.assertEqual(unknown.status, BrokerOrderStatus.UNKNOWN)
        self.assertTrue(self.guardian.mark_order_unknown(unknown.request.client_order_id))
        self.assertEqual(self.positions.get(position.position_id).state.value, "unknown")
        self.assertIsNone(await self.manager.dispatch_once(TARGET))

    def test_guardian_configuration_is_explicit_and_fail_closed(self):
        self.assertFalse(ExecutionServiceSettings().live_position_guardian_enabled)
        invalid = ExecutionServiceSettings(live_position_guardian_enabled=True)
        self.assertIn("guardian_requires_live_canary", invalid.validation_issues())


if __name__ == "__main__":
    unittest.main()

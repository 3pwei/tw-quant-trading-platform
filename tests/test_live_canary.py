from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tw_quant.broker import (
    BrokerAccountRef,
    BrokerCapabilities,
    BrokerOrder,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerFillSnapshot,
    BrokerReconciliationSnapshot,
    BrokerRegistration,
    BrokerRegistry,
    BrokerRuntimeState,
    CanaryOrderAdmissionGate,
    CompositeOrderAdmissionGate,
    ExecutionMode,
    LIVE_CANARY_CONFIRMATION,
    LiveCanaryConfig,
    LiveOrderManager,
    LockedInstrumentMapper,
    SQLiteCanaryArmRepository,
    SQLiteLiveOrderRepository,
)
from tw_quant.execution.canary import LiveExecutionSink
from tw_quant.execution.live_models import InstrumentSpec
from tw_quant.execution.live_policy import MarketableLimitIOCPolicy
from tw_quant.live.application.live_canary import ManualLiveCanaryService
from tw_quant.live.api_security import rate_limit_scope, required_permission
from tw_quant.execution_service.config import ExecutionServiceSettings
from tw_quant.market import ExecutionQuote, ExecutionQuoteCache
from tw_quant.risk import LiveRiskConfig, LiveRiskService
from tw_quant.risk.live import LiveRiskContext


TARGET = BrokerAccountRef("fake-broker", "account-1")
NOW = datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc)


class FakeBroker:
    broker_name = "fake-broker"

    def __init__(self, fail_submit=False):
        self.submit_calls = self.cancel_calls = 0
        self.fail_submit = fail_submit

    async def account_state(self): return {}
    async def positions(self): return []

    async def submit_order(self, request):
        self.submit_calls += 1
        if self.fail_submit:
            raise TimeoutError("ambiguous")
        return BrokerOrder(
            request, BrokerOrderStatus.ACCEPTED, NOW + timedelta(seconds=1),
            broker_order_id=f"broker-{self.submit_calls}", status_reason="accepted",
        )

    async def cancel_order(self, order):
        self.cancel_calls += 1
        return replace(
            order, status=BrokerOrderStatus.CANCELLED,
            updated_at=NOW + timedelta(seconds=2), status_reason="cancelled",
        )

    async def refresh_order(self, order): return order


class FakeContext:
    def __init__(self):
        self.target_id = "exec_0123456789abcdef"
        self.position = 0
        self.connected = True
        self.kill_switches = []

    def target(self, owner_id):
        if owner_id != "owner-1": raise RuntimeError("live_canary_owner_not_allowed")
        return TARGET

    def instrument(self, symbol, contract):
        return InstrumentSpec(symbol, contract, 1.0, 10.0, date(2026, 12, 31))

    def capabilities(self, target):
        self.target("owner-1")
        return BrokerCapabilities(supports_limit_orders=True, supports_ioc=True)

    def risk_context(self, candidate):
        return LiveRiskContext(
            now=NOW, recovery_status="ready", broker_connected=self.connected,
            broker_truth_captured_at=NOW, market_status="healthy",
            account_position=self.position, owner_portfolio_position=self.position,
            working_order_quantity=0, owner_working_order_quantity=0,
            pending_order_count=0, daily_realized_pnl=0.0, daily_trade_count=0,
            active_live_runtimes=0,
        )

    def broker_position(self, target, contract): return self.position
    def assert_arm_ready(self, target):
        if not self.connected: raise RuntimeError("live_canary_broker_disconnected")
    def public_status(self, owner_id):
        return {"enabled": True, "broker_name": TARGET.broker_name,
                "masked_account_id": "****nt-1", "recovery_status": "ready",
                "position_guardian": {"enabled": True}}

    def activate_kill_switch(self, owner_id, target, action, reason, now):
        self.kill_switches.append((owner_id, target, action.value, reason, now))


class LiveCanaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "live.sqlite3"
        self.clock = [NOW]
        self.config = LiveCanaryConfig(
            enabled=True, allowed_owner_ids=frozenset({"owner-1"}),
            allowed_broker_accounts=frozenset({TARGET}),
            allowed_symbols=frozenset({"TMF"}),
            allowed_contracts=frozenset({"TMF202612"}),
        )
        self.arms = SQLiteCanaryArmRepository(self.path)
        self.orders = SQLiteLiveOrderRepository(self.path)
        self.broker = FakeBroker()
        self.registry = BrokerRegistry()
        self.registry.register(BrokerRegistration(
            TARGET, self.broker,
            BrokerCapabilities(supports_limit_orders=True, supports_ioc=True),
            LockedInstrumentMapper(),
            state=BrokerRuntimeState.READY,
        ))
        self.registry.freeze()
        gate = CanaryOrderAdmissionGate(
            self.config, self.arms, TARGET, lambda: None,
            lambda: {"connected": True, "ca_ready": True},
            lambda owner, reduce: False, lambda: self.clock[0],
            execution_target_id="exec_0123456789abcdef",
        )
        self.manager = LiveOrderManager(
            self.orders, self.registry,
            {TARGET: CompositeOrderAdmissionGate((gate,))},
            recover_interrupted=False,
        )
        self.quotes = ExecutionQuoteCache()
        self.quotes.update(ExecutionQuote(
            "TMF", "TMF202612", 100.0, 101.0, 100.5, NOW, NOW, "fake",
        ))
        risk_config = LiveRiskConfig(
            allowed_symbols=frozenset({"TMF"}),
            allowed_contracts=frozenset({"TMF202612"}),
            max_spread_ticks=4,
        )
        self.context = FakeContext()
        self.service = ManualLiveCanaryService(
            config=self.config, arms=self.arms, sink=LiveExecutionSink(self.manager),
            quotes=self.quotes, risk=LiveRiskService(risk_config),
            policy=MarketableLimitIOCPolicy(2, 4), context=self.context,
            now=lambda: self.clock[0],
        )

    def tearDown(self):
        self.orders.close(); self.arms.close(); self.temp.cleanup()

    def arm(self):
        return self.service.arm("owner-1", "owner-1", LIVE_CANARY_CONFIRMATION)

    def reserve(self, key="request-1"):
        return self.service.reserve_order(
            owner_id="owner-1", side="buy", quantity=1,
            idempotency_key=key,
            confirmation="BUY 1 TMF202612 REAL ORDER",
        )

    def test_arm_requires_confirmation_is_target_scoped_and_expires(self):
        with self.assertRaisesRegex(RuntimeError, "confirmation_invalid"):
            self.service.arm("owner-1", "owner-1", "yes")
        session = self.arm()
        self.assertEqual(session.account_ref, TARGET)
        self.assertEqual(session.target_id, "exec_0123456789abcdef")
        self.assertIsNone(self.arms.active(
            "owner-1", TARGET, self.clock[0], "exec_fedcba9876543210"
        ))
        self.clock[0] += timedelta(seconds=self.config.arm_ttl_seconds)
        self.assertIsNone(self.arms.active("owner-1", TARGET, self.clock[0]))

    def test_repository_restart_clears_arm(self):
        self.arm(); self.arms.close()
        self.arms = SQLiteCanaryArmRepository(self.path)
        self.assertIsNone(self.arms.active("owner-1", TARGET, NOW))

    def test_status_exposes_fail_closed_readiness_summary(self):
        status = self.service.status("owner-1")
        self.assertFalse(status["ordering_enabled"])
        self.assertTrue(status["readiness"]["ready"])
        self.assertNotIn(TARGET.account_id, str(status))

        self.context.connected = False
        with self.assertRaisesRegex(RuntimeError, "broker_disconnected"):
            self.arm()

    def test_durable_before_broker_and_idempotent(self):
        self.arm()
        first, created = self.reserve()
        second, replay_created = self.reserve()
        self.assertTrue(created); self.assertFalse(replay_created)
        self.assertEqual(first.request.client_order_id, second.request.client_order_id)
        self.assertEqual(self.orders.outbox_state(first.request.client_order_id), "pending")
        self.assertEqual(self.broker.submit_calls, 0)

    async def test_dispatch_once_and_unknown_never_retry(self):
        self.arm(); order, _ = self.reserve()
        result = await self.manager.dispatch_once(TARGET)
        self.assertEqual(result.status, BrokerOrderStatus.ACCEPTED)
        self.assertEqual(self.broker.submit_calls, 1)
        self.assertIsNone(await self.manager.dispatch_once(TARGET))
        self.assertEqual(self.broker.submit_calls, 1)

        # A second deterministic request reaches an ambiguous external outcome.
        self.broker.fail_submit = True
        unknown, _ = self.reserve("request-2")
        result = await self.manager.dispatch_once(TARGET)
        self.assertEqual(result.status, BrokerOrderStatus.UNKNOWN)
        self.assertEqual(self.orders.outbox_state(unknown.request.client_order_id), "blocked")
        self.assertIsNone(await self.manager.dispatch_once(TARGET))

    async def test_worker_recheck_blocks_expired_arm_before_external_call(self):
        self.arm(); order, _ = self.reserve()
        self.clock[0] += timedelta(seconds=self.config.arm_ttl_seconds)
        result = await self.manager.dispatch_once(TARGET)
        self.assertEqual(result.status, BrokerOrderStatus.RISK_APPROVED)
        self.assertEqual(self.orders.outbox_state(order.request.client_order_id), "blocked")
        self.assertEqual(self.broker.submit_calls, 0)

    async def test_cancel_is_durable_owner_scoped_and_terminal_truth_wins(self):
        self.arm(); order, _ = self.reserve()
        accepted = await self.manager.dispatch_once(TARGET)
        with self.assertRaises(KeyError):
            self.manager.request_cancel(TARGET, "other-owner", accepted.request.client_order_id)
        pending, created = self.service.reserve_cancel(
            "owner-1", accepted.request.client_order_id
        )
        self.assertTrue(created)
        self.assertEqual(pending.status, BrokerOrderStatus.CANCEL_PENDING)
        self.assertEqual(self.broker.cancel_calls, 0)
        cancelled = await self.manager.dispatch_cancel_once(TARGET)
        self.assertEqual(cancelled.status, BrokerOrderStatus.CANCELLED)
        self.assertEqual(self.broker.cancel_calls, 1)

    async def test_cancel_working_kill_switch_targets_only_platform_entry_orders(self):
        self.arm(); _reserved, _ = self.reserve()
        accepted = await self.manager.dispatch_once(TARGET)
        result = self.service.activate_kill_switch(
            "owner-1", "cancel_working", "operator safety stop"
        )
        self.assertEqual(result["cancel_reserved"], 1)
        self.assertEqual(self.broker.cancel_calls, 0)
        self.assertEqual(self.context.kill_switches[0][1], TARGET)
        cancelled = await self.manager.dispatch_cancel_once(TARGET)
        self.assertEqual(cancelled.request.client_order_id, accepted.request.client_order_id)
        self.assertEqual(self.broker.cancel_calls, 1)
        flatten = self.service.activate_kill_switch(
            "owner-1", "flatten", "guardian emergency"
        )
        self.assertTrue(flatten["flatten_enabled"])
        self.assertTrue(flatten["guardian_managed"])

    def test_non_manual_source_cannot_reach_real_sink(self):
        session = self.arm()
        request = BrokerOrderRequest(
            "strategy-order", "owner-1", "strategy", 1, "TMF", "TMF202612",
            "buy", 1, ExecutionMode.LIVE, order_type="limit", time_in_force="ioc",
            limit_price=101, source="strategy_live", arm_id=session.arm_id,
        )
        with self.assertRaisesRegex(RuntimeError, "source"):
            LiveExecutionSink(self.manager).reserve(TARGET, request)
        self.assertEqual(self.broker.submit_calls, 0)

    async def test_position_ledger_changes_only_from_reconciled_broker_fill(self):
        self.arm(); accepted, _ = self.reserve()
        accepted = await self.manager.dispatch_once(TARGET)
        self.assertEqual(self.orders.live_positions("owner-1", TARGET), [])
        snapshot = BrokerReconciliationSnapshot(
            TARGET.broker_name, TARGET.account_id, NOW + timedelta(seconds=3),
            orders=(),
            fills=(BrokerFillSnapshot(
                "fill-1", accepted.broker_order_id or "", "TMF202612",
                "buy", 1, 101.0, NOW + timedelta(seconds=2),
            ),), positions=(),
        )
        self.orders.apply_reconciled_snapshot(TARGET, snapshot)
        self.assertEqual(self.orders.live_positions("owner-1", TARGET)[0]["quantity"], 1)

    async def test_manual_canary_entry_fill_reduce_only_close_and_flat(self):
        self.arm(); entry, _ = self.reserve("entry")
        entry = await self.manager.dispatch_once(TARGET)
        buy_fill = BrokerFillSnapshot(
            "fill-buy", entry.broker_order_id or "", "TMF202612",
            "buy", 1, 101.0, NOW + timedelta(seconds=2),
        )
        self.orders.apply_reconciled_snapshot(TARGET, BrokerReconciliationSnapshot(
            TARGET.broker_name, TARGET.account_id, NOW + timedelta(seconds=3),
            orders=(), fills=(buy_fill,), positions=(),
        ))
        self.context.position = 1
        close, created = self.service.reserve_close("owner-1", "close")
        self.assertTrue(created)
        self.assertTrue(close.request.reduce_only)
        self.assertEqual(close.request.side, "sell")
        close = await self.manager.dispatch_once(TARGET)
        sell_fill = BrokerFillSnapshot(
            "fill-sell", close.broker_order_id or "", "TMF202612",
            "sell", 1, 100.0, NOW + timedelta(seconds=4),
        )
        self.orders.apply_reconciled_snapshot(TARGET, BrokerReconciliationSnapshot(
            TARGET.broker_name, TARGET.account_id, NOW + timedelta(seconds=5),
            orders=(), fills=(buy_fill, sell_fill), positions=(),
        ))
        self.assertEqual(self.orders.live_positions("owner-1", TARGET)[0]["quantity"], 0)
        self.assertEqual(self.broker.submit_calls, 2)

    def test_live_endpoint_permissions_and_dedicated_rate_scope(self):
        self.assertEqual(required_permission("POST", "/api/live/orders"), "orders.live.manual")
        self.assertEqual(
            required_permission("POST", "/api/live/orders/x/cancel"),
            "orders.live.cancel",
        )
        self.assertEqual(rate_limit_scope("POST", "/api/live/orders"), "live_orders")
        self.assertEqual(
            required_permission("POST", "/api/live/canary/kill-switch"),
            "orders.live.close",
        )

    def test_canary_configuration_is_explicit_and_default_disabled(self):
        self.assertFalse(ExecutionServiceSettings().live_canary_enabled)
        settings = ExecutionServiceSettings(
            broker_name="shioaji", account_id="account-1",
            live_trading_enabled=True,
            confirmation="I_UNDERSTAND_LIVE_ORDERS",
            allowed_account_ids=frozenset({"account-1"}),
            instrument_map_json='[{"symbol":"TMF","contract":"TMF202612","broker_contract":"TMFZ6"}]',
            live_canary_enabled=True,
            live_canary_confirmation=LIVE_CANARY_CONFIRMATION,
            live_canary_allowed_owner_ids=frozenset({"owner-1"}),
            live_canary_allowed_symbols=frozenset({"TMF"}),
            live_canary_allowed_contracts=frozenset({"TMF202612"}),
        )
        self.assertNotIn("invalid_live_canary_config", settings.validation_issues())


if __name__ == "__main__":
    unittest.main()

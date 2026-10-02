from __future__ import annotations

from public_fixtures import InertProvider, PublicTestCase, PublicAsyncTestCase, synthetic_csv, services

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time
import unittest

from tw_quant.broker import (
    BrokerAccountRef,
    BrokerCapabilities,
    BrokerReconciliationSnapshot,
    SQLiteBrokerTruthRepository,
    SQLiteLiveOrderRepository,
)
from tw_quant.execution.live_models import InstrumentSpec, LiveExecutionCandidate
from tw_quant.execution.live_policy import (
    EmergencyExitPolicy,
    MarketableLimitIOCPolicy,
    MarketPriceIOCPolicy,
)
from tw_quant.execution.shadow import ShadowExecutionService
from tw_quant.market import ExecutionQuote, ExecutionQuoteCache
from tw_quant.risk import (
    AccountRiskConfig,
    LiveKillSwitchAction,
    LiveKillSwitchScope,
    LiveKillSwitchState,
    LiveRiskConfig,
    LiveRiskContext,
    LiveRiskService,
)
from tw_quant.live.shadow_context import (
    LiveExecutionTargetCatalog,
    StaticBrokerCapabilityView,
    StaticInstrumentSpecCatalog,
)
from tw_quant.live.shadow_store import SQLiteShadowExecutionRepository
from tw_quant.live.application import TradingRuntimeApplicationService
from tw_quant.live.application.live_shadow import LiveShadowExecutionController
from tw_quant.live.application.errors import InvalidInputError
from tw_quant.live.api_models import TradingRuntimeCreate
from tw_quant.live.api_security import required_permission
from tw_quant.live.storage import SQLiteBarRepository
from tw_quant.live.settings import LiveSettings
from pydantic import ValidationError
from tw_quant.market import KBar


NOW = datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc)
TARGET_A = BrokerAccountRef("broker-a", "account-1")
TARGET_B = BrokerAccountRef("broker-b", "account-1")
SPEC = InstrumentSpec(
    "TMF", "TMF202610", 1.0, 10.0,
    expiry_date=date(2026, 10, 21), commission_per_side=10,
)
QUOTE = ExecutionQuote(
    "TMF", "TMF202610", 20_000, 20_002, 20_001,
    NOW - timedelta(milliseconds=20), NOW - timedelta(milliseconds=10), "fake",
)
CAPABILITIES = BrokerCapabilities(
    supports_market_orders=True,
    supports_limit_orders=True,
    supports_ioc=True,
)


def candidate(
    decision_id: str = "decision-1", *, target: BrokerAccountRef = TARGET_A,
    action: str = "entry", direction: str = "long", quantity: int = 1,
    stop: float | None = 19_900, session: str = "night",
) -> LiveExecutionCandidate:
    return LiveExecutionCandidate(
        owner_id="owner-1", runtime_id="runtime-1", decision_id=decision_id,
        target=target, strategy_id="strategy-1", strategy_version=3,
        symbol="TMF", contract="TMF202610", trigger_time=NOW,
        action=action, direction=direction, quantity=quantity,
        reason="strategy_signal", planned_stop_price=stop, session=session,
    )


def risk_config(**changes) -> LiveRiskConfig:
    values = dict(
        allowed_symbols=frozenset({"TMF"}),
        allowed_contracts=frozenset({"TMF202610"}),
        max_order_quantity=1,
        max_account_position_contracts=1,
        max_owner_portfolio_position_contracts=1,
        max_risk_per_trade=5_000,
        max_daily_loss=10_000,
        max_daily_trades=10,
        max_pending_orders=2,
        max_quote_age_seconds=2,
        max_slippage_ticks=2,
        max_spread_ticks=4,
        allowed_sessions=frozenset({"day", "night"}),
        expiry_guard_days=2,
        max_active_live_runtimes=1,
        reservation_seconds=5,
    )
    values.update(changes)
    return LiveRiskConfig(**values)


def context(**changes) -> LiveRiskContext:
    values = dict(
        now=NOW, recovery_status="ready", broker_connected=True,
        broker_truth_captured_at=NOW - timedelta(milliseconds=50),
        market_status="healthy", account_position=0,
        owner_portfolio_position=0, working_order_quantity=0,
        owner_working_order_quantity=0,
        pending_order_count=0, daily_realized_pnl=0.0,
        daily_trade_count=0, active_live_runtimes=1,
    )
    values.update(changes)
    return LiveRiskContext(**values)


class LiveRiskTests(PublicTestCase):
    def setUp(self):
        self.risk = LiveRiskService(risk_config())

    def test_live_config_is_distinct_versioned_and_server_shape(self):
        self.assertNotIsInstance(self.risk.config, AccountRiskConfig)
        self.assertEqual(self.risk.config.version, risk_config().version)
        self.assertTrue(self.risk.config.version.startswith("live-risk:"))

    def test_entry_approval_uses_stop_multiplier_fees_and_slippage(self):
        result = self.risk.evaluate(candidate(), context(), QUOTE, SPEC)
        self.assertTrue(result.approved)
        self.assertEqual(result.reason, "approved")
        self.assertGreater(result.estimated_risk or 0, 1_000)

    def test_account_order_pending_daily_and_trade_risk_limits(self):
        cases = (
            (candidate(quantity=2), context(), "max_order_quantity_exceeded"),
            (candidate(), context(account_position=1), "account_position_limit"),
            (candidate(), context(working_order_quantity=1), "account_position_limit"),
            (candidate(), context(pending_order_count=2), "max_pending_orders_exceeded"),
            (candidate(), context(daily_realized_pnl=-10_000), "daily_loss_exceeded"),
            (candidate(), context(daily_trade_count=10), "daily_trades_exceeded"),
            (candidate(stop=None), context(), "stop_required"),
            (candidate(stop=10_000), context(), "max_risk_per_trade"),
        )
        for item, state, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(
                    self.risk.evaluate(item, state, QUOTE, SPEC).reason, reason
                )

    def test_portfolio_limit_aggregates_independently_from_account(self):
        result = self.risk.evaluate(
            candidate(), context(owner_portfolio_position=1), QUOTE, SPEC
        )
        self.assertEqual(result.reason, "portfolio_position_limit")
        working = LiveRiskService(risk_config(
            max_account_position_contracts=3,
            max_owner_portfolio_position_contracts=1,
        )).evaluate(
            candidate(), context(owner_working_order_quantity=1), QUOTE, SPEC
        )
        self.assertEqual(working.reason, "portfolio_position_limit")

    def test_unknown_daily_or_position_truth_fails_closed(self):
        for values, reason in (
            ({"daily_realized_pnl": None}, "daily_loss_unavailable"),
            ({"daily_trade_count": None}, "daily_trade_count_unavailable"),
            ({"account_position": None}, "position_unavailable"),
            ({"owner_portfolio_position": None}, "position_unavailable"),
            ({"working_order_quantity": None}, "working_orders_unavailable"),
            ({"owner_working_order_quantity": None}, "working_orders_unavailable"),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(
                    self.risk.evaluate(candidate(), context(**values), QUOTE, SPEC).reason,
                    reason,
                )

    def test_recovery_connection_market_and_truth_fail_closed(self):
        cases = (
            ({"recovery_status": "locked"}, "recovery_not_ready"),
            ({"recovery_status": "reconciling"}, "recovery_not_ready"),
            ({"broker_connected": False}, "target_unavailable"),
            ({"market_status": "market_stale"}, "market_stale"),
            ({"market_status": "provider_disconnected"}, "provider_disconnected"),
            ({"broker_truth_captured_at": NOW - timedelta(seconds=5)}, "broker_truth_stale"),
        )
        for values, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(
                    self.risk.evaluate(candidate(), context(**values), QUOTE, SPEC).reason,
                    reason,
                )

    def test_quote_contract_expiry_and_session_guards(self):
        stale = replace(QUOTE, received_at=NOW - timedelta(seconds=3))
        near_expiry = replace(SPEC, expiry_date=NOW.date() + timedelta(days=2))
        no_expiry = replace(SPEC, expiry_date=None)
        cases = (
            (candidate(), context(), None, SPEC, "execution_quote_incomplete"),
            (candidate(), context(), stale, SPEC, "stale_execution_quote"),
            (replace(candidate(), contract="OTHER"), context(), QUOTE, SPEC, "instrument_spec_mismatch"),
            (candidate(), context(), QUOTE, near_expiry, "expiry_guard"),
            (candidate(), context(), QUOTE, no_expiry, "expiry_metadata_unavailable"),
            (candidate(session="night"), context(), QUOTE, SPEC, "approved"),
        )
        restricted = LiveRiskService(risk_config(allowed_sessions=frozenset({"day"})))
        self.assertEqual(
            restricted.evaluate(candidate(session="night"), context(), QUOTE, SPEC).reason,
            "session_not_allowed",
        )
        for item, state, quote, spec, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.risk.evaluate(item, state, quote, spec).reason, reason)

    def test_reduce_only_bypasses_entry_limits_but_requires_position(self):
        exit_candidate = candidate(action="exit", stop=None)
        state = context(
            account_position=1, daily_realized_pnl=-99_999,
            daily_trade_count=99, market_status="market_stale",
            broker_connected=False,
        )
        result = self.risk.evaluate(exit_candidate, state, QUOTE, SPEC)
        self.assertTrue(result.approved)
        self.assertIn("broker_unavailable", result.warnings)
        self.assertIn("market_stale", result.warnings)
        self.assertEqual(
            self.risk.evaluate(exit_candidate, context(account_position=0), QUOTE, SPEC).reason,
            "reduce_only_position_unavailable",
        )

    def test_three_level_kill_switch_blocks_entry(self):
        for action in LiveKillSwitchAction:
            state = LiveKillSwitchState(
                action, LiveKillSwitchScope.GLOBAL, "global", "test", NOW
            )
            self.assertEqual(
                self.risk.evaluate(candidate(), context(kill_switches=(state,)), QUOTE, SPEC).reason,
                f"kill_switch_{action.value}",
            )


class ExecutionPolicyTests(PublicTestCase):
    def setUp(self):
        self.approval = LiveRiskService(risk_config()).evaluate(
            candidate(), context(), QUOTE, SPEC
        )

    def test_buy_and_sell_marketable_limit_ioc_are_deterministic(self):
        policy = MarketableLimitIOCPolicy(2, 4)
        buy = policy.evaluate(candidate(), QUOTE, SPEC, CAPABILITIES, self.approval)
        sell_candidate = candidate(direction="short", stop=20_100)
        sell_risk = LiveRiskService(risk_config()).evaluate(
            sell_candidate, context(), QUOTE, SPEC
        )
        sell = policy.evaluate(sell_candidate, QUOTE, SPEC, CAPABILITIES, sell_risk)
        self.assertEqual(buy.limit_price, 20_004)
        self.assertEqual(sell.limit_price, 19_998)
        self.assertEqual(buy.request.time_in_force, "ioc")
        self.assertEqual(
            policy.evaluate(candidate(), QUOTE, SPEC, CAPABILITIES, self.approval), buy
        )

    def test_capability_and_spread_never_silently_fallback(self):
        unsupported = BrokerCapabilities(supports_limit_orders=True)
        policy = MarketableLimitIOCPolicy(2, 1)
        self.assertEqual(
            policy.evaluate(candidate(), QUOTE, SPEC, unsupported, self.approval).reason,
            "execution_policy_unsupported",
        )
        self.assertEqual(
            policy.evaluate(candidate(), QUOTE, SPEC, CAPABILITIES, self.approval).reason,
            "spread_limit_exceeded",
        )

    def test_market_ioc_and_emergency_are_explicit(self):
        market = MarketPriceIOCPolicy().evaluate(
            candidate(), QUOTE, SPEC, CAPABILITIES, self.approval
        )
        self.assertEqual(market.request.order_type, "market")
        self.assertEqual(market.request.time_in_force, "ioc")
        emergency = EmergencyExitPolicy().evaluate(
            candidate(), QUOTE, SPEC, CAPABILITIES, self.approval
        )
        self.assertEqual(emergency.reason, "emergency_exit_requires_reduce_only")


class FakeContextProvider:
    def __init__(self, base: LiveRiskContext):
        self.base = base

    def context(self, _candidate, account_reservation, portfolio_reservation):
        return replace(
            self.base,
            account_reservation_quantity=account_reservation,
            portfolio_reservation_quantity=portfolio_reservation,
        )


class ShadowExecutionTests(PublicTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "live.sqlite3"
        self.store = SQLiteShadowExecutionRepository(self.path)
        self.quotes = ExecutionQuoteCache()
        self.quotes.update(QUOTE)
        self.service = ShadowExecutionService(
            risk=LiveRiskService(risk_config()),
            policy=MarketableLimitIOCPolicy(2, 4),
            quotes=self.quotes,
            instruments=StaticInstrumentSpecCatalog({("TMF", "TMF202610"): SPEC}),
            capabilities=StaticBrokerCapabilityView({TARGET_A: CAPABILITIES, TARGET_B: CAPABILITIES}),
            context=FakeContextProvider(context()),
            store=self.store,
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_approved_result_is_durable_idempotent_and_publicly_masked(self):
        first = self.service.evaluate(candidate())
        second = self.service.evaluate(candidate())
        self.assertEqual(first, second)
        self.assertEqual(first.result, "would_submit")
        self.assertEqual(self.store.count(), 1)
        public = first.public_dict()
        self.assertEqual(public["masked_account_id"], "****nt-1")
        self.assertNotIn("account_id", public)
        self.assertNotIn("broker_name", public)
        self.assertNotIn("request", public)
        restarted = SQLiteShadowExecutionRepository(self.path)
        try:
            self.assertEqual(restarted.list_owner("owner-1")[0]["shadow_id"], first.shadow_id)
            restarted_service = ShadowExecutionService(
                risk=LiveRiskService(risk_config()),
                policy=MarketableLimitIOCPolicy(2, 4), quotes=self.quotes,
                instruments=StaticInstrumentSpecCatalog({("TMF", "TMF202610"): SPEC}),
                capabilities=StaticBrokerCapabilityView({TARGET_A: CAPABILITIES}),
                context=FakeContextProvider(context()), store=restarted,
            )
            self.assertEqual(restarted_service.evaluate(candidate()), first)
            self.assertEqual(restarted.count(), 1)
        finally:
            restarted.close()

    def test_shadow_sink_never_creates_live_order_or_outbox(self):
        self.service.evaluate(candidate())
        orders = SQLiteLiveOrderRepository(self.path)
        try:
            self.assertEqual(orders.orders(), [])
            count = orders.connection.execute(
                "SELECT COUNT(*) FROM live_order_outbox"
            ).fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            orders.close()

    def test_concurrent_entries_cannot_bypass_exposure_limit(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                self.service.evaluate,
                (candidate("decision-a"), candidate("decision-b")),
            ))
        self.assertEqual(sorted(item.result for item in results), ["rejected", "would_submit"])
        self.assertIn(
            next(item.reason for item in results if item.result == "rejected"),
            {"account_position_limit", "portfolio_position_limit"},
        )
        self.assertEqual(self.store.release_runtime("runtime-1"), 1)

    def test_broker_targets_and_capabilities_remain_isolated(self):
        service = ShadowExecutionService(
            risk=LiveRiskService(risk_config(max_owner_portfolio_position_contracts=2)),
            policy=MarketableLimitIOCPolicy(2, 4), quotes=self.quotes,
            instruments=StaticInstrumentSpecCatalog({("TMF", "TMF202610"): SPEC}),
            capabilities=StaticBrokerCapabilityView({
                TARGET_A: CAPABILITIES,
                TARGET_B: BrokerCapabilities(supports_limit_orders=True),
            }),
            context=FakeContextProvider(context()), store=self.store,
        )
        self.assertEqual(service.evaluate(candidate("a", target=TARGET_A)).result, "would_submit")
        result_b = service.evaluate(candidate("b", target=TARGET_B))
        self.assertEqual(result_b.reason, "execution_policy_unsupported")
        self.assertEqual(result_b.broker_name, "broker-b")

    def test_policy_exception_fails_closed(self):
        class BrokenPolicy(MarketableLimitIOCPolicy):
            def evaluate(self, *args):
                raise RuntimeError("secret detail")
        service = ShadowExecutionService(
            risk=LiveRiskService(risk_config()), policy=BrokenPolicy(2, 4),
            quotes=self.quotes,
            instruments=StaticInstrumentSpecCatalog({("TMF", "TMF202610"): SPEC}),
            capabilities=StaticBrokerCapabilityView({TARGET_A: CAPABILITIES}),
            context=FakeContextProvider(context()), store=self.store,
        )
        result = service.evaluate(candidate("broken"))
        self.assertEqual(result.reason, "execution_policy_failed")
        self.assertNotIn("secret detail", str(result.public_dict()))

    def test_context_and_instrument_failures_are_durable_rejections(self):
        class BrokenContext:
            def context(self, *_args):
                raise RuntimeError("credential-shaped detail")
        service = ShadowExecutionService(
            risk=LiveRiskService(risk_config()), policy=MarketableLimitIOCPolicy(2, 4),
            quotes=self.quotes,
            instruments=StaticInstrumentSpecCatalog({}),
            capabilities=StaticBrokerCapabilityView({TARGET_A: CAPABILITIES}),
            context=BrokenContext(), store=self.store,
        )
        result = service.evaluate(candidate("context-failure"))
        self.assertEqual(result.reason, "execution_context_unavailable")
        self.assertEqual(self.store.count(), 1)
        self.assertNotIn("credential-shaped detail", str(result.public_dict()))

    def test_kill_switch_preview_is_shadow_only_and_masks_target(self):
        self.store.activate_kill_switch(LiveKillSwitchState(
            LiveKillSwitchAction.FLATTEN,
            LiveKillSwitchScope.BROKER_ACCOUNT,
            "broker-a:account-1",
            "operator drill",
            NOW,
        ))
        preview = self.store.public_kill_switch_preview("owner-1", [TARGET_A])
        self.assertTrue(preview[0]["would_cancel"])
        self.assertTrue(preview[0]["would_flatten"])
        self.assertEqual(preview[0]["target_id"], TARGET_A.public_id)
        self.assertNotIn("account-1", str(preview))

    def test_controller_enqueues_without_waiting_for_shadow_evaluation(self):
        class SlowContext(FakeContextProvider):
            def context(self, *args):
                time.sleep(0.05)
                return super().context(*args)

        service = ShadowExecutionService(
            risk=LiveRiskService(risk_config()), policy=MarketableLimitIOCPolicy(2, 4),
            quotes=self.quotes,
            instruments=StaticInstrumentSpecCatalog({("TMF", "TMF202610"): SPEC}),
            capabilities=StaticBrokerCapabilityView({TARGET_A: CAPABILITIES}),
            context=SlowContext(context()), store=self.store,
        )

        class RuntimeRepository:
            updates = []
            def trading_runtime(self, _runtime_id, _owner_id):
                return {
                    "runtime_id": "runtime-1", "owner_user_id": "owner-1",
                    "status": "active", "mode": "live_shadow",
                    "broker_name": "broker-a", "account_id": "account-1",
                    "strategy_id": "scripted", "strategy_version": 1,
                    "strategy_kind": "atomic", "symbol": "TMF", "quantity": 1,
                    "strategy_snapshot": {"parameters": {"stop_loss_pct": 0.005}},
                }
            def update_decision_execution(self, *args):
                self.updates.append(args)

        repository = RuntimeRepository()
        controller = LiveShadowExecutionController(repository, service)
        controller.start()
        signal_bar = KBar(
            symbol="TMF", contract="TMF202610", time=NOW,
            open=20_000, high=20_010, low=19_990, close=20_000,
            volume=1, status="closed", session="night",
            trading_date=NOW.date(), first_tick_time=NOW,
            last_tick_time=NOW, exchange_time=NOW, received_time=NOW,
            latency_ms=0,
        )
        runtime = repository.trading_runtime("runtime-1", "owner-1")
        decision = {
            "decision_id": "queued", "action": "entry", "direction": "long",
            "contract": "TMF202610", "trigger_time": NOW.isoformat(),
            "reference_price": 20_000, "reason": "signal",
        }
        started = time.perf_counter()
        controller.on_decision(runtime, decision, signal_bar)
        self.assertLess(time.perf_counter() - started, 0.02)
        controller.queue.join()
        self.assertTrue(controller.stop())
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(repository.updates[0][2]["execution_status"], "shadow_would_submit")


class TargetAndTruthTests(PublicTestCase):
    def test_owner_target_and_same_account_cross_broker_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            truth = SQLiteBrokerTruthRepository(Path(directory) / "live.sqlite3")
            empty_a = BrokerReconciliationSnapshot("broker-a", "account-1", NOW, (), (), ())
            empty_b = BrokerReconciliationSnapshot("broker-b", "account-1", NOW, (), (), ())
            truth.save(TARGET_A, empty_a)
            truth.save(TARGET_B, empty_b)
            catalog = LiveExecutionTargetCatalog(
                truth,
                {"owner-a": frozenset({TARGET_A.public_id}), "owner-b": frozenset({TARGET_B.public_id})},
            )
            self.assertEqual(catalog.resolve(TARGET_A.public_id, "owner-a"), TARGET_A)
            with self.assertRaises(KeyError):
                catalog.resolve(TARGET_B.public_id, "owner-a")
            self.assertNotEqual(TARGET_A.public_id, TARGET_B.public_id)
            truth.close()

    def test_runtime_requires_owner_scoped_explicit_immutable_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            repository = SQLiteBarRepository(path)
            truth = SQLiteBrokerTruthRepository(path)
            truth.save(TARGET_A, BrokerReconciliationSnapshot(
                "broker-a", "account-1", NOW, (), (), (),
            ))
            catalog = LiveExecutionTargetCatalog(
                truth, {"owner-a": frozenset({TARGET_A.public_id})},
            )
            service = TradingRuntimeApplicationService(
                repository, repository, repository, "TMF",
                live_shadow_enabled=True, execution_targets=catalog,
            )
            with self.assertRaises(InvalidInputError):
                service.create({"strategy_id": "scripted", "mode": "live_shadow"}, "owner-a")
            with self.assertRaises(InvalidInputError):
                service.create({
                    "strategy_id": "scripted", "mode": "live_shadow",
                    "execution_target_id": TARGET_A.public_id,
                }, "owner-b")
            created = service.create({
                "strategy_id": "scripted", "mode": "live_shadow",
                "execution_target_id": TARGET_A.public_id,
            }, "owner-a")
            self.assertEqual(created["mode"], "live_shadow")
            self.assertEqual(created["execution_target_id"], TARGET_A.public_id)
            self.assertEqual(created["masked_account_id"], "****nt-1")
            self.assertNotIn("account_id", created)
            self.assertNotIn("broker_name", created)
            internal = repository.trading_runtime(str(created["runtime_id"]), "owner-a")
            self.assertEqual(
                (internal["broker_name"], internal["account_id"]),
                (TARGET_A.broker_name, TARGET_A.account_id),
            )
            truth.close()
            repository.close()

    def test_live_shadow_default_disabled_and_browser_risk_fields_fscriptedidden(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteBarRepository(Path(directory) / "runtime.sqlite3")
            service = TradingRuntimeApplicationService(
                repository, repository, repository, "TMF",
            )
            with self.assertRaises(InvalidInputError):
                service.create({
                    "strategy_id": "scripted", "mode": "live_shadow",
                    "execution_target_id": TARGET_A.public_id,
                }, "owner-a")
            with self.assertRaises(ValidationError):
                TradingRuntimeCreate.model_validate({
                    "strategy_id": "scripted", "max_daily_loss": 999_999,
                })
            self.assertEqual(
                required_permission("GET", "/api/live-shadow"),
                "strategy.read.own",
            )
            self.assertEqual(
                required_permission("POST", "/api/live-shadow"),
                "__deny_unknown_api__",
            )
            repository.close()

    def test_enabled_settings_accept_only_opaque_owner_target_ids(self):
        valid = LiveSettings(
            environment="test", live_shadow_enabled=True,
            live_shadow_allowed_contracts=frozenset({"TMF202610"}),
            live_shadow_contract_expiry=date(2026, 10, 21),
            live_shadow_owner_targets_json=(
                '{"owner-a":["' + TARGET_A.public_id + '"]}'
            ),
        )
        valid.validate()
        unsafe = LiveSettings(
            environment="test", live_shadow_enabled=True,
            live_shadow_allowed_contracts=frozenset({"TMF202610"}),
            live_shadow_contract_expiry=date(2026, 10, 21),
            live_shadow_owner_targets_json=(
                '{"owner-a":[{"broker_name":"broker-a","account_id":"account-1"}]}'
            ),
        )
        with self.assertRaisesRegex(ValueError, "opaque target IDs"):
            unsafe.validate()


if __name__ == "__main__":
    unittest.main()

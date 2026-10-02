from datetime import date, datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from tw_quant.auth import AccountStatus, AuthUser, Role, TradingMode
from tw_quant.events import (
    BarClosedEvent,
    DeterministicEventEngine,
    EventMetadata,
    OrderIntent,
    SessionEvent,
    SignalEvent,
)
from tw_quant.execution import PositionLedger, SimulatedExecutionPipeline
from tw_quant_core.futures_costs import FuturesCostConfig
from tw_quant.risk import (
    AccountRiskConfig,
    AccountRiskGate,
    TradingAccess,
    TradingAccessRegistry,
)


TAIPEI = ZoneInfo("Asia/Taipei")
START = datetime(2026, 9, 1, 15, 0, tzinfo=TAIPEI)
TRADE_DATE = date(2026, 9, 2)
ZERO_COSTS = FuturesCostConfig(
    multiplier=10,
    commission_per_side=0,
    tax_rate=0,
    slippage_points=0,
)


def metadata(kind, minute, key, *, owner="owner-1"):
    return EventMetadata.create(
        kind=kind,
        occurred_at=START + timedelta(minutes=minute),
        source="risk-test",
        source_key=key,
        owner_id=owner,
    )


def order(
    minute,
    key,
    *,
    owner="owner-1",
    quantity=1,
    side="buy",
    reference=100,
    stop=95,
    trading_date=TRADE_DATE,
):
    return OrderIntent(
        meta=metadata("order_intent", minute, f"event:{key}", owner=owner),
        order_id=key,
        strategy_id="strategy-a",
        strategy_version=1,
        symbol="TMF",
        contract="TMFU6",
        side=side,
        quantity=quantity,
        reference_price=reference,
        stop_loss_price=stop,
        trading_date=trading_date,
    )


def bar(minute, price):
    return BarClosedEvent(
        meta=metadata("bar_closed", minute, f"bar:{minute}", owner=None),
        symbol="TMF",
        contract="TMFU6",
        timeframe="1m",
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=100,
        session="night",
        trading_date=TRADE_DATE,
    )


def signal(minute, key, action, direction, price=100, stop=95):
    return SignalEvent(
        meta=metadata("signal", minute, key),
        strategy_id="strategy-a",
        strategy_version=1,
        symbol="TMF",
        contract="TMFU6",
        direction=direction,
        action=action,
        reference_price=price,
        stop_loss_price=stop,
        trading_date=TRADE_DATE,
        reason=key,
    )


def paper_user(owner="owner-1"):
    return AuthUser(
        user_id=owner,
        email=f"{owner}@example.com",
        role=Role.TRADER,
        status=AccountStatus.ACTIVE,
        trading_mode=TradingMode.PAPER,
        permissions=("orders.paper", "positions.read.own"),
    )


class AccountRiskRuleTests(unittest.TestCase):
    def setUp(self):
        self.ledger = PositionLedger(multiplier=10)
        self.access = TradingAccessRegistry((paper_user(),))

    def gate(self, **overrides):
        defaults = {
            "max_position_contracts": 2,
            "max_risk_per_trade": 1_000,
            "max_daily_loss": 1_000,
            "max_trades_per_day": 10,
            "max_consecutive_losses": 3,
            "cooldown_minutes": 30,
        }
        defaults.update(overrides)
        return AccountRiskGate(
            self.ledger,
            self.access,
            config=AccountRiskConfig(**defaults),
            costs=ZERO_COSTS,
        )

    def test_active_paper_trader_with_permission_is_approved_and_audited(self):
        gate = self.gate()
        decision = gate.on_order(order(0, "approved"))[0]

        self.assertTrue(decision.approved)
        self.assertEqual(decision.reason, "approved")
        self.assertEqual(gate.audit_log[0].estimated_risk, 50)
        self.assertEqual(gate.snapshot("owner-1").reserved_contracts, 1)

    def test_account_status_mode_and_permission_fail_closed(self):
        cases = (
            (
                TradingAccess(
                    "suspended", AccountStatus.SUSPENDED, TradingMode.PAPER,
                    frozenset({"orders.paper"}),
                ),
                "account_not_active",
            ),
            (
                TradingAccess(
                    "disabled", AccountStatus.ACTIVE, TradingMode.DISABLED,
                    frozenset({"orders.paper"}),
                ),
                "paper_mode_required",
            ),
            (
                TradingAccess(
                    "missing-permission", AccountStatus.ACTIVE, TradingMode.PAPER,
                    frozenset(),
                ),
                "paper_permission_required",
            ),
        )
        for access, expected in cases:
            with self.subTest(expected=expected):
                self.access.set_access(access)
                gate = self.gate()
                result = gate.on_order(
                    order(0, f"order:{access.owner_id}", owner=access.owner_id)
                )[0]
                self.assertFalse(result.approved)
                self.assertEqual(result.reason, expected)

    def test_unknown_account_and_duplicate_order_are_rejected(self):
        gate = self.gate()
        unknown = gate.on_order(order(0, "unknown", owner="nobody"))[0]
        first = gate.on_order(order(0, "same-order"))[0]
        duplicate = gate.on_order(order(1, "same-order"))[0]

        self.assertEqual(unknown.reason, "account_unknown")
        self.assertTrue(first.approved)
        self.assertEqual(duplicate.reason, "duplicate_order")

    def test_stop_and_per_trade_risk_are_required(self):
        gate = self.gate(max_risk_per_trade=40)
        missing = gate.on_order(order(0, "missing-stop", stop=None))[0]
        wrong_side = gate.on_order(order(1, "wrong-stop", stop=105))[0]
        oversized = gate.on_order(order(2, "too-risky", stop=95))[0]

        self.assertEqual(missing.reason, "stop_loss_required")
        self.assertEqual(wrong_side.reason, "invalid_stop_direction")
        self.assertEqual(oversized.reason, "max_trade_risk_exceeded")

    def test_position_limit_counts_pending_reservations(self):
        gate = self.gate(max_position_contracts=2)
        first = gate.on_order(order(0, "two-contracts", quantity=2))[0]
        second = gate.on_order(order(1, "one-more"))[0]

        self.assertTrue(first.approved)
        self.assertEqual(second.reason, "max_position_exceeded")

    def test_manual_kill_switch_persists_across_day_until_reset(self):
        gate = self.gate()
        gate.activate_kill_switch(
            "owner-1", "operator_stop", occurred_at=START
        )
        blocked = gate.on_order(order(0, "blocked"))[0]
        next_day = gate.on_order(
            order(1, "next-day", trading_date=TRADE_DATE + timedelta(days=1))
        )[0]
        gate.reset_kill_switch(
            "owner-1", occurred_at=START + timedelta(minutes=2)
        )
        restored = gate.on_order(
            order(2, "restored", trading_date=TRADE_DATE + timedelta(days=1))
        )[0]

        self.assertEqual(blocked.reason, "kill_switch_active")
        self.assertEqual(next_day.reason, "kill_switch_active")
        self.assertTrue(restored.approved)
        self.assertEqual(
            [entry.action for entry in gate.control_log],
            ["kill_switch.activated", "kill_switch.reset"],
        )


class AccountRiskPipelineTests(unittest.TestCase):
    def setUp(self):
        self.engine = DeterministicEventEngine()
        self.ledger = PositionLedger(multiplier=10)
        self.access = TradingAccessRegistry((paper_user(),))
        self.gate = AccountRiskGate(
            self.ledger,
            self.access,
            config=AccountRiskConfig(
                max_position_contracts=2,
                max_risk_per_trade=1_000,
                max_daily_loss=50,
                max_trades_per_day=10,
                max_consecutive_losses=2,
                cooldown_minutes=30,
            ),
            costs=ZERO_COSTS,
        )
        self.pipeline = SimulatedExecutionPipeline(
            costs=ZERO_COSTS,
            ledger=self.ledger,
            risk_gate=self.gate,
        )
        self.pipeline.install(self.engine)

    def enter(self, minute, key, price=100):
        self.engine.publish(bar(minute, price))
        self.engine.publish(signal(minute, key, "enter", "long", price, price - 5))
        self.engine.publish(bar(minute + 1, price))
        self.engine.run()

    def exit(self, minute, key, price):
        self.engine.publish(signal(minute, key, "exit", "long", price, price - 5))
        self.engine.publish(bar(minute + 1, price))
        self.engine.run()

    def test_realized_daily_loss_activates_automatic_kill_switch(self):
        self.enter(0, "enter-loss")
        self.exit(1, "exit-loss", 90)

        snapshot = self.gate.snapshot("owner-1")
        self.assertEqual(snapshot.realized_pnl, -100)
        self.assertTrue(snapshot.kill_switch_active)
        self.assertEqual(snapshot.kill_switch_reason, "daily_loss_limit")
        self.engine.publish(signal(2, "blocked-after-loss", "enter", "long"))
        self.engine.run()
        self.assertEqual(self.engine.events("risk_decision")[-1].reason, "kill_switch_active")

    def test_manual_kill_switch_blocks_entry_but_allows_exit(self):
        self.enter(0, "enter-before-stop")
        self.gate.activate_kill_switch(
            "owner-1",
            "operator_stop",
            occurred_at=START + timedelta(minutes=1),
        )
        self.exit(1, "safe-exit", 100)

        self.assertEqual(self.ledger.open_positions(), ())
        decisions = self.engine.events("risk_decision")
        self.assertEqual(decisions[-1].reason, "risk_reducing_approved")

    def test_daily_trade_limit_counts_filled_entries(self):
        self.gate.config = AccountRiskConfig(
            max_position_contracts=2,
            max_risk_per_trade=1_000,
            max_daily_loss=10_000,
            max_trades_per_day=1,
            max_consecutive_losses=3,
            cooldown_minutes=30,
        )
        self.enter(0, "first-entry")
        self.exit(1, "first-exit", 100)
        self.engine.publish(signal(2, "second-entry", "enter", "long"))
        self.engine.run()

        self.assertEqual(self.engine.events("risk_decision")[-1].reason, "daily_trade_limit")

    def test_consecutive_losses_enforce_then_release_cooldown(self):
        self.gate.config = AccountRiskConfig(
            max_position_contracts=2,
            max_risk_per_trade=1_000,
            max_daily_loss=10_000,
            max_trades_per_day=10,
            max_consecutive_losses=2,
            cooldown_minutes=30,
        )
        self.enter(0, "loss-one-entry")
        self.exit(1, "loss-one-exit", 90)
        self.enter(2, "loss-two-entry")
        self.exit(3, "loss-two-exit", 80)
        self.engine.publish(signal(4, "cooldown-blocked", "enter", "long"))
        self.engine.run()
        blocked = self.engine.events("risk_decision")[-1]

        self.engine.publish(signal(35, "cooldown-released", "enter", "long"))
        self.engine.run()
        released = self.engine.events("risk_decision")[-1]
        self.assertEqual(blocked.reason, "loss_streak_cooldown")
        self.assertTrue(released.approved)
        self.assertIsNone(self.gate.snapshot("owner-1").cooldown_until)

    def test_automatic_daily_kill_switch_resets_next_trading_date(self):
        self.enter(0, "rollover-loss-entry")
        self.exit(1, "rollover-loss-exit", 90)
        next_date = TRADE_DATE + timedelta(days=1)
        next_day_signal = SignalEvent(
            meta=metadata("signal", 10, "next-trading-day"),
            strategy_id="strategy-a",
            strategy_version=1,
            symbol="TMF",
            contract="TMFU6",
            direction="long",
            action="enter",
            reference_price=100,
            stop_loss_price=95,
            trading_date=next_date,
            reason="new_day",
        )
        self.engine.publish(next_day_signal)
        self.engine.run()

        decision = self.engine.events("risk_decision")[-1]
        self.assertTrue(decision.approved)
        self.assertFalse(self.gate.snapshot("owner-1").kill_switch_active)

    def test_session_close_cancels_unfilled_entries_and_releases_reservation(self):
        self.engine.publish(signal(0, "pending-entry", "enter", "long"))
        self.engine.run()
        closing = SessionEvent(
            meta=metadata("session", 1, "closing", owner=None),
            symbol="TMF",
            contract="TMFU6",
            session="night",
            trading_date=TRADE_DATE,
            action="closing",
        )
        self.engine.publish(closing)
        self.engine.run()

        record = next(iter(self.pipeline.broker.orders.values()))
        self.assertEqual(record.status, "rejected")
        self.assertEqual(record.status_reason, "session_closed_before_fill")
        self.assertEqual(self.gate.snapshot("owner-1").reserved_contracts, 0)

    def test_default_pipeline_fails_closed_without_configured_risk_gate(self):
        engine = DeterministicEventEngine()
        pipeline = SimulatedExecutionPipeline(costs=ZERO_COSTS)
        pipeline.install(engine)
        engine.publish(signal(0, "unconfigured", "enter", "long"))
        engine.run()

        decision = engine.events("risk_decision")[0]
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "risk_gate_not_configured")


if __name__ == "__main__":
    unittest.main()

from datetime import date, datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from tw_quant.events import (
    BarClosedEvent,
    DeterministicEventEngine,
    EventMetadata,
    FillEvent,
    OrderIntent,
    RiskDecision,
    SessionEvent,
    SignalEvent,
)
from tw_quant.execution import (
    PassThroughRiskGate,
    PositionLedger,
    SimulatedBroker,
    SimulatedExecutionPipeline,
)
from tw_quant_core.futures_costs import FuturesCostConfig


TAIPEI = ZoneInfo("Asia/Taipei")
START = datetime(2026, 9, 1, 8, 45, tzinfo=TAIPEI)
ZERO_TAX_COSTS = FuturesCostConfig(
    multiplier=10,
    commission_per_side=10,
    tax_rate=0,
    slippage_points=1,
)


def meta(kind, minute, key, *, owner="owner-1", cause=None, correlation=None):
    return EventMetadata.create(
        kind=kind,
        occurred_at=START + timedelta(minutes=minute),
        source="test",
        source_key=key,
        owner_id=owner,
        causation_id=cause,
        correlation_id=correlation,
    )


def bar(minute, *, contract="TMFU6", open_price=100, close_price=None):
    close = open_price if close_price is None else close_price
    return BarClosedEvent(
        meta=meta("bar_closed", minute, f"bar:{contract}:{minute}", owner=None),
        symbol="TMF",
        contract=contract,
        timeframe="1m",
        open=open_price,
        high=max(open_price, close) + 1,
        low=min(open_price, close) - 1,
        close=close,
        volume=100,
        session="day",
        trading_date=date(2026, 9, 1),
    )


def signal(minute, action, direction, key, *, owner="owner-1", contract="TMFU6"):
    return SignalEvent(
        meta=meta("signal", minute, key, owner=owner),
        strategy_id="strategy-a",
        strategy_version=1,
        symbol="TMF",
        contract=contract,
        direction=direction,
        action=action,
        reference_price=100,
        reason="test_signal",
    )


class SimulatedExecutionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.engine = DeterministicEventEngine()
        self.pipeline = SimulatedExecutionPipeline(
            costs=ZERO_TAX_COSTS,
            risk_gate=PassThroughRiskGate(),
        )
        self.pipeline.install(self.engine)

    def open_long(self):
        self.engine.publish(bar(0, open_price=100))
        self.engine.publish(signal(0, "enter", "long", "enter-long"))
        self.engine.publish(bar(1, open_price=100, close_price=100))
        self.engine.run()

    def test_signal_fills_at_next_bar_open_and_updates_position(self):
        self.open_long()

        fills = self.engine.events("fill")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].price, 101)
        self.assertEqual(fills[0].commission, 10)
        self.assertEqual(fills[0].meta.occurred_at, START + timedelta(minutes=1))
        state = self.pipeline.ledger.state("owner-1", "strategy-a", 1, "TMF", "TMFU6")
        self.assertEqual(state.quantity, 1)
        self.assertEqual(state.average_price, 101)
        self.assertEqual(self.engine.events("position")[-1].unrealized_pnl, -10)

    def test_long_exit_realizes_net_pnl_with_both_side_costs(self):
        self.open_long()
        self.engine.publish(signal(1, "exit", "long", "exit-long"))
        self.engine.publish(bar(2, open_price=110, close_price=110))
        self.engine.run()

        state = self.pipeline.ledger.state("owner-1", "strategy-a", 1, "TMF", "TMFU6")
        trade = self.pipeline.ledger.trades[0]
        self.assertEqual(state.quantity, 0)
        self.assertEqual(trade.entry_price, 101)
        self.assertEqual(trade.exit_price, 109)
        self.assertEqual(trade.gross_pnl, 80)
        self.assertEqual(trade.total_cost, 20)
        self.assertEqual(trade.net_pnl, 60)
        self.assertEqual(trade.exit_reason, "test_signal")
        self.assertEqual(self.engine.events("position")[-1].realized_pnl, 60)

    def test_short_entry_and_cover_use_directional_slippage(self):
        self.engine.publish(bar(0, open_price=100))
        self.engine.publish(signal(0, "enter", "short", "enter-short"))
        self.engine.publish(bar(1, open_price=100))
        self.engine.run()
        self.engine.publish(signal(1, "exit", "short", "exit-short"))
        self.engine.publish(bar(2, open_price=90))
        self.engine.run()

        trade = self.pipeline.ledger.trades[0]
        self.assertEqual(trade.direction, "short")
        self.assertEqual(trade.entry_price, 99)
        self.assertEqual(trade.exit_price, 91)
        self.assertEqual(trade.net_pnl, 60)

    def test_mark_to_market_emits_unrealized_pnl_on_later_bar(self):
        self.open_long()
        self.engine.publish(bar(2, open_price=104, close_price=105))
        self.engine.run()

        position = self.engine.events("position")[-1]
        self.assertEqual(position.quantity, 1)
        self.assertEqual(position.unrealized_pnl, 30)
        self.assertEqual(position.realized_pnl, 0)

    def test_owner_positions_are_isolated(self):
        self.engine.publish(bar(0))
        self.engine.publish(signal(0, "enter", "long", "owner-a", owner="owner-a"))
        self.engine.publish(signal(0, "enter", "short", "owner-b", owner="owner-b"))
        self.engine.publish(bar(1))
        self.engine.run()

        long_state = self.pipeline.ledger.state(
            "owner-a", "strategy-a", 1, "TMF", "TMFU6"
        )
        short_state = self.pipeline.ledger.state(
            "owner-b", "strategy-a", 1, "TMF", "TMFU6"
        )
        self.assertEqual(long_state.quantity, 1)
        self.assertEqual(short_state.quantity, -1)

    def test_exit_without_position_does_not_create_order(self):
        self.engine.publish(signal(0, "exit", "long", "orphan-exit"))
        self.engine.run()
        self.assertEqual(self.engine.events("order_intent"), ())

    def test_two_reduce_only_exits_cannot_reverse_the_position(self):
        self.open_long()
        self.engine.publish(signal(1, "exit", "long", "exit-a"))
        self.engine.publish(signal(1, "exit", "long", "exit-b"))
        self.engine.run()
        self.engine.publish(bar(2, open_price=110))
        self.engine.run()

        state = self.pipeline.ledger.state("owner-1", "strategy-a", 1, "TMF", "TMFU6")
        statuses = [record.status for record in self.pipeline.broker.orders.values()]
        self.assertEqual(state.quantity, 0)
        self.assertEqual(len(self.pipeline.ledger.trades), 1)
        self.assertEqual(statuses.count("filled"), 2)  # entry and one exit
        self.assertEqual(statuses.count("rejected"), 1)

    def test_session_closing_liquidates_at_last_close(self):
        self.open_long()
        closing = SessionEvent(
            meta=meta("session", 2, "session-closing", owner=None),
            symbol="TMF",
            contract="TMFU6",
            session="day",
            trading_date=date(2026, 9, 1),
            action="closing",
        )
        self.engine.publish(closing)
        self.engine.run()

        trade = self.pipeline.ledger.trades[0]
        self.assertEqual(trade.exit_reason, "session_end")
        self.assertEqual(trade.exit_price, 99)
        self.assertEqual(
            self.pipeline.ledger.state("owner-1", "strategy-a", 1, "TMF", "TMFU6").quantity,
            0,
        )

    def test_contract_roll_liquidates_old_contract(self):
        self.open_long()
        self.engine.publish(bar(2, contract="TMFV6", open_price=102))
        self.engine.run()

        trade = self.pipeline.ledger.trades[0]
        self.assertEqual(trade.contract, "TMFU6")
        self.assertEqual(trade.exit_reason, "contract_roll")
        self.assertEqual(trade.exit_price, 99)


class ExecutionComponentTests(unittest.TestCase):
    def test_signal_price_execution_is_disabled_by_default(self):
        broker = SimulatedBroker(ZERO_TAX_COSTS)
        intent = OrderIntent(
            meta=meta("order_intent", 0, "historical-price-order"),
            order_id="historical-price-order",
            strategy_id="strategy-a",
            strategy_version=1,
            symbol="TMF",
            contract="TMFU6",
            side="buy",
            quantity=1,
            execution_timing="signal_price",
            reference_price=100,
        )
        broker.on_order(intent)
        decision = RiskDecision(
            meta=meta("risk_decision", 0, "historical-price-approved"),
            order_id=intent.order_id,
            approved=True,
            approved_quantity=1,
            reason="test",
        )
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            broker.on_risk_decision(decision)

    def test_rejected_risk_decision_never_fills(self):
        broker = SimulatedBroker(ZERO_TAX_COSTS)
        order = OrderIntent(
            meta=meta("order_intent", 0, "rejected-order"),
            order_id="order-1",
            strategy_id="strategy-a",
            strategy_version=1,
            symbol="TMF",
            contract="TMFU6",
            side="buy",
            quantity=1,
        )
        broker.on_order(order)
        rejected = RiskDecision(
            meta=meta("risk_decision", 0, "rejected-risk"),
            order_id=order.order_id,
            approved=False,
            approved_quantity=0,
            reason="test_rejected",
        )
        self.assertIsNone(broker.on_risk_decision(rejected))
        self.assertIsNone(broker.on_bar(bar(1)))
        self.assertEqual(broker.orders[order.order_id].status, "rejected")

    def test_risk_decision_cannot_increase_order_quantity(self):
        broker = SimulatedBroker(ZERO_TAX_COSTS)
        order = OrderIntent(
            meta=meta("order_intent", 0, "bounded-order"),
            order_id="order-bounded",
            strategy_id="strategy-a",
            strategy_version=1,
            symbol="TMF",
            contract="TMFU6",
            side="buy",
            quantity=1,
        )
        broker.on_order(order)
        decision = RiskDecision(
            meta=meta("risk_decision", 0, "oversized-approval"),
            order_id=order.order_id,
            approved=True,
            approved_quantity=2,
            reason="invalid_increase",
        )
        with self.assertRaisesRegex(ValueError, "cannot increase"):
            broker.on_risk_decision(decision)

    def test_duplicate_fill_is_idempotent_in_ledger(self):
        ledger = PositionLedger(multiplier=10)
        fill = FillEvent(
            meta=meta("fill", 1, "fill-1"),
            fill_id="fill-1",
            order_id="order-1",
            strategy_id="strategy-a",
            strategy_version=1,
            symbol="TMF",
            contract="TMFU6",
            side="buy",
            quantity=1,
            price=100,
            commission=10,
        )
        self.assertIsNotNone(ledger.on_fill(fill))
        self.assertIsNone(ledger.on_fill(fill))
        self.assertEqual(
            ledger.state("owner-1", "strategy-a", 1, "TMF", "TMFU6").quantity,
            1,
        )

    def test_position_flip_closes_old_side_and_opens_residual(self):
        ledger = PositionLedger(multiplier=10)
        first = FillEvent(
            meta=meta("fill", 0, "open-two"),
            fill_id="open-two",
            order_id="order-open",
            strategy_id="strategy-a",
            strategy_version=1,
            symbol="TMF",
            contract="TMFU6",
            side="buy",
            quantity=2,
            price=100,
            commission=20,
        )
        flip = FillEvent(
            meta=meta("fill", 1, "flip-three"),
            fill_id="flip-three",
            order_id="order-flip",
            strategy_id="strategy-a",
            strategy_version=1,
            symbol="TMF",
            contract="TMFU6",
            side="sell",
            quantity=3,
            price=110,
            commission=30,
            reason="flip",
        )
        ledger.on_fill(first)
        ledger.on_fill(flip)

        state = ledger.state("owner-1", "strategy-a", 1, "TMF", "TMFU6")
        self.assertEqual(state.quantity, -1)
        self.assertEqual(state.average_price, 110)
        self.assertEqual(state.entry_commission, 10)
        self.assertEqual(ledger.trades[0].gross_pnl, 200)
        self.assertEqual(ledger.trades[0].net_pnl, 160)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from public_fixtures import InertProvider, PublicTestCase, PublicAsyncTestCase, synthetic_csv, services

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tw_quant.auth import AccountStatus, AuthUser, Role, TradingMode
from tw_quant.events import EventMetadata, SessionEvent
from tw_quant.execution import SimulatedBroker
from tw_quant.live.application import (
    PaperAutoEntryController,
    TradingRuntimeApplicationService,
    auto_entry_idempotency_key,
    auto_exit_idempotency_key,
)
from tw_quant.live.application.errors import InvalidInputError
from tw_quant.live.storage import SQLiteBarRepository
from tw_quant.market import KBar
from tw_quant.paper import (
    PaperOrderCommand,
    PaperTradingService,
    SQLitePaperRepository,
)


TAIPEI = ZoneInfo("Asia/Taipei")


def market_bar(
    minute: int,
    close: float,
    *,
    open_price: float | None = None,
    high: float | None = None,
    low: float | None = None,
    contract: str = "TMFU6",
) -> KBar:
    timestamp = datetime(2026, 9, 10, 15, 0, tzinfo=TAIPEI) + timedelta(minutes=minute)
    return KBar(
        symbol="TMF", contract=contract, time=timestamp,
        open=open_price if open_price is not None else close,
        high=high if high is not None else max(close, open_price or close) + 0.2,
        low=low if low is not None else min(close, open_price or close) - 0.2,
        close=close, volume=100, status="closed", session="night",
        trading_date=date(2026, 9, 11), first_tick_time=timestamp,
        last_tick_time=timestamp + timedelta(seconds=50),
        exchange_time=timestamp + timedelta(seconds=50),
        received_time=timestamp + timedelta(seconds=50, milliseconds=5),
        latency_ms=5,
    )


class FakeUsers:
    def __init__(self, user: AuthUser):
        self.user = user

    def user_by_id(self, user_id: str) -> AuthUser | None:
        return self.user if user_id == self.user.user_id else None


class FakeMarket:
    def __init__(self):
        self.service_status = "healthy"
        self.block_reason: str | None = None

    def status_message(self) -> dict[str, object]:
        return {
            "service_status": self.service_status,
            "trading_block_reason": self.block_reason,
        }


class PaperAutoEntryTests(PublicTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "paper-auto.sqlite3"
        self.repo = SQLiteBarRepository(self.path)
        self.paper = PaperTradingService(SQLitePaperRepository(self.path), strategy_services=services())
        self.user = AuthUser(
            user_id="owner-a", email="owner@example.com", role=Role.TRADER,
            status=AccountStatus.ACTIVE, trading_mode=TradingMode.PAPER,
            permissions=("orders.paper", "positions.read.own"),
        )
        self.users = FakeUsers(self.user)
        self.market = FakeMarket()
        self.runtime_service = TradingRuntimeApplicationService(
            self.repo, self.repo, self.repo, "TMF"
        )
        self.controller = PaperAutoEntryController(
            self.repo, self.users, self.market, self.paper
        )
        self.runtime_service.add_decision_listener(self.controller.on_decision)
        self.runtime = self.runtime_service.create(
            {"strategy_id": "scripted", "mode": "paper_auto", "quantity": 2},
            self.user.user_id,
        )
        self.runtime = self.runtime_service.arm(
            str(self.runtime["runtime_id"]), self.user.user_id
        )

    def tearDown(self):
        self.paper.close()
        self.repo.close()
        self.temp.cleanup()

    def _decision(self, minute: int = 20) -> tuple[dict[str, object], KBar]:
        bar = market_bar(minute, 9_000.0)
        trigger = bar.time.isoformat(timespec="milliseconds")
        decision = {
            "decision_id": f"decision-{minute}",
            "runtime_id": self.runtime["runtime_id"],
            "owner_user_id": self.user.user_id,
            "strategy_id": "scripted", "strategy_version": None,
            "symbol": "TMF", "contract": "TMFU6", "interval": "1m",
            "trigger_time": trigger, "direction": "long", "action": "entry",
            "reason": "scripted entry", "context": {"test_value": -4.0},
            "source_bar_time": trigger, "execution_status": "pending",
            "source_bar_id": f"bar-{minute}",
            "reference_price": bar.close,
        }
        inserted = self.repo.record_runtime_evaluation(
            str(self.runtime["runtime_id"]), trigger, [decision]
        )
        self.assertEqual(len(inserted), 1)
        return decision, bar

    def _exit_decision(
        self, minute: int = 22, *, reason: str = "scripted exit"
    ) -> tuple[dict[str, object], KBar]:
        bar = market_bar(minute, 9_020.0)
        trigger = bar.time.isoformat(timespec="milliseconds")
        decision = {
            "decision_id": f"exit-decision-{minute}",
            "runtime_id": self.runtime["runtime_id"],
            "owner_user_id": self.user.user_id,
            "strategy_id": "scripted", "strategy_version": None,
            "symbol": "TMF", "contract": "TMFU6", "interval": "1m",
            "trigger_time": trigger, "direction": "long", "action": "exit",
            "reason": reason, "context": {"test_value": 0.0},
            "source_bar_time": trigger, "execution_status": "pending",
            "source_bar_id": f"bar-{minute}",
            "reference_price": bar.close,
        }
        inserted = self.repo.record_runtime_evaluation(
            str(self.runtime["runtime_id"]), trigger, [decision]
        )
        self.assertEqual(len(inserted), 1)
        return decision, bar

    def _open_auto_long(self) -> tuple[dict[str, object], KBar]:
        decision, signal = self._decision()
        self.controller.on_decision(self.runtime, decision, signal)
        fill_bar = market_bar(21, 9_006.0, open_price=9_005.0)
        self.controller.before_bar(fill_bar)
        self.paper.on_bar(fill_bar)
        self.controller.after_bar(fill_bar)
        self.assertEqual(len(self.paper.positions(self.user.user_id)), 1)
        return decision, fill_bar

    def test_entry_decision_routes_to_paper_and_fills_next_bar_open(self):
        decision, signal = self._decision()
        self.controller.on_decision(self.runtime, decision, signal)
        orders = self.paper.orders(self.user.user_id)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "approved")
        self.assertEqual(orders[0]["execution_timing"], "next_bar_open")
        self.assertEqual(orders[0]["order_source"], "strategy_auto")
        self.assertEqual(orders[0]["runtime_id"], self.runtime["runtime_id"])
        self.assertEqual(orders[0]["reference_price"], 9_000.0)
        self.assertAlmostEqual(orders[0]["stop_loss_price"], 8_946.0)

        next_bar = market_bar(21, 9_006.0, open_price=9_005.0)
        self.controller.before_bar(next_bar)
        self.paper.on_bar(next_bar)
        self.controller.after_bar(next_bar)
        fill = self.paper.fills(self.user.user_id)[0]
        expected_fill = 9_005.0 + self.paper.pipeline.broker.costs.slippage_points
        self.assertAlmostEqual(fill["price"], expected_fill)
        stored = self.repo.trading_decisions(
            str(self.runtime["runtime_id"]), self.user.user_id, 10
        )[0]
        self.assertEqual(stored["execution_status"], "filled")
        self.assertEqual(stored["reference_price"], 9_000.0)
        self.assertAlmostEqual(stored["planned_stop_price"], 8_946.0)
        self.assertAlmostEqual(stored["actual_fill_price"], expected_fill)
        position = self.paper.positions(self.user.user_id)[0]
        self.assertEqual(position["runtime_id"], self.runtime["runtime_id"])
        self.assertEqual(position["decision_id"], decision["decision_id"])
        self.assertEqual(position["order_source"], "strategy_auto")
        self.assertEqual(position["entry_fill_price"], expected_fill)
        self.assertAlmostEqual(position["stop_loss_price"], expected_fill * 0.994)
        self.assertAlmostEqual(position["take_profit_price"], expected_fill * 1.012)
        self.assertEqual(position["strategy_snapshot"], self.runtime["strategy_snapshot"])

    def test_deterministic_idempotency_and_duplicate_callback_make_one_order(self):
        decision, signal = self._decision()
        first_key = auto_entry_idempotency_key(
            self.user.user_id, str(self.runtime["runtime_id"]), "TMFU6",
            str(decision["trigger_time"]), "long",
        )
        second_key = auto_entry_idempotency_key(
            self.user.user_id, str(self.runtime["runtime_id"]), "TMFU6",
            str(decision["trigger_time"]), "long",
        )
        self.assertEqual(first_key, second_key)
        self.controller.on_decision(self.runtime, decision, signal)
        self.controller.on_decision(self.runtime, decision, signal)
        self.assertEqual(len(self.paper.orders(self.user.user_id)), 1)
        duplicate = self.repo.record_runtime_evaluation(
            str(self.runtime["runtime_id"]), str(decision["trigger_time"]),
            [decision],
        )
        self.assertEqual(duplicate, [])
        metrics = self.runtime_service.health()
        self.assertEqual(metrics["duplicate_decisions_blocked"], 1)
        self.assertEqual(metrics["armed_runtimes"], 1)
        self.assertEqual(metrics["decisions"], 1)

    def test_gap_risk_is_rejected_before_fill_and_counted(self):
        decision, signal = self._decision()
        self.controller.on_decision(self.runtime, decision, signal)
        planned_stop = float(self.paper.orders(self.user.user_id)[0][
            "stop_loss_price"
        ])
        gap = market_bar(
            21, planned_stop - 10, open_price=planned_stop - 5,
            high=planned_stop + 1, low=planned_stop - 20,
        )
        self.paper.on_bar(gap)
        self.controller.after_bar(gap)

        order = self.paper.orders(self.user.user_id)[0]
        self.assertEqual(order["status"], "rejected")
        self.assertEqual(order["status_reason"], "gap_risk_exceeded")
        self.assertEqual(self.paper.fills(self.user.user_id), [])
        self.assertEqual(self.paper.positions(self.user.user_id), [])
        self.assertEqual(self.paper.health()["gap_risk_rejected"], 1)
        stored = self.repo.trading_decision(
            str(decision["decision_id"]), self.user.user_id
        )
        self.assertEqual(stored["execution_status"], "rejected")
        self.assertEqual(stored["execution_reason"], "gap_risk_exceeded")

    def test_restart_locks_runtime_and_rejects_recovered_pending_entry(self):
        decision, signal = self._decision()
        self.controller.on_decision(self.runtime, decision, signal)
        self.paper.close()
        self.paper = PaperTradingService(SQLitePaperRepository(self.path), strategy_services=services())
        self.controller = PaperAutoEntryController(
            self.repo, self.users, self.market, self.paper
        )
        report = self.controller.recover()

        restored = self.repo.trading_runtime(
            str(self.runtime["runtime_id"]), self.user.user_id
        )
        self.assertEqual(restored["status"], "recovery_locked")
        self.assertIsNone(restored["recovery_issue"])
        self.assertEqual(report["locked_runtimes"], 1)
        order = self.paper.orders(self.user.user_id)[0]
        self.assertEqual(order["status"], "rejected")
        self.assertEqual(order["status_reason"], "stale_or_recovered_signal")
        stored = self.repo.trading_decision(
            str(decision["decision_id"]), self.user.user_id
        )
        self.assertEqual(stored["execution_status"], "skipped")
        self.assertEqual(
            stored["execution_reason"], "stale_or_recovered_signal"
        )
        rearmed = self.runtime_service.arm(
            str(self.runtime["runtime_id"]), self.user.user_id
        )
        self.assertEqual(rearmed["status"], "armed")

    def test_recovery_mismatch_stays_locked_but_reduce_only_still_works(self):
        self._open_auto_long()
        self.repo.connection.execute(
            "UPDATE strategy_runtimes SET strategy_id='ma_crossover' "
            "WHERE runtime_id=?", (self.runtime["runtime_id"],)
        )
        self.repo.connection.commit()
        self.paper.close()
        self.paper = PaperTradingService(SQLitePaperRepository(self.path), strategy_services=services())
        self.controller = PaperAutoEntryController(
            self.repo, self.users, self.market, self.paper
        )
        self.controller.recover()

        restored = self.repo.trading_runtime(
            str(self.runtime["runtime_id"]), self.user.user_id
        )
        self.assertEqual(restored["status"], "recovery_locked")
        self.assertIn("paper_runtime_attribution_mismatch", str(
            restored["recovery_issue"]
        ))
        with self.assertRaises(InvalidInputError):
            self.runtime_service.arm(
                str(self.runtime["runtime_id"]), self.user.user_id
            )

        position = self.paper.positions(self.user.user_id)[0]
        stop = float(position["stop_loss_price"])
        trigger = market_bar(
            22, stop - 2, open_price=stop + 1,
            high=stop + 3, low=stop - 5,
        )
        self.controller.before_bar(trigger)
        self.paper.on_bar(trigger)
        self.controller.after_bar(trigger)
        self.assertEqual(self.paper.positions(self.user.user_id), [])
        self.assertEqual(
            self.paper.pipeline.ledger.trades[-1].exit_reason, "stop_loss"
        )

    def test_auto_audit_chain_links_decision_order_risk_fill_and_position(self):
        decision, signal = self._decision()
        self.controller.on_decision(self.runtime, decision, signal)
        fill_bar = market_bar(21, 9_006.0, open_price=9_005.0)
        self.paper.on_bar(fill_bar)
        self.controller.after_bar(fill_bar)
        events = list(reversed(self.paper.events(self.user.user_id, 50)))
        order = next(item for item in events if item["kind"] == "order_intent")
        prefill = next(
            item for item in events
            if item["kind"] == "risk_decision" and item.get("phase") == "prefill"
        )
        fill = next(item for item in events if item["kind"] == "fill")
        position = next(
            item for item in events
            if item["kind"] == "position" and int(item["quantity"]) != 0
        )
        self.assertEqual(order["meta"]["causation_id"], decision["decision_id"])
        self.assertEqual(order["meta"]["correlation_id"], decision["decision_id"])
        self.assertEqual(prefill["meta"]["causation_id"], order["meta"]["event_id"])
        self.assertEqual(fill["meta"]["causation_id"], prefill["meta"]["event_id"])
        self.assertEqual(position["meta"]["causation_id"], fill["meta"]["event_id"])
        self.assertTrue(self.repo.trading_decision(
            str(decision["decision_id"]), self.user.user_id
        )["source_bar_id"])

    def test_existing_runtime_position_skips_new_entry_without_pyramiding(self):
        first, signal = self._decision()
        self.controller.on_decision(self.runtime, first, signal)
        next_bar = market_bar(21, 9_001.0)
        self.controller.before_bar(next_bar)
        self.paper.on_bar(next_bar)
        self.controller.after_bar(next_bar)
        second, second_bar = self._decision(22)
        self.controller.on_decision(self.runtime, second, second_bar)
        decisions = self.repo.trading_decisions(
            str(self.runtime["runtime_id"]), self.user.user_id, 10
        )
        newest = next(item for item in decisions if item["decision_id"] == "decision-22")
        self.assertEqual(newest["execution_status"], "skipped")
        self.assertEqual(newest["execution_reason"], "position_already_open")
        self.assertEqual(len(self.paper.orders(self.user.user_id)), 1)

    def test_fail_closed_market_account_permission_and_runtime_gates(self):
        cases = (
            ("market_stale", {"block_reason": "market_stale"}),
            ("provider_disconnected", {"block_reason": "provider_disconnected"}),
            ("recovery_degraded", {}),
            ("kill_switch_active", {}),
            ("permission_missing", {}),
            ("account_inactive", {}),
            ("paper_mode_disabled", {}),
        )
        for index, (expected, changes) in enumerate(cases, start=30):
            with self.subTest(reason=expected):
                decision, signal = self._decision(index)
                self.market.block_reason = changes.get("block_reason")
                original_user = self.users.user
                original_recovery = self.paper.recovery
                if expected == "permission_missing":
                    self.users.user = AuthUser(
                        **{**original_user.__dict__, "permissions": ()}
                    )
                elif expected == "account_inactive":
                    self.users.user = AuthUser(
                        **{**original_user.__dict__, "status": AccountStatus.SUSPENDED}
                    )
                elif expected == "paper_mode_disabled":
                    self.users.user = AuthUser(
                        **{**original_user.__dict__, "trading_mode": TradingMode.DISABLED}
                    )
                elif expected == "kill_switch_active":
                    self.paper.activate_kill_switch(self.user.user_id, "test")
                elif expected == "recovery_degraded":
                    self.paper.recovery = type(original_recovery)(
                        recovered_at=original_recovery.recovered_at,
                        restored_orders=0, restored_fills=0, restored_positions=0,
                        issues_by_owner={self.user.user_id: ("test",)}, duration_ms=0,
                    )
                self.controller.on_decision(self.runtime, decision, signal)
                stored = next(
                    item for item in self.repo.trading_decisions(
                        str(self.runtime["runtime_id"]), self.user.user_id, 100
                    ) if item["decision_id"] == decision["decision_id"]
                )
                self.assertEqual(stored["execution_reason"], expected)
                self.assertEqual(stored["execution_status"], "skipped")
                self.assertEqual(self.paper.orders(self.user.user_id), [])
                self.market.block_reason = None
                self.users.user = original_user
                self.paper.recovery = original_recovery
                if expected == "kill_switch_active":
                    self.paper.reset_kill_switch(self.user.user_id, "reset")

    def test_approved_next_open_order_survives_service_restart(self):
        decision, signal = self._decision()
        self.controller.on_decision(self.runtime, decision, signal)
        self.paper.close()
        self.paper = PaperTradingService(SQLitePaperRepository(self.path), strategy_services=services())
        self.controller = PaperAutoEntryController(
            self.repo, self.users, self.market, self.paper
        )
        next_bar = market_bar(21, 9_002.0, open_price=9_001.0)
        self.controller.before_bar(next_bar)
        self.paper.on_bar(next_bar)
        self.controller.after_bar(next_bar)
        self.assertEqual(len(self.paper.fills(self.user.user_id)), 1)
        stored = self.repo.trading_decisions(
            str(self.runtime["runtime_id"]), self.user.user_id, 10
        )[0]
        self.assertEqual(stored["execution_status"], "filled")

    def test_strategy_exit_is_reduce_only_and_fills_next_bar_open(self):
        self._open_auto_long()
        decision, signal_bar = self._exit_decision()
        self.controller.on_decision(self.runtime, decision, signal_bar)
        order = self.paper.orders(self.user.user_id)[0]
        self.assertTrue(order["reduce_only"])
        self.assertEqual(order["execution_timing"], "next_bar_open")

        fill_bar = market_bar(23, 9_030.0, open_price=9_025.0)
        self.controller.before_bar(fill_bar)
        self.paper.on_bar(fill_bar)
        self.controller.after_bar(fill_bar)
        self.assertEqual(self.paper.positions(self.user.user_id), [])
        trade = self.paper.pipeline.ledger.trades[-1]
        self.assertEqual(trade.exit_reason, "scripted exit")
        self.assertEqual(trade.exit_time, fill_bar.received_time)
        stored = self.repo.trading_decisions(
            str(self.runtime["runtime_id"]), self.user.user_id, 10
        )
        exit_decision = next(item for item in stored if item["action"] == "exit")
        self.assertEqual(exit_decision["execution_status"], "filled")

    def test_paused_runtime_still_permits_managed_strategy_exit(self):
        self._open_auto_long()
        self.runtime = self.runtime_service.pause(
            str(self.runtime["runtime_id"]), self.user.user_id
        )
        decision, signal_bar = self._exit_decision(24)
        self.controller.on_decision(self.runtime, decision, signal_bar)
        order = self.paper.orders(self.user.user_id)[0]
        self.assertTrue(order["reduce_only"])
        self.assertEqual(order["execution_timing"], "next_bar_open")

        fill_bar = market_bar(25, 9_030.0, open_price=9_025.0)
        self.paper.on_bar(fill_bar)
        self.controller.after_bar(fill_bar)
        self.assertEqual(self.paper.positions(self.user.user_id), [])

    def test_paused_runtime_remains_evaluable_without_allowing_entry(self):
        self.runtime = self.runtime_service.pause(
            str(self.runtime["runtime_id"]), self.user.user_id
        )
        active = self.repo.active_trading_runtimes("TMF")
        self.assertEqual([item["runtime_id"] for item in active], [
            self.runtime["runtime_id"]
        ])
        decision, _signal = self._decision(26)
        stored = self.repo.trading_decision(
            str(decision["decision_id"]), self.user.user_id
        )
        self.assertIsNotNone(stored)

    def test_stop_loss_uses_conservative_gap_price(self):
        self._open_auto_long()
        position = self.paper.positions(self.user.user_id)[0]
        stop = float(position["stop_loss_price"])
        trigger_bar = market_bar(
            22, stop - 10, open_price=stop - 5,
            high=stop + 10, low=stop - 20,
        )
        self.controller.before_bar(trigger_bar)
        self.paper.on_bar(trigger_bar)
        self.controller.after_bar(trigger_bar)
        trade = self.paper.pipeline.ledger.trades[-1]
        self.assertEqual(trade.exit_reason, "stop_loss")
        self.assertAlmostEqual(
            trade.exit_price,
            trigger_bar.open - self.paper.pipeline.broker.costs.slippage_points,
        )

    def test_take_profit_uses_actual_entry_fill_level(self):
        self._open_auto_long()
        position = self.paper.positions(self.user.user_id)[0]
        take = float(position["take_profit_price"])
        trigger_bar = market_bar(
            22, take - 5, open_price=take - 10,
            high=take + 10, low=take - 20,
        )
        self.controller.before_bar(trigger_bar)
        self.paper.on_bar(trigger_bar)
        self.controller.after_bar(trigger_bar)
        trade = self.paper.pipeline.ledger.trades[-1]
        self.assertEqual(trade.exit_reason, "take_profit")
        self.assertAlmostEqual(
            trade.exit_price,
            take - self.paper.pipeline.broker.costs.slippage_points,
        )

    def test_protective_levels_survive_restart_and_paused_runtime(self):
        self._open_auto_long()
        before = self.paper.positions(self.user.user_id)[0]
        stop = float(before["stop_loss_price"])
        self.runtime = self.runtime_service.pause(
            str(self.runtime["runtime_id"]), self.user.user_id
        )
        self.paper.close()
        self.paper = PaperTradingService(SQLitePaperRepository(self.path), strategy_services=services())
        self.controller = PaperAutoEntryController(
            self.repo, self.users, self.market, self.paper
        )
        restored = self.paper.positions(self.user.user_id)[0]
        self.assertEqual(restored["strategy_snapshot"], self.runtime["strategy_snapshot"])
        self.assertEqual(restored["stop_loss_price"], stop)

        trigger_bar = market_bar(
            22, stop - 1, open_price=stop + 2,
            high=stop + 5, low=stop - 5,
        )
        self.controller.before_bar(trigger_bar)
        self.paper.on_bar(trigger_bar)
        self.controller.after_bar(trigger_bar)
        self.assertEqual(self.paper.positions(self.user.user_id), [])
        self.assertEqual(self.paper.pipeline.ledger.trades[-1].exit_reason, "stop_loss")

    def test_same_bar_stop_and_take_profit_prefers_stop_and_deduplicates(self):
        self._open_auto_long()
        position = self.paper.positions(self.user.user_id)[0]
        stop = float(position["stop_loss_price"])
        take = float(position["take_profit_price"])
        trigger_bar = market_bar(
            22, 9_010, open_price=9_010,
            high=take + 10, low=stop - 10,
        )
        self.controller.before_bar(trigger_bar)
        self.controller.before_bar(trigger_bar)
        self.paper.on_bar(trigger_bar)
        self.controller.after_bar(trigger_bar)
        exits = [fill for fill in self.paper.fills(self.user.user_id) if fill["purpose"] == "exit"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["reason"], "stop_loss")

    def test_stop_loss_beats_pending_strategy_exit(self):
        self._open_auto_long()
        decision, signal_bar = self._exit_decision()
        self.controller.on_decision(self.runtime, decision, signal_bar)
        position = self.paper.positions(self.user.user_id)[0]
        stop = float(position["stop_loss_price"])
        collision = market_bar(
            23, stop - 1, open_price=stop + 2,
            high=stop + 5, low=stop - 5,
        )
        self.controller.before_bar(collision)
        self.paper.on_bar(collision)
        self.controller.after_bar(collision)
        self.assertEqual(self.paper.pipeline.ledger.trades[-1].exit_reason, "stop_loss")
        strategy_order = next(
            item for item in self.paper.orders(self.user.user_id)
            if item.get("decision_id") == decision["decision_id"]
        )
        self.assertEqual(strategy_order["status"], "rejected")
        self.assertEqual(
            strategy_order["status_reason"], "reduce_only_position_unavailable"
        )

    def test_manual_emergency_close_prevents_later_auto_exit(self):
        self._open_auto_long()
        self.paper.submit(
            self.user,
            PaperOrderCommand(
                "scripted", 1, "sell", 2, None,
                reduce_only=True, reason="manual_emergency_close",
            ),
            idempotency_key="manual-emergency-close",
            market_bar=market_bar(22, 9_010),
        )
        decision, signal_bar = self._exit_decision(23)
        self.controller.on_decision(self.runtime, decision, signal_bar)
        stored = next(
            item for item in self.repo.trading_decisions(
                str(self.runtime["runtime_id"]), self.user.user_id, 10
            ) if item["decision_id"] == decision["decision_id"]
        )
        self.assertEqual(stored["execution_reason"], "position_not_open")
        self.assertEqual(len(self.paper.pipeline.ledger.trades), 1)

    def test_exit_idempotency_is_position_and_trigger_scoped(self):
        first = auto_exit_idempotency_key(
            self.user.user_id, str(self.runtime["runtime_id"]), "TMFU6",
            "2026-09-10T15:21:00+08:00", "2026-09-10T15:22:00+08:00",
            "stop_loss",
        )
        second = auto_exit_idempotency_key(
            self.user.user_id, str(self.runtime["runtime_id"]), "TMFU6",
            "2026-09-10T15:21:00+08:00", "2026-09-10T15:22:00+08:00",
            "stop_loss",
        )
        self.assertEqual(first, second)

    def test_session_end_does_not_duplicate_pending_auto_exit_fill(self):
        self._open_auto_long()
        decision, signal_bar = self._exit_decision(30)
        self.controller.on_decision(self.runtime, decision, signal_bar)
        event = SessionEvent(
            meta=EventMetadata.create(
                kind="session", occurred_at=signal_bar.received_time + timedelta(seconds=1),
                source="test", source_key="session-close",
            ),
            symbol="TMF", contract="TMFU6", session="night",
            trading_date=signal_bar.trading_date, action="closing",
        )
        self.paper.engine.publish(event)
        self.paper._run()
        self.assertEqual(self.paper.positions(self.user.user_id), [])
        exits = [
            fill for fill in self.paper.fills(self.user.user_id)
            if fill["purpose"] in {"exit", "liquidation"}
        ]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["reason"], "session_end")

    def test_contract_roll_does_not_duplicate_pending_auto_exit_fill(self):
        self._open_auto_long()
        decision, signal_bar = self._exit_decision(40)
        self.controller.on_decision(self.runtime, decision, signal_bar)
        self.paper.on_bar(market_bar(41, 9_030, contract="TMFV6"))
        self.assertEqual(self.paper.positions(self.user.user_id), [])
        exits = [
            fill for fill in self.paper.fills(self.user.user_id)
            if fill["purpose"] in {"exit", "liquidation"}
        ]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["reason"], "contract_roll")

    def test_auto_orders_are_owner_scoped_and_no_real_broker_is_used(self):
        decision, signal = self._decision()
        self.controller.on_decision(self.runtime, decision, signal)
        self.assertIsInstance(self.paper.pipeline.broker, SimulatedBroker)
        self.assertEqual(len(self.paper.orders(self.user.user_id)), 1)
        other_owner_orders = self.paper.orders("owner-b")
        self.assertEqual(other_owner_orders, [])

    def test_paused_and_stopped_runtime_cannot_execute(self):
        decision, signal = self._decision()
        self.runtime = self.runtime_service.pause(
            str(self.runtime["runtime_id"]), self.user.user_id
        )
        self.controller.on_decision(self.runtime, decision, signal)
        stored = self.repo.trading_decisions(
            str(self.runtime["runtime_id"]), self.user.user_id, 10
        )[0]
        self.assertEqual(stored["execution_reason"], "runtime_paused")
        stopped = self.runtime_service.stop(
            str(self.runtime["runtime_id"]), self.user.user_id
        )
        self.assertEqual(stopped["status"], "stopped")
        with self.assertRaisesRegex(InvalidInputError, "stopped runtime"):
            self.runtime_service.arm(
                str(self.runtime["runtime_id"]), self.user.user_id
            )

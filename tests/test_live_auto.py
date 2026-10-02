from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from tw_quant.broker import BrokerAccountRef, BrokerCapabilities, BrokerOrder, BrokerOrderRequest, BrokerOrderStatus
from tw_quant.execution.live_models import InstrumentSpec
from tw_quant.execution.live_policy import MarketableLimitIOCPolicy
from tw_quant.live.application.errors import InvalidInputError
from tw_quant.live.application.live_auto import LIVE_AUTO_CONFIRMATION, LiveAutoService
from tw_quant.live.storage import SQLiteBarRepository
from tw_quant.market import ExecutionQuote, ExecutionQuoteCache, KBar
from tw_quant.risk import LiveRiskApproval


NOW = datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc)
TARGET = BrokerAccountRef("shioaji", "account-secret-1234")


class Risk:
    def evaluate(self, *_):
        return LiveRiskApproval(True, "approved", "risk:v1")


class Sink:
    def __init__(self): self.requests = []
    def reserve(self, target, request):
        self.requests.append((target, request))
        return BrokerOrder(request, BrokerOrderStatus.CREATED, NOW), True


class Context:
    def __init__(self):
        self.ready = True; self.position = 0; self.entries = 0; self.events = []
    def preflight(self, runtime):
        return {key: self.ready for key in (
            "permission", "account_allowlist", "recovery_ready", "broker_connected",
            "market_healthy", "quote_fresh", "position_reconciled", "guardian_healthy",
            "no_unknown_orders", "no_unknown_external_orders", "no_unmanaged_position",
            "kill_switch_allows_entry", "live_risk_available", "production_acceptance_passed",
        )}
    def resolve_target(self, runtime):
        if runtime.get("execution_target_id") != "exec_0123456789abcdef":
            raise RuntimeError("live_auto_target_mismatch")
        if (runtime.get("broker_name"), runtime.get("account_id")) != (
            TARGET.broker_name, TARGET.account_id
        ):
            raise RuntimeError("live_auto_target_mismatch")
        return TARGET
    def risk_context(self, candidate): return object()
    def instrument(self, symbol, contract): return InstrumentSpec(symbol, contract, 1, 10, date(2026, 10, 21))
    def capabilities(self, target): return BrokerCapabilities(supports_limit_orders=True, supports_ioc=True)
    def active_position(self, owner, target, contract): return self.position
    def active_entry_orders(self, owner, target, contract): return self.entries
    def request_strategy_exit(self, runtime): return "guardian-exit-1"
    def audit(self, event, runtime, detail): self.events.append((event, detail))


def bar():
    return KBar("TMF", "TMF202610", NOW, 20000, 20002, 19999, 20001, 1,
                "closed", "night", NOW.date(), NOW, NOW, NOW, NOW, 0)


class LiveAutoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = SQLiteBarRepository(str(Path(self.temp.name) / "db.sqlite3"))
        self.runtime = self.repo.create_trading_runtime({
            "owner_user_id": "owner-1", "strategy_kind": "atomic", "strategy_id": "scripted",
            "strategy_version": 2, "strategy_snapshot": {"strategy": "scripted", "parameters": {}, "execution_contract": "TMF202610", "live_risk_config_version": "risk:v1", "execution_policy_version": "marketable-limit-ioc:v1"},
            "symbol": "TMF", "interval": "1m", "quantity": 1, "mode": "live_auto",
            "broker_name": TARGET.broker_name, "account_id": TARGET.account_id,
            "execution_target_id": "exec_0123456789abcdef",
        })
        self.context = Context(); self.sink = Sink(); self.quotes = ExecutionQuoteCache()
        self.quotes.update(ExecutionQuote("TMF", "TMF202610", 20000, 20002, 20001, NOW, NOW, "test"))
        self.service = LiveAutoService(enabled=True, runtimes=self.repo, context=self.context,
            quotes=self.quotes, risk=Risk(), policy=MarketableLimitIOCPolicy(1, 4),
            sink=self.sink, now=lambda: NOW)

    def tearDown(self): self.repo.close(); self.temp.cleanup()

    def test_defaults_disarmed_and_restart_has_no_arm(self):
        self.assertEqual(self.runtime["status"], "paused")
        self.service.arm(self.runtime["runtime_id"], "owner-1", LIVE_AUTO_CONFIRMATION)
        restarted = LiveAutoService(enabled=True, runtimes=self.repo, context=self.context,
            quotes=self.quotes, risk=Risk(), policy=MarketableLimitIOCPolicy(1, 4), sink=self.sink)
        self.assertFalse(restarted.status(self.runtime)["arm_active"])

    def test_arm_preflight_and_single_runtime(self):
        self.context.ready = False
        with self.assertRaises(InvalidInputError):
            self.service.arm(self.runtime["runtime_id"], "owner-1", LIVE_AUTO_CONFIRMATION)
        self.context.ready = True
        armed = self.service.arm(self.runtime["runtime_id"], "owner-1", LIVE_AUTO_CONFIRMATION)
        self.assertTrue(armed["arm_active"])

    def test_entry_is_durable_source_attributed_and_idempotent_keyed(self):
        self.service.arm(self.runtime["runtime_id"], "owner-1", LIVE_AUTO_CONFIRMATION)
        decision = {"decision_id": "decision-1", "action": "entry", "direction": "long",
                    "contract": "TMF202610", "trigger_time": NOW.isoformat(),
                    "reason": "signal", "reference_price": 20001}
        self.service.on_decision(self.runtime | {"status": "armed"}, decision, bar())
        request = self.sink.requests[0][1]
        self.assertEqual(request.source, "strategy_live_auto")
        self.assertEqual(request.quantity, 1)
        self.assertFalse(request.reduce_only)
        self.assertTrue(request.client_order_id.startswith("live-auto:"))

    def test_dispatch_recheck_position_and_unknown_lock(self):
        self.service.arm(self.runtime["runtime_id"], "owner-1", LIVE_AUTO_CONFIRMATION)
        self.context.position = 1
        decision = {"decision_id": "decision-2", "action": "entry", "direction": "long",
                    "contract": "TMF202610", "trigger_time": NOW.isoformat(), "reason": "signal"}
        self.service.on_decision(self.runtime | {"status": "armed"}, decision, bar())
        self.assertEqual(self.sink.requests, [])
        locked = self.service.lock_unknown(self.runtime["runtime_id"], "owner-1")
        self.assertEqual(locked["status"], "recovery_locked")
        self.assertFalse(locked["arm_active"])

    def test_strategy_exit_uses_guardian_only(self):
        decision = {"decision_id": "exit-1", "action": "exit", "direction": "long"}
        self.service.on_decision(self.runtime, decision, bar())
        self.assertEqual(self.sink.requests, [])


if __name__ == "__main__": unittest.main()

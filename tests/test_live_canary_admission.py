from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tw_quant.broker.canary import (
    LIVE_CANARY_CONFIRMATION,
    CanaryArmSession,
    CanaryOrderAdmissionGate,
    LiveCanaryConfig,
)
from tw_quant.broker.canary_repository import SQLiteCanaryArmRepository
from tw_quant.broker.capabilities import BrokerCapabilities
from tw_quant.broker.disabled import LockedBroker
from tw_quant.broker.identity import BrokerAccountRef
from tw_quant.broker.instruments import LockedInstrumentMapper
from tw_quant.broker.manager import LiveOrderManager
from tw_quant.broker.models import BrokerOrderRequest, ExecutionMode
from tw_quant.broker.ports import CompositeOrderAdmissionGate
from tw_quant.broker import (
    BrokerRegistration,
    BrokerRegistry,
    BrokerRuntimeState,
)
from tw_quant.broker.repository import SQLiteLiveOrderRepository
from tw_quant.execution.canary import LiveExecutionSink


TARGET = BrokerAccountRef("fake-broker", "account-1")
NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


class LiveCanaryAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "live.sqlite3"
        self.arms = SQLiteCanaryArmRepository(path)
        self.orders = SQLiteLiveOrderRepository(path)
        self.config = LiveCanaryConfig(
            enabled=True,
            allowed_owner_ids=frozenset({"owner-1"}),
            allowed_broker_accounts=frozenset({TARGET}),
            allowed_symbols=frozenset({"TMF"}),
            allowed_contracts=frozenset({"TMF202612"}),
        )
        self.arms.arm(CanaryArmSession(
            arm_id="arm-1",
            owner_id="owner-1",
            account_ref=TARGET,
            armed_at=NOW,
            expires_at=NOW.replace(minute=10),
            created_by="owner-1",
        ))
        registry = BrokerRegistry()
        registry.register(BrokerRegistration(
            TARGET,
            LockedBroker(TARGET.broker_name),
            BrokerCapabilities(),
            LockedInstrumentMapper(),
            state=BrokerRuntimeState.READY,
        ))
        registry.freeze()
        gate = CanaryOrderAdmissionGate(
            self.config,
            self.arms,
            TARGET,
            lambda: None,
            lambda: {"connected": True, "ca_ready": True},
            lambda _owner, _reduce_only: False,
            lambda: NOW,
        )
        self.sink = LiveExecutionSink(LiveOrderManager(
            self.orders,
            registry,
            {TARGET: CompositeOrderAdmissionGate((gate,))},
        ))

    def tearDown(self) -> None:
        self.orders.close()
        self.arms.close()
        self.temp.cleanup()

    def request(self, *, source: str = "manual_live_canary") -> BrokerOrderRequest:
        return BrokerOrderRequest(
            client_order_id="canary-1",
            owner_id="owner-1",
            strategy_id="manual-live-canary",
            strategy_version=1,
            symbol="TMF",
            contract="TMF202612",
            side="buy",
            quantity=1,
            mode=ExecutionMode.LIVE,
            order_type="limit",
            time_in_force="ioc",
            limit_price=101,
            source=source,
            arm_id="arm-1",
        )

    def test_manual_source_is_durable_before_any_broker_call(self) -> None:
        order, created = self.sink.reserve(TARGET, self.request())
        self.assertTrue(created)
        self.assertEqual(order.request.source, "manual_live_canary")
        self.assertEqual(self.orders.outbox_state("canary-1"), "pending")

    def test_non_manual_source_cannot_reach_live_sink(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "source"):
            self.sink.reserve(TARGET, self.request(source="strategy_live"))
        self.assertEqual(self.orders.orders(), [])

    def test_arm_confirmation_constant_is_explicit(self) -> None:
        self.assertEqual(
            LIVE_CANARY_CONFIRMATION,
            "I_UNDERSTAND_MANUAL_LIVE_CANARY",
        )


if __name__ == "__main__":
    unittest.main()

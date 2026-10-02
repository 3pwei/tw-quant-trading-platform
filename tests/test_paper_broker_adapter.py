from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from tw_quant.auth import AccountStatus, AuthUser, Role, TradingMode
from tw_quant.broker import (
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerPort,
    ExecutionMode,
)
from tw_quant.market import KBar, TAIPEI
from tw_quant.paper import (
    PaperBrokerAdapter,
    PaperTradingService,
    SQLitePaperRepository,
)


def user() -> AuthUser:
    return AuthUser(
        user_id="owner-1",
        email="trader@example.com",
        role=Role.TRADER,
        status=AccountStatus.ACTIVE,
        trading_mode=TradingMode.PAPER,
        permissions=("orders.paper", "positions.read.own"),
    )


def bar() -> KBar:
    at = datetime(2026, 9, 9, 9, 1, tzinfo=TAIPEI)
    return KBar(
        symbol="TMF",
        contract="TMFTEST",
        time=at,
        open=20_000,
        high=20_010,
        low=19_990,
        close=20_000,
        volume=10,
        status="closed",
        session="day",
        trading_date=at.date(),
        first_tick_time=at,
        last_tick_time=at,
        exchange_time=at,
        received_time=at,
        latency_ms=0,
    )


def request(**changes) -> BrokerOrderRequest:
    values = {
        "client_order_id": "paper-client-1",
        "owner_id": "owner-1",
        "strategy_id": "manual",
        "strategy_version": 1,
        "symbol": "TMF",
        "contract": "TMFTEST",
        "side": "buy",
        "quantity": 1,
        "mode": ExecutionMode.PAPER,
        "reference_price": 20_000,
        "risk_stop_price": 19_950,
    }
    values.update(changes)
    return BrokerOrderRequest(**values)


class PaperBrokerAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = SQLitePaperRepository(
            Path(self.temp.name) / "paper.sqlite3"
        )
        self.service = PaperTradingService(self.repository)
        self.adapter = PaperBrokerAdapter(self.service, user(), bar())

    def tearDown(self):
        self.repository.close()
        self.temp.cleanup()

    async def test_implements_typed_broker_contract_and_fills(self):
        broker: BrokerPort = self.adapter
        self.assertIsInstance(broker, BrokerPort)
        order = await broker.submit_order(request())
        self.assertEqual(order.status, BrokerOrderStatus.FILLED)
        self.assertEqual(order.filled_quantity, 1)
        self.assertEqual(order.average_fill_price, 20_001)
        self.assertEqual(order.request.reference_price, 20_000)
        self.assertEqual(order.request.mode, ExecutionMode.PAPER)
        self.assertEqual((await broker.account_state())["open_contracts"], 1)
        self.assertEqual(len(await broker.positions()), 1)

    async def test_same_client_order_is_idempotent_through_adapter(self):
        first = await self.adapter.submit_order(request())
        repeated = await self.adapter.submit_order(request())
        self.assertEqual(first.broker_order_id, repeated.broker_order_id)
        self.assertEqual(len(self.service.fills("owner-1")), 1)

    async def test_refresh_and_terminal_cancel_preserve_filled_order(self):
        filled = await self.adapter.submit_order(request())
        refreshed = await self.adapter.refresh_order(filled)
        cancelled = await self.adapter.cancel_order(refreshed)
        self.assertEqual(refreshed.status, BrokerOrderStatus.FILLED)
        self.assertEqual(cancelled, refreshed)

    async def test_rejects_live_mode_and_cross_owner_requests(self):
        with self.assertRaisesRegex(ValueError, "only accepts paper"):
            await self.adapter.submit_order(request(mode=ExecutionMode.LIVE))
        with self.assertRaisesRegex(ValueError, "owner"):
            await self.adapter.submit_order(request(owner_id="owner-2"))


if __name__ == "__main__":
    unittest.main()

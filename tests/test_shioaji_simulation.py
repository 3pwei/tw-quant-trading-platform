from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from tw_quant.broker import (
    BrokerAccountRef,
    BrokerCapabilities,
    BrokerOrder,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerRegistration,
    BrokerRegistry,
    CanonicalInstrument,
    ExecutionMode,
    LiveOrderManager,
    LiveTradingSafety,
    RoutedBrokerOrderRequest,
    ShioajiBrokerAdapter,
    ShioajiCallbackBridge,
    ShioajiSimulationExecutionClient,
    SQLiteLiveOrderRepository,
    normalize_callback,
    normalize_trade,
)


NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
TARGET = BrokerAccountRef("shioaji", "sim-1")


class FakeMapper:
    def to_broker_contract(self, instrument):
        return instrument.contract

    def to_canonical_instrument(self, broker_contract):
        return CanonicalInstrument("TMF", broker_contract)


def request(**changes: object) -> BrokerOrderRequest:
    values: dict[str, object] = {
        "client_order_id": "client-1",
        "owner_id": "owner-1",
        "strategy_id": "dow-channel",
        "strategy_version": 2,
        "symbol": "TMF",
        "contract": "TMF202609",
        "side": "buy",
        "quantity": 2,
        "mode": ExecutionMode.LIVE,
    }
    values.update(changes)
    return BrokerOrderRequest(**values)  # type: ignore[arg-type]


def trade(
    status: str = "Submitted",
    *,
    broker_order_id: str = "broker-1",
    deals: list[object] | None = None,
) -> object:
    return SimpleNamespace(
        contract=SimpleNamespace(code="TMF202609"),
        order=SimpleNamespace(id=broker_order_id, action="Buy"),
        status=SimpleNamespace(
            id=broker_order_id,
            status=status,
            deals=deals or [],
            msg="",
        ),
    )


class FakeFuturesOrder:
    def __init__(self, **values: object):
        self.values = values


class FakeSDK:
    FuturesOrder = FakeFuturesOrder
    constant = SimpleNamespace(
        Action=SimpleNamespace(Buy="BUY", Sell="SELL"),
        FuturesPriceType=SimpleNamespace(MKT="MKT", LMT="LMT"),
        OrderType=SimpleNamespace(ROD="ROD"),
        FuturesOCType=SimpleNamespace(Auto="AUTO"),
    )


class FakeAPI:
    def __init__(self, trades: list[object] | None = None):
        self.Contracts = SimpleNamespace(Futures={"TMF202609": object()})
        self.trades = trades or []
        self.calls: list[tuple[object, ...]] = []
        self.last_order: FakeFuturesOrder | None = None

    def place_order(self, contract: object, order: FakeFuturesOrder) -> object:
        self.calls.append(("place_order", contract))
        self.last_order = order
        placed = trade()
        self.trades.append(placed)
        return placed

    def update_status(self, account: object, *, trade: object | None = None) -> None:
        self.calls.append(("update_status", account, trade))

    def cancel_order(self, target: object) -> None:
        self.calls.append(("cancel_order", target))
        target.status.status = "Cancelled"

    def list_trades(self) -> list[object]:
        self.calls.append(("list_trades",))
        return self.trades

    def list_positions(self, account: object) -> list[object]:
        self.calls.append(("list_positions", account))
        return [SimpleNamespace(
            code="TMF202609",
            quantity=1,
            direction="Buy",
            dict=lambda: {"code": "TMF", "quantity": 1},
        )]


class TradeNormalizationTests(unittest.TestCase):
    def test_maps_every_documented_status(self):
        expected = {
            "PendingSubmit": "accepted",
            "PreSubmitted": "accepted",
            "Submitted": "accepted",
            "PartFilled": "partially_filled",
            "Filled": "filled",
            "Cancelled": "cancelled",
            "Failed": "rejected",
            "Inactive": "expired",
        }
        deals = [SimpleNamespace(price=100, quantity=2, ts=NOW)]
        for raw, canonical in expected.items():
            with self.subTest(raw=raw):
                report = normalize_trade(
                    trade(raw, deals=deals if "Filled" in raw else []),
                    now=lambda: NOW,
                )
                self.assertEqual(report.status, canonical)

    def test_aggregates_deals_using_quantity_weighted_average(self):
        deals = [
            SimpleNamespace(price=100, quantity=1, ts=NOW.timestamp()),
            SimpleNamespace(price=102, quantity=3, ts=NOW.timestamp()),
        ]
        report = normalize_trade(trade("Filled", deals=deals), now=lambda: NOW)
        self.assertEqual(report.filled_quantity, 4)
        self.assertEqual(report.average_fill_price, 101.5)
        self.assertEqual(report.occurred_at, NOW)

    def test_accepts_shioaji_nanosecond_deal_timestamp(self):
        deals = [SimpleNamespace(price=100, quantity=2, ts=int(NOW.timestamp() * 1e9))]
        report = normalize_trade(trade("Filled", deals=deals), now=lambda: NOW)
        self.assertEqual(report.occurred_at, NOW)

    def test_invalid_deal_timestamp_uses_injected_receive_time(self):
        deals = [SimpleNamespace(price=100, quantity=2, ts="invalid")]
        report = normalize_trade(trade("Filled", deals=deals), now=lambda: NOW)
        self.assertEqual(report.occurred_at, NOW)

    def test_rejects_filled_status_without_deal_details(self):
        with self.assertRaisesRegex(ValueError, "no valid deals"):
            normalize_trade(trade("Filled"), now=lambda: NOW)


class SimulationClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, api: FakeAPI | None = None) -> ShioajiSimulationExecutionClient:
        return ShioajiSimulationExecutionClient(
            api or FakeAPI(),
            FakeSDK,
            account=SimpleNamespace(account_id="sim-1"),
            simulation=True,
            now=lambda: NOW,
        )

    async def test_rejects_non_simulation_construction(self):
        with self.assertRaisesRegex(ValueError, "simulation=True"):
            ShioajiSimulationExecutionClient(
                FakeAPI(), FakeSDK, simulation=False
            )

    async def test_builds_market_and_limit_futures_orders(self):
        market_api = FakeAPI()
        await self.client(market_api).submit(request())
        self.assertEqual(
            market_api.last_order.values,
            {
                "action": "BUY",
                "price": 0,
                "quantity": 2,
                "price_type": "MKT",
                "order_type": "ROD",
                "octype": "AUTO",
                "account": market_api.last_order.values["account"],
            },
        )

        limit_api = FakeAPI()
        await self.client(limit_api).submit(
            request(side="sell", order_type="limit", limit_price=20_001)
        )
        self.assertEqual(limit_api.last_order.values["action"], "SELL")
        self.assertEqual(limit_api.last_order.values["price_type"], "LMT")
        self.assertEqual(limit_api.last_order.values["price"], 20_001)

    async def test_cancel_updates_status_before_and_after_original_trade(self):
        target = trade()
        api = FakeAPI([target])
        report = await self.client(api).cancel("broker-1")
        self.assertEqual(report.status, "cancelled")
        self.assertEqual(
            [call[0] for call in api.calls],
            ["update_status", "list_trades", "update_status", "cancel_order", "update_status"],
        )
        self.assertIs(api.calls[3][1], target)

    async def test_recovers_known_broker_order_after_process_restart(self):
        api = FakeAPI([trade("Submitted", broker_order_id="broker-recovered")])
        report = await self.client(api).order("broker-recovered")
        self.assertIsNotNone(report)
        self.assertEqual(report.broker_order_id, "broker-recovered")

    async def test_does_not_guess_client_id_after_ambiguous_submit(self):
        report = await self.client().order_by_client_id("client-1")
        self.assertIsNone(report)

    async def test_returns_simulation_account_and_normalized_positions(self):
        client = self.client()
        self.assertEqual((await client.account_state())["simulation"], True)
        self.assertEqual(await client.positions(), [{"code": "TMF", "quantity": 1}])

    async def test_builds_typed_order_fill_and_position_snapshot(self):
        deals = [SimpleNamespace(price=20_000, quantity=2, ts=NOW, seq="deal-1")]
        api = FakeAPI([trade("Filled", deals=deals)])
        snapshot = await self.client(api).reconciliation_snapshot()
        self.assertEqual(snapshot.broker_name, "shioaji")
        self.assertEqual(snapshot.account_id, "sim-1")
        self.assertEqual(snapshot.orders[0].status, BrokerOrderStatus.FILLED)
        self.assertEqual(snapshot.fills[0].fill_id, "broker-1:deal-1")
        self.assertEqual(snapshot.fills[0].quantity, 2)
        self.assertEqual(snapshot.positions[0].quantity, 1)


class CallbackBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalizes_and_enqueues_supported_callbacks(self):
        queue: asyncio.Queue = asyncio.Queue()
        bridge = ShioajiCallbackBridge(
            asyncio.get_running_loop(), TARGET, queue, now=lambda: NOW
        )
        bridge("FORDER", {"order_id": "broker-1", "status": "Submitted"})
        event = await asyncio.wait_for(queue.get(), 1)
        self.assertEqual(event.event_type, "FORDER")
        self.assertEqual(event.broker_order_id, "broker-1")
        self.assertEqual(event.received_at, NOW)

    async def test_ignores_malformed_callbacks(self):
        queue: asyncio.Queue = asyncio.Queue()
        bridge = ShioajiCallbackBridge(
            asyncio.get_running_loop(), TARGET, queue
        )
        bridge("STOCK", {"id": "not-a-futures-order"})
        bridge("FORDER", "invalid")
        await asyncio.sleep(0)
        self.assertTrue(queue.empty())

    async def test_can_delegate_queue_policy_to_execution_worker_sink(self):
        events = []

        def capture(event):
            events.append(event)
            return True

        bridge = ShioajiCallbackBridge(
            asyncio.get_running_loop(),
            TARGET,
            enqueue_callback=capture,
            now=lambda: NOW,
        )
        bridge("FORDER", {"order_id": "broker-1", "status": "Submitted"})
        await asyncio.sleep(0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].broker_order_id, "broker-1")

    def test_requires_exactly_one_callback_destination(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            ShioajiCallbackBridge(loop, TARGET)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            ShioajiCallbackBridge(
                loop,
                TARGET,
                asyncio.Queue(),
                enqueue_callback=lambda _event: True,
            )

    def test_callback_normalizer_is_pure_and_rejects_unknown_state(self):
        message = {"trade_id": "deal-1", "price": 20_000}
        event = normalize_callback(
            "FDEAL", message, account_ref=TARGET, now=lambda: NOW
        )
        self.assertEqual(event.payload["price"], 20_000)
        self.assertEqual(message, {"trade_id": "deal-1", "price": 20_000})
        with self.assertRaisesRegex(ValueError, "unsupported"):
            normalize_callback(
                "SORDER", message, account_ref=TARGET, now=lambda: NOW
            )


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconciles_broker_orders_without_resubmission(self):
        api = FakeAPI([trade("Submitted")])
        client = ShioajiSimulationExecutionClient(
            api,
            FakeSDK,
            account=SimpleNamespace(account_id="sim-1"),
            simulation=True,
            now=lambda: NOW,
        )
        safety = LiveTradingSafety(
            account_id="sim-1",
            enabled=True,
            confirmation="I_UNDERSTAND_LIVE_ORDERS",
            allowed_account_ids=frozenset({"sim-1"}),
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteLiveOrderRepository(Path(directory) / "orders.sqlite3")
            repository.reserve(
                RoutedBrokerOrderRequest(TARGET, request()), occurred_at=NOW
            )
            claimed = repository.claim_next(TARGET)
            self.assertIsNotNone(claimed)
            repository.finish_dispatch(
                BrokerOrder(
                    request=claimed.order.request,
                    status=BrokerOrderStatus.UNKNOWN,
                    updated_at=NOW,
                    broker_order_id="broker-1",
                    status_reason="test_interruption",
                )
            )
            registry = BrokerRegistry()
            registry.register(BrokerRegistration(
                TARGET,
                ShioajiBrokerAdapter(client, safety),
                BrokerCapabilities(),
                FakeMapper(),
            ))
            registry.freeze()
            manager = LiveOrderManager(repository, registry)
            results = await manager.reconcile_broker_orders(TARGET, "owner-1")
            self.assertEqual([item.status for item in results], [BrokerOrderStatus.ACCEPTED])
            self.assertFalse(any(call[0] == "place_order" for call in api.calls))
            self.assertEqual(repository.outbox_state("client-1"), "resolved")
            repository.close()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace

from tw_quant.broker import (
    BrokerAccountRef,
    BrokerCapabilities,
    BrokerRegistration,
    BrokerOrderRequest,
    BrokerRegistry,
    BrokerRuntimeState,
    BrokerSecretMaterial,
    CanonicalInstrument,
    ExecutionMode,
    READ_ONLY_ERROR,
    SHIOAJI_READ_ONLY_CAPABILITIES,
    ShioajiInstrumentMapper,
    ShioajiProductionCallbackBridge,
    ShioajiProductionError,
    ShioajiProductionExecutionClient,
    normalize_production_callback,
    ExecutionTarget,
    SQLiteExecutionTargetRepository,
    legacy_target_id,
    mask_account_id,
)
from tw_quant.execution_service import build_execution_service


class FakeAccount:
    def __init__(self, account_id: str, account_type: str = "F"):
        self.account_id = account_id
        self.account_type = account_type
        self.person_id = "PERSON-ID-SECRET"


class FakeApi:
    def __init__(self, accounts: list[FakeAccount]):
        self.accounts = accounts
        self.callback = None
        self.ca_result = True
        self.trades: list[object] = []
        self.positions: list[object] = []
        self.calls: list[str] = []
        self.active_calls = 0
        self.max_active_calls = 0
        self.slow_reads = False
        self.login_error: Exception | None = None
        self.callback_error: Exception | None = None
        self.Contracts = SimpleNamespace(Futures={"TMFU6": {"code": "TMFU6"}})
        self.placed_orders: list[object] = []
        self.cancelled_orders: list[object] = []

    def Order(self, **values: object) -> object:
        return SimpleNamespace(**values)

    def place_order(self, contract: object, order: object) -> object:
        self.calls.append("place_order")
        self.placed_orders.append(order)
        trade = {
            "contract": contract,
            "order": {"account_id": "account-1", "action": getattr(order, "action")},
            "status": {"id": "broker-canary-1", "status": "Submitted", "deals": []},
        }
        self.trades.append(trade)
        return trade

    def cancel_order(self, trade: object) -> object:
        self.calls.append("cancel_order")
        self.cancelled_orders.append(trade)
        trade["status"]["status"] = "Cancelled"
        return trade

    def _enter(self, name: str) -> None:
        self.calls.append(name)
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        if self.slow_reads:
            time.sleep(0.02)

    def _exit(self) -> None:
        self.active_calls -= 1

    def login(self, **kwargs: object) -> list[FakeAccount]:
        self.calls.append("login")
        if self.login_error is not None:
            raise self.login_error
        if kwargs.get("subscribe_trade") is not False:
            raise AssertionError("production login must explicitly defer subscription")
        return self.accounts

    def list_accounts(self) -> list[FakeAccount]:
        self.calls.append("list_accounts")
        return self.accounts

    def activate_ca(self, **kwargs: object) -> bool:
        self.calls.append("activate_ca")
        return self.ca_result

    def set_order_callback(self, callback: object) -> None:
        self.calls.append("set_order_callback")
        if self.callback_error is not None:
            raise self.callback_error
        self.callback = callback

    def subscribe_trade(self, account: object) -> None:
        self.calls.append("subscribe_trade")

    def update_status(self, account: object) -> None:
        self._enter("update_status")
        self._exit()

    def list_trades(self) -> list[object]:
        self._enter("list_trades")
        result = self.trades
        self._exit()
        return result

    def list_positions(self, account: object) -> list[object]:
        self._enter("list_positions")
        result = self.positions
        self._exit()
        return result

    def logout(self) -> None:
        self.calls.append("logout")


class FakeSdk:
    def __init__(self, api: FakeApi):
        self.api = api
        self.simulation_values: list[bool] = []
        self.constant = SimpleNamespace(
            Action=SimpleNamespace(Buy="Buy", Sell="Sell"),
            FuturesPriceType=SimpleNamespace(LMT="LMT", MKT="MKT"),
            OrderType=SimpleNamespace(IOC="IOC"),
            FuturesOCType=SimpleNamespace(Auto="Auto"),
        )

    def Shioaji(self, *, simulation: bool) -> FakeApi:
        self.simulation_values.append(simulation)
        return self.api


class FakeBroker:
    broker_name = "fake-b"

    async def account_state(self) -> dict[str, object]:
        return {"connected": True}

    async def positions(self) -> list[dict[str, object]]:
        return []

    async def submit_order(self, request: BrokerOrderRequest) -> object:
        raise AssertionError("fake broker must not receive a Shioaji operation")

    async def cancel_order(self, order: object) -> object:
        raise AssertionError("fake broker must not receive a Shioaji operation")

    async def refresh_order(self, order: object) -> object:
        raise AssertionError("fake broker must not receive a Shioaji operation")


class ProductionReadOnlyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.ca = Path(self.temp.name) / "ca.pfx"
        self.ca.write_bytes(b"fake-ca")
        self.ca.chmod(0o600)
        self.target = BrokerAccountRef("shioaji", "account-1")
        self.api = FakeApi([FakeAccount("account-1")])
        self.sdk = FakeSdk(self.api)
        self.mapper = ShioajiInstrumentMapper({
            CanonicalInstrument("TMF", "TMF-202609"): "TMFU6"
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def client(self, **changes: object) -> ShioajiProductionExecutionClient:
        values: dict[str, object] = {
            "account_ref": self.target,
            "allowed_accounts": frozenset({self.target}),
            "secret_material": BrokerSecretMaterial(
                values=(("api_key", "API_KEY_TEST_SECRET"),
                        ("secret_key", "SECRET_KEY_TEST_SECRET"),
                        ("ca_password", "CA_PASSWORD_TEST_SECRET")),
                files=(self.ca,),
            ),
            "instrument_mapper": self.mapper,
            "sdk": self.sdk,
        }
        values.update(changes)
        return ShioajiProductionExecutionClient(**values)  # type: ignore[arg-type]

    async def test_login_is_explicitly_production_and_selects_exact_account(self):
        client = self.client()
        await client.start()
        self.assertEqual(self.sdk.simulation_values, [False])
        self.assertIs(client.account, self.api.accounts[0])
        self.assertEqual(client.state, "read_only_ready")
        self.assertEqual(
            self.api.calls[:5],
            ["login", "list_accounts", "activate_ca", "set_order_callback", "subscribe_trade"],
        )

    async def test_account_missing_ambiguous_and_not_allowed_fail_closed(self):
        cases = (
            ([FakeAccount("other")], frozenset({self.target}), "configured_account_not_found"),
            (
                [FakeAccount("account-1"), FakeAccount("account-1")],
                frozenset({self.target}),
                "account_identity_ambiguous",
            ),
            ([FakeAccount("account-1")], frozenset(), "account_not_allowed"),
        )
        for accounts, allowed, code in cases:
            with self.subTest(code=code):
                api = FakeApi(accounts)
                client = self.client(sdk=FakeSdk(api), allowed_accounts=allowed)
                with self.assertRaises(ShioajiProductionError) as caught:
                    await client.start()
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(client.state, "locked")

    async def test_missing_and_failed_ca_lock(self):
        missing = self.client(secret_material=BrokerSecretMaterial(
            (("api_key", "a"), ("secret_key", "b"), ("ca_password", "c")),
            (Path(self.temp.name) / "missing",),
        ))
        with self.assertRaises(ShioajiProductionError) as caught:
            await missing.start()
        self.assertEqual(caught.exception.code, "ca_secret_missing")
        self.api.ca_result = False
        failed = self.client()
        with self.assertRaises(ShioajiProductionError) as caught:
            await failed.start()
        self.assertEqual(caught.exception.code, "ca_activation_failed")

    async def test_login_and_callback_registration_have_stable_sanitized_errors(self):
        cases = (
            ("login_error", "broker_login_failed"),
            ("callback_error", "callback_registration_failed"),
        )
        for attribute, expected in cases:
            with self.subTest(expected=expected):
                api = FakeApi([FakeAccount("account-1")])
                setattr(api, attribute, RuntimeError("API_KEY_TEST_SECRET"))
                client = self.client(sdk=FakeSdk(api))
                with self.assertRaises(ShioajiProductionError) as caught:
                    await client.start()
                self.assertEqual(str(caught.exception), expected)
                self.assertNotIn("API_KEY_TEST_SECRET", str(caught.exception))
                self.assertEqual(client.state, "locked")
                self.assertIn("logout", api.calls)

    async def test_health_is_cached_mask_safe_and_read_only(self):
        client = self.client()
        await client.start()
        health = client.health_state()
        self.assertTrue(health["connected"])
        self.assertTrue(health["ca_ready"])
        self.assertTrue(health["read_only"])
        self.assertTrue(health["callback_registered"])
        serialized = str(health)
        for secret in ("API_KEY_TEST_SECRET", "CA_PASSWORD_TEST_SECRET", "PERSON-ID-SECRET"):
            self.assertNotIn(secret, serialized)

    async def test_status_fill_and_position_normalization_is_deterministic(self):
        statuses = {
            "Submitted": "accepted",
            "PartFilled": "partially_filled",
            "Filled": "filled",
            "Cancelled": "cancelled",
            "Failed": "rejected",
            "Inactive": "expired",
        }
        for index, (raw, expected) in enumerate(statuses.items()):
            deals = [] if expected not in {"partially_filled", "filled"} else [
                {"trade_id": "deal-1", "quantity": 1, "price": 100.5,
                 "ts": "2026-09-13T01:00:00+00:00"}
            ]
            self.api.trades = [{
                "contract": {"code": "TMFU6"},
                "order": {"account_id": "account-1", "action": "Buy", "seqno": str(index)},
                "status": {"status": raw, "id": f"order-{index}", "deals": deals},
            }]
            client = self.client()
            await client.start()
            snapshot = await client.reconciliation_snapshot()
            self.assertEqual(snapshot.orders[0].status.value, expected)
            if deals:
                self.assertEqual(snapshot.fills[0].fill_id, f"order-{index}:deal-1")
                second = await client.reconciliation_snapshot()
                self.assertEqual(snapshot.fills[0].fill_id, second.fills[0].fill_id)
            await client.close()

        self.api.trades = []
        self.api.positions = [
            {"code": "TMFU6", "quantity": 3, "direction": "Buy"},
            {"code": "TMFU6", "quantity": 1, "direction": "Sell"},
        ]
        client = self.client()
        await client.start()
        snapshot = await client.reconciliation_snapshot()
        self.assertEqual(snapshot.positions[0].contract, "TMF-202609")
        self.assertEqual(snapshot.positions[0].quantity, 2)
        self.assertEqual(snapshot.account_ref, self.target)
        self.assertIsNotNone(snapshot.captured_at.utcoffset())

    async def test_unknown_status_contract_and_direction_fail_closed(self):
        client = self.client()
        await client.start()
        self.api.trades = [{
            "contract": {"code": "TMFU6"},
            "order": {"account_id": "account-1", "action": "Buy"},
            "status": {"status": "Mystery", "id": "x", "deals": []},
        }]
        with self.assertRaises(ShioajiProductionError) as caught:
            await client.reconciliation_snapshot()
        self.assertEqual(caught.exception.code, "broker_status_normalization_failed")

    async def test_callback_is_minimal_deterministic_and_bounded(self):
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        bridge = ShioajiProductionCallbackBridge(
            asyncio.get_running_loop(), self.target, queue
        )
        message = {
            "account_id": "account-1", "ordno": "order-1", "trade_id": "deal-1",
            "price": 100, "quantity": 1, "api_key": "API_KEY_TEST_SECRET",
            "ca_password": "CA_PASSWORD_TEST_SECRET",
        }
        first = normalize_production_callback("FDEAL", message, account_ref=self.target)
        second = normalize_production_callback("FDEAL", message, account_ref=self.target)
        self.assertEqual(first.event_id, second.event_id)
        self.assertNotIn("api_key", first.payload)
        bridge("FDEAL", message)
        bridge("FDEAL", message)
        await asyncio.sleep(0)
        metrics = bridge.metrics()
        self.assertEqual(metrics.received, 2)
        self.assertEqual(metrics.dropped, 1)
        self.assertEqual(metrics.queue_high_watermark, 1)

    async def test_callback_overflow_degrades_client_until_broker_read_recovers(self):
        client = self.client(callback_queue_size=1)
        await client.start()
        assert self.api.callback is not None
        message = {"account_id": "account-1", "ordno": "order-1"}
        self.api.callback("FORDER", message)
        self.api.callback("FORDER", message)
        await asyncio.sleep(0)
        self.assertEqual(client.state, "degraded")
        await client.reconciliation_snapshot()
        self.assertEqual(client.state, "read_only_ready")

    async def test_cross_account_callback_is_rejected(self):
        with self.assertRaises(ShioajiProductionError) as caught:
            normalize_production_callback(
                "FORDER", {"account_id": "other", "ordno": "1"},
                account_ref=self.target,
            )
        self.assertEqual(caught.exception.code, "callback_account_mismatch")

    async def test_concurrent_reads_are_serialized_without_blocking_loop(self):
        self.api.slow_reads = True
        client = self.client()
        await client.start()
        marker = False

        async def tick() -> None:
            nonlocal marker
            await asyncio.sleep(0.001)
            marker = True

        await asyncio.gather(client.reconciliation_snapshot(), client.positions(), tick())
        self.assertTrue(marker)
        self.assertEqual(self.api.max_active_calls, 1)

    async def test_all_write_methods_are_blocked_before_sdk(self):
        client = self.client()
        request = BrokerOrderRequest(
            "client", "owner", "strategy", 1, "TMF", "TMF-202609",
            "buy", 1, ExecutionMode.LIVE,
        )
        for call in (
            client.submit(request), client.cancel("order"), client.replace("order", request)
        ):
            with self.assertRaisesRegex(RuntimeError, READ_ONLY_ERROR):
                await call
        self.assertNotIn("place_order", self.api.calls)
        self.assertNotIn("cancel_order", self.api.calls)

    async def test_canary_mode_allows_only_one_verified_limit_ioc_and_cancel(self):
        client = self.client(effective_mode="canary")
        await client.start()
        request = BrokerOrderRequest(
            "canary:1", "owner-1", "manual-live-canary", 1,
            "TMF", "TMF-202609", "buy", 1, ExecutionMode.LIVE,
            order_type="limit", time_in_force="ioc", limit_price=100,
            source="manual_live_canary", arm_id="arm:1",
        )
        report = await client.submit(request)
        self.assertEqual(report.broker_order_id, "broker-canary-1")
        self.assertEqual(client.health_state()["external_order_calls"], 1)
        cancelled = await client.cancel("broker-canary-1")
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(client.health_state()["external_cancel_calls"], 1)
        with self.assertRaises(ShioajiProductionError):
            await client.submit(replace(request, quantity=2))
        self.assertEqual(client.health_state()["external_order_calls"], 1)

        guardian = replace(
            request,
            client_order_id="guardian:1",
            source="live_position_guardian",
            arm_id=None,
            side="sell",
            reduce_only=True,
            purpose="liquidation",
            order_type="market",
            limit_price=None,
        )
        guardian_report = await client.submit(guardian)
        self.assertEqual(guardian_report.broker_order_id, "broker-canary-1")
        self.assertEqual(self.api.placed_orders[-1].price_type, "MKT")
        with self.assertRaisesRegex(ShioajiProductionError, "requires_liquidation"):
            await client.submit(replace(guardian, purpose="exit"))

    async def test_execution_composition_registers_read_only_and_ordering_stays_locked(self):
        database = Path(self.temp.name) / "live.sqlite3"
        target_id = legacy_target_id("owner-1", self.target)
        repository = SQLiteExecutionTargetRepository(database)
        repository.create(ExecutionTarget(
            target_id, "owner-1", self.target.broker_name, self.target.account_id,
            mask_account_id(self.target.account_id), f"file:broker-secrets/{target_id}",
        ))
        repository.close()
        secret_dir = Path(self.temp.name) / "brokers" / target_id
        secret_dir.mkdir(parents=True, mode=0o700)
        credentials = secret_dir / "credentials.env"
        credentials.write_text("SJ_API_KEY=x\nSJ_SECRET_KEY=y\nSJ_CA_PASSWORD=z\n")
        credentials.chmod(0o600)
        target_ca = secret_dir / "shioaji-ca.pfx"
        target_ca.write_bytes(self.ca.read_bytes())
        target_ca.chmod(0o600)
        env = {
            "BROKER_PROVIDER": "shioaji",
            "LIVE_TRADING_ENABLED": "false",
            "LIVE_BROKER_READ_ONLY_ENABLED": "true",
            "LIVE_BROKER_READ_ONLY_CONFIRMATION": "I_UNDERSTAND_PRODUCTION_READ_ONLY",
            "LIVE_BROKER_INSTRUMENT_MAP_JSON": (
                '[{"symbol":"TMF","contract":"TMF-202609",'
                '"broker_contract":"TMFU6"}]'
            ),
            "LIVE_BROKER_ACCOUNT_ID": "account-1",
            "LIVE_ALLOWED_ACCOUNT_IDS": "account-1",
            "LIVE_EXECUTION_DB_PATH": str(database),
            "LIVE_EXECUTION_OWNER_USER_ID": "owner-1",
            "LIVE_EXECUTION_TARGET_ID": target_id,
            "LIVE_BROKER_SECRET_ROOT": str(Path(self.temp.name) / "brokers"),
            "LIVE_EXECUTION_HEALTH_PATH": str(Path(self.temp.name) / "health.json"),
            "SJ_API_KEY": "API_KEY_TEST_SECRET",
            "SJ_SECRET_KEY": "SECRET_KEY_TEST_SECRET",
            "SJ_CA_CERT_PATH": str(self.ca),
            "SJ_CA_PASSWORD": "CA_PASSWORD_TEST_SECRET",
        }
        client = self.client()
        runtime = build_execution_service(
            env=env, production_client_factory=lambda **_: client
        )
        try:
            await runtime.start()
            registration = runtime.broker_registry.registration(  # type: ignore[union-attr]
                self.target
            )
            health = runtime.public_health()
            self.assertEqual(registration.state.value, "ready")
            self.assertFalse(registration.capabilities.supports_market_orders)
            self.assertEqual(health["execution_state"], "ready_read_only")
            self.assertTrue(health["read_only"])
            self.assertTrue(health["locked"])
            self.assertFalse(health["ordering_enabled"])
            self.assertEqual(health["recovery_status"], "ready")
            self.assertEqual(runtime.state_document()["external_order_calls"], 0)
        finally:
            await runtime.close()

    async def test_read_only_configuration_fails_before_sdk_when_incomplete(self):
        base = {
            "BROKER_PROVIDER": "shioaji",
            "LIVE_TRADING_ENABLED": "false",
            "LIVE_BROKER_READ_ONLY_ENABLED": "true",
            "LIVE_BROKER_READ_ONLY_CONFIRMATION": "I_UNDERSTAND_PRODUCTION_READ_ONLY",
            "LIVE_BROKER_ACCOUNT_ID": "account-1",
            "LIVE_ALLOWED_ACCOUNT_IDS": "account-1",
            "LIVE_EXECUTION_DB_PATH": str(Path(self.temp.name) / "invalid.sqlite3"),
            "LIVE_EXECUTION_HEALTH_PATH": str(Path(self.temp.name) / "invalid-health.json"),
            "SJ_API_KEY": "API_KEY_TEST_SECRET",
            "SJ_SECRET_KEY": "SECRET_KEY_TEST_SECRET",
            "SJ_CA_CERT_PATH": str(self.ca),
            "SJ_CA_PASSWORD": "CA_PASSWORD_TEST_SECRET",
        }
        for expected, change in (
            ("missing_broker_instrument_map", {}),
            ("invalid_read_only_confirmation", {
                "LIVE_BROKER_INSTRUMENT_MAP_JSON": "[]",
                "LIVE_BROKER_READ_ONLY_CONFIRMATION": "wrong",
            }),
        ):
            with self.subTest(expected=expected):
                env = {**base, **change}
                runtime = build_execution_service(env=env)
                try:
                    self.assertIn(expected, runtime.issues)
                    self.assertIsNone(runtime.read_only_client)
                    self.assertTrue(runtime.locked)
                finally:
                    await runtime.close()

    def test_mapper_and_runtime_capabilities_fail_closed(self):
        instrument = CanonicalInstrument("TMF", "TMF-202609")
        self.assertEqual(self.mapper.to_broker_contract(instrument), "TMFU6")
        self.assertEqual(self.mapper.to_canonical_instrument("TMFU6"), instrument)
        with self.assertRaises(LookupError):
            self.mapper.to_canonical_instrument("UNKNOWN")
        self.assertFalse(SHIOAJI_READ_ONLY_CAPABILITIES.supports_market_orders)
        self.assertFalse(SHIOAJI_READ_ONLY_CAPABILITIES.supports_cancel)
        self.assertTrue(SHIOAJI_READ_ONLY_CAPABILITIES.supports_order_callback)

        parsed = ShioajiInstrumentMapper.from_json(
            '[{"symbol":"TMF","contract":"TMF-202609",'
            '"broker_contract":"TMFU6"}]'
        )
        self.assertEqual(parsed.to_broker_contract(instrument), "TMFU6")
        for invalid in ("", "{}", "[]", '[{"symbol":"TMF"}]'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ShioajiInstrumentMapper.from_json(invalid)

    async def test_shioaji_failure_does_not_change_second_broker_registration(self):
        registry = BrokerRegistry()
        fake_target = BrokerAccountRef("fake-b", "account-1")
        registry.register(BrokerRegistration(
            fake_target,
            FakeBroker(),
            BrokerCapabilities(),
            self.mapper,
            BrokerRuntimeState.READY,
        ))
        registry.freeze()
        self.api.ca_result = False
        with self.assertRaises(ShioajiProductionError):
            await self.client().start()
        self.assertIs(registry.resolve(fake_target), registry.registration(fake_target).port)
        self.assertEqual(registry.registration(fake_target).state, BrokerRuntimeState.READY)
        with self.assertRaises(KeyError):
            registry.registration(self.target)


if __name__ == "__main__":
    unittest.main()

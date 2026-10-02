from __future__ import annotations

from public_fixtures import InertProvider, PublicTestCase, PublicAsyncTestCase, synthetic_csv, services

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from tw_quant.auth import (
    AccessIdentity,
    AccessTokenError,
    Role,
    SQLiteAuthRepository,
    TradingMode,
)
from tw_quant.live.api import create_app
from tw_quant.live.settings import LiveSettings
from tw_quant.live.storage import SQLiteBarRepository
from tw_quant.market import KBar, TAIPEI


ROOT = Path(__file__).resolve().parents[1]


class PaperMarketFeed:
    provider_name = "paper-test"
    contract = "TMFTEST"

    async def start(self, _on_tick, on_status):
        on_status("connected")

    async def stop(self):
        return None

    async def heartbeat(self):
        return True


class PaperAccessValidator:
    def authenticate(self, token):
        identities = {
            "admin-token": AccessIdentity("cf-admin", "admin@example.com"),
            "trader-token": AccessIdentity("cf-trader", "trader@example.com"),
            "other-token": AccessIdentity("cf-other", "other@example.com"),
            "reader-token": AccessIdentity("cf-reader", "reader@example.com"),
        }
        try:
            return identities[token]
        except KeyError as exc:
            raise AccessTokenError("invalid assertion") from exc


class PaperTradingApiTests(PublicTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "paper.sqlite3"
        auth = SQLiteAuthRepository(self.db_path)
        auth.create_user(
            "admin@example.com", role=Role.ADMIN,
            trading_mode=TradingMode.PAPER,
        )
        auth.create_user(
            "trader@example.com", role=Role.TRADER,
            trading_mode=TradingMode.PAPER,
        )
        auth.create_user(
            "other@example.com", role=Role.TRADER,
            trading_mode=TradingMode.PAPER,
        )
        auth.create_user("reader@example.com", role=Role.RESEARCHER)
        self.repository = SQLiteBarRepository(self.db_path)
        at = datetime.now(TAIPEI) - timedelta(minutes=1)
        self.repository.save(KBar(
            symbol="TMF", contract="TMFTEST", time=at.replace(second=0, microsecond=0),
            open=20_000, high=20_010, low=19_990, close=20_000, volume=100,
            status="forming", session="day", trading_date=at.date(),
            first_tick_time=at, last_tick_time=at, exchange_time=at,
            received_time=at, latency_ms=0,
        ))
        settings = LiveSettings(
            mode="mock", db_path=str(self.db_path),
            replay_csv=str(synthetic_csv()), replay_speed=1000,
            heartbeat_seconds=0.05, access_mode="cloudflare",
            cloudflare_access_team_domain="team.cloudflareaccess.com",
            cloudflare_access_audience="audience", authorization_mode="enforced",
            bootstrap_admin_emails=("admin@example.com",),
        )
        self.client = TestClient(create_app(
            settings,
            feed=PaperMarketFeed(),
            repository=self.repository,
            access_validator=PaperAccessValidator(),
            auth_repository=auth,
         strategy_provider=InertProvider()))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    @staticmethod
    def headers(subject: str, email: str, key: str | None = None):
        result = {
            "Cf-Access-Jwt-Assertion": {
                "cf-admin": "admin-token", "cf-trader": "trader-token",
                "cf-other": "other-token", "cf-reader": "reader-token",
            }[subject],
        }
        if key:
            result["Idempotency-Key"] = key
        return result

    def test_market_order_is_risk_checked_filled_and_idempotent(self):
        headers = self.headers("cf-trader", "trader@example.com", "mobile-tap-1")
        payload = {
            "side": "buy", "quantity": 1, "stop_loss_price": 19_950,
        }
        first = self.client.post("/api/paper/orders", headers=headers, json=payload)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertTrue(first.json()["created"])
        order = first.json()["order"]
        self.assertEqual(order["status"], "filled")
        self.assertEqual(order["lifecycle_status"], "filled")
        self.assertEqual(order["client_order_id"], "mobile-tap-1")
        self.assertEqual(order["reference_price"], 20_000)
        self.assertEqual(order["order_source"], "manual")
        self.assertIsNone(order["runtime_id"])

        repeated = self.client.post("/api/paper/orders", headers=headers, json=payload)
        self.assertEqual(repeated.status_code, 201, repeated.text)
        self.assertFalse(repeated.json()["created"])
        self.assertEqual(repeated.json()["order"]["order_id"], order["order_id"])

        account = self.client.get(
            "/api/paper/account",
            headers=self.headers("cf-trader", "trader@example.com"),
        ).json()
        self.assertEqual(account["account"]["open_contracts"], 1)
        self.assertEqual(
            account["account"]["risk_limits"]["max_position_contracts"],
            2,
        )
        self.assertIn("max_risk_per_trade", account["account"]["risk_limits"])
        self.assertEqual(account["positions"][0]["quantity"], 1)
        self.assertEqual(account["positions"][0]["order_source"], "manual")
        fills = self.client.get(
            "/api/paper/fills",
            headers=self.headers("cf-trader", "trader@example.com"),
        ).json()["fills"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["order_source"], "manual")

    def test_rapid_repeated_mobile_taps_create_one_order_and_fill(self):
        headers = self.headers(
            "cf-trader", "trader@example.com", "rapid-mobile-tap"
        )
        payload = {"side": "buy", "quantity": 1, "stop_loss_price": 19_950}
        responses = [
            self.client.post("/api/paper/orders", headers=headers, json=payload)
            for _ in range(25)
        ]
        self.assertTrue(all(response.status_code == 201 for response in responses))
        self.assertEqual(sum(response.json()["created"] for response in responses), 1)
        order_ids = {response.json()["order"]["order_id"] for response in responses}
        self.assertEqual(len(order_ids), 1)
        orders = self.client.get(
            "/api/paper/orders",
            headers=self.headers("cf-trader", "trader@example.com"),
        ).json()["orders"]
        fills = self.client.get(
            "/api/paper/fills",
            headers=self.headers("cf-trader", "trader@example.com"),
        ).json()["fills"]
        self.assertEqual(len(orders), 1)
        self.assertEqual(len(fills), 1)

    def test_idempotency_key_reuse_with_different_payload_is_conflict(self):
        headers = self.headers(
            "cf-trader", "trader@example.com", "conflicting-mobile-tap"
        )
        first = self.client.post(
            "/api/paper/orders",
            headers=headers,
            json={"side": "buy", "quantity": 1, "stop_loss_price": 19_950},
        )
        conflict = self.client.post(
            "/api/paper/orders",
            headers=headers,
            json={"side": "sell", "quantity": 1, "stop_loss_price": 20_050},
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(conflict.status_code, 409)
        self.assertIn("different paper order", conflict.json()["detail"])
        self.assertEqual(len(self.client.get(
            "/api/paper/orders",
            headers=self.headers("cf-trader", "trader@example.com"),
        ).json()["orders"]), 1)

    def test_owner_data_is_isolated_and_researcher_is_denied(self):
        created = self.client.post(
            "/api/paper/orders",
            headers=self.headers("cf-trader", "trader@example.com", "private-order"),
            json={"side": "sell", "stop_loss_price": 20_050},
        )
        self.assertEqual(created.status_code, 201)
        other = self.client.get(
            "/api/paper/orders",
            headers=self.headers("cf-other", "other@example.com"),
        )
        self.assertEqual(other.status_code, 200)
        self.assertEqual(other.json()["orders"], [])
        denied = self.client.get(
            "/api/paper/account",
            headers=self.headers("cf-reader", "reader@example.com"),
        )
        self.assertEqual(denied.status_code, 403)

    def test_admin_with_explicit_paper_mode_can_submit_but_remains_isolated(self):
        response = self.client.post(
            "/api/paper/orders",
            headers=self.headers("cf-admin", "admin@example.com", "admin-paper"),
            json={"side": "buy", "stop_loss_price": 19_950},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["order"]["status"], "filled")
        trader_orders = self.client.get(
            "/api/paper/orders",
            headers=self.headers("cf-trader", "trader@example.com"),
        )
        self.assertEqual(trader_orders.status_code, 200)
        self.assertEqual(trader_orders.json()["orders"], [])

    def test_two_traders_can_interleave_orders_without_cross_account_leakage(self):
        cases = (
            ("cf-trader", "trader@example.com", "trader-interleaved", "buy", 19_950),
            ("cf-other", "other@example.com", "other-interleaved", "sell", 20_050),
        )
        created_order_ids = {}
        for subject, email, key, side, stop in cases:
            response = self.client.post(
                "/api/paper/orders",
                headers=self.headers(subject, email, key),
                json={"side": side, "stop_loss_price": stop},
            )
            self.assertEqual(response.status_code, 201, response.text)
            created_order_ids[email] = response.json()["order"]["order_id"]
        for subject, email, _key, _side, _stop in cases:
            orders = self.client.get(
                "/api/paper/orders", headers=self.headers(subject, email)
            ).json()["orders"]
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0]["order_id"], created_order_ids[email])

    def test_kill_switch_blocks_new_exposure(self):
        identity = self.headers("cf-trader", "trader@example.com")
        activated = self.client.post(
            "/api/paper/kill-switch", headers=identity,
            json={"reason": "operator_test"},
        )
        self.assertEqual(activated.status_code, 200)
        self.assertTrue(activated.json()["kill_switch_active"])
        blocked = self.client.post(
            "/api/paper/orders",
            headers={**identity, "Idempotency-Key": "blocked-order"},
            json={"side": "buy", "stop_loss_price": 19_950},
        )
        self.assertEqual(blocked.status_code, 201)
        self.assertEqual(blocked.json()["order"]["status"], "rejected")
        self.assertEqual(
            blocked.json()["order"]["status_reason"], "kill_switch_active"
        )

        reset = self.client.post(
            "/api/paper/kill-switch/reset", headers=identity,
            json={"reason": "operator_resume"},
        )
        self.assertEqual(reset.status_code, 200)
        self.assertFalse(reset.json()["kill_switch_active"])

    def test_control_reason_and_idempotency_key_lengths_are_limited(self):
        identity = self.headers("cf-trader", "trader@example.com")
        reason = self.client.post(
            "/api/paper/kill-switch",
            headers=identity,
            json={"reason": "x" * 501},
        )
        self.assertEqual(reason.status_code, 422)

        order = self.client.post(
            "/api/paper/orders",
            headers={**identity, "Idempotency-Key": "x" * 129},
            json={"side": "buy", "stop_loss_price": 19_950},
        )
        self.assertEqual(order.status_code, 422)

    def test_kill_switch_blocks_entry_but_allows_reduce_only_close(self):
        identity = self.headers("cf-trader", "trader@example.com")
        entry = self.client.post(
            "/api/paper/orders",
            headers={**identity, "Idempotency-Key": "entry-before-halt"},
            json={"side": "buy", "stop_loss_price": 19_950},
        )
        self.assertEqual(entry.status_code, 201, entry.text)
        self.client.post(
            "/api/paper/kill-switch", headers=identity,
            json={"reason": "acceptance_halt"},
        )
        blocked = self.client.post(
            "/api/paper/orders",
            headers={**identity, "Idempotency-Key": "entry-after-halt"},
            json={"side": "buy", "stop_loss_price": 19_950},
        )
        self.assertEqual(blocked.json()["order"]["status"], "rejected")

        close = self.client.post(
            "/api/paper/orders",
            headers={**identity, "Idempotency-Key": "close-during-halt"},
            json={"side": "sell", "reduce_only": True},
        )
        self.assertEqual(close.status_code, 201, close.text)
        self.assertEqual(close.json()["order"]["status"], "filled")
        account = self.client.get(
            "/api/paper/account", headers=identity
        ).json()
        self.assertEqual(account["positions"], [])

    def test_stale_market_price_cannot_be_used_for_a_paper_fill(self):
        old = datetime.now(TAIPEI) - timedelta(minutes=5)
        self.repository.save(KBar(
            symbol="TMF", contract="TMFSTALE", time=datetime.now(TAIPEI),
            open=19_000, high=19_010, low=18_990, close=19_000, volume=1,
            status="forming", session="day", trading_date=old.date(),
            first_tick_time=old, last_tick_time=old, exchange_time=old,
            received_time=old, latency_ms=0,
        ))
        response = self.client.post(
            "/api/paper/orders",
            headers=self.headers("cf-trader", "trader@example.com", "stale-order"),
            json={"side": "buy", "stop_loss_price": 18_950},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "market price is stale")

    def test_provider_disconnect_blocks_new_position_without_delayed_resend(self):
        service = self.client.app.state.market_service
        service.set_connection_status("disconnected")
        headers = self.headers(
            "cf-trader", "trader@example.com", "disconnect-order"
        )
        blocked = self.client.post(
            "/api/paper/orders", headers=headers,
            json={"side": "buy", "stop_loss_price": 19_950},
        )
        self.assertEqual(blocked.status_code, 503)
        self.assertIn("new positions are blocked", blocked.json()["detail"])

        positions = self.client.get(
            "/api/paper/account",
            headers=self.headers("cf-trader", "trader@example.com"),
        )
        self.assertEqual(positions.status_code, 200)
        self.assertEqual(positions.json()["positions"], [])

        service.set_connection_status("connected")
        orders = self.client.get(
            "/api/paper/orders",
            headers=self.headers("cf-trader", "trader@example.com"),
        )
        self.assertEqual(orders.json()["orders"], [])
        health = self.client.get(
            "/api/admin/health",
            headers=self.headers("cf-admin", "admin@example.com"),
        ).json()
        self.assertEqual(
            health["paper_trading"]["market_blocked_requests"], 1
        )


if __name__ == "__main__":
    unittest.main()

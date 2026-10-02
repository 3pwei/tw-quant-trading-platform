
from public_fixtures import InertProvider, PublicTestCase, PublicAsyncTestCase, synthetic_csv, services
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tw_quant.auth import (
    AccessIdentity,
    AccessTokenError,
    Role,
    SQLiteAuthRepository,
)
from tw_quant.live.api import create_app
from tw_quant.live.feed import ReplayFeed
from tw_quant.live.settings import LiveSettings
from tw_quant.live.storage import SQLiteBarRepository

ROOT = Path(__file__).resolve().parents[1]


class FakeAccessValidator:
    def authenticate(self, token):
        identities = {
            "admin-token": AccessIdentity("cf-admin", "admin@example.com"),
            "reader-token": AccessIdentity("cf-reader", "reader@example.com"),
            "guest-token": AccessIdentity("cf-guest", "guest@example.com"),
            "second-guest-token": AccessIdentity(
                "cf-second-guest", "second@example.com"
            ),
        }
        if token in identities:
            return identities[token]
        if isinstance(token, str) and token.startswith("test:"):
            subject, email = token.removeprefix("test:").split("|", 1)
            return AccessIdentity(subject, email)
        raise AccessTokenError("invalid assertion")


class AuthorizationApiTests(PublicTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "authorization.sqlite3"
        identities = SQLiteAuthRepository(self.db_path)
        identities.create_user("admin@example.com", role=Role.ADMIN)
        identities.create_user("reader@example.com", role=Role.RESEARCHER)
        settings = LiveSettings(
            mode="mock",
            db_path=str(self.db_path),
            replay_csv=str(synthetic_csv()),
            replay_speed=1000,
            heartbeat_seconds=0.05,
            access_mode="cloudflare",
            cloudflare_access_team_domain="team.cloudflareaccess.com",
            cloudflare_access_audience="audience",
            authorization_mode="enforced",
            bootstrap_admin_emails=("admin@example.com",),
        )
        self.app = create_app(
            settings,
            feed=ReplayFeed(settings.replay_csv, speed=1000, loop=False),
            repository=SQLiteBarRepository(self.db_path),
            access_validator=FakeAccessValidator(),
            auth_repository=identities,
         strategy_provider=InertProvider())
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    @staticmethod
    def headers(subject: str, email: str):
        return {"Cf-Access-Jwt-Assertion": f"test:{subject}|{email}"}

    def test_forwarded_identity_headers_cannot_replace_verified_assertion(self):
        forged_admin = {
            "X-Authenticated-Subject": "cf-admin",
            "X-Authenticated-Email": "admin@example.com",
            "X-Authenticated-User-ID": "forged-admin",
            "X-Authenticated-Role": "admin",
            "CF-Access-Authenticated-User-Email": "admin@example.com",
        }
        for path in (
            "/api/me", "/api/backtest-runs", "/api/strategies",
            "/api/paper/orders", "/api/live/orders", "/api/admin/users",
        ):
            self.assertEqual(self.client.get(path, headers=forged_admin).status_code, 401)
            self.assertEqual(self.client.get(
                path, headers={**forged_admin, "Cf-Access-Jwt-Assertion": "invalid"},
            ).status_code, 401)

        reader = {**forged_admin, "Cf-Access-Jwt-Assertion": "reader-token"}
        me = self.client.get("/api/me", headers=reader)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["email"], "reader@example.com")
        self.assertEqual(me.json()["role"], "researcher")
        self.assertEqual(self.client.get("/api/admin/users", headers=reader).status_code, 403)
        self.assertEqual(self.client.get("/api/paper/orders", headers=reader).status_code, 403)
        self.assertEqual(self.client.get("/api/backtest-runs", headers=reader).status_code, 200)

        admin = {**forged_admin, "Cf-Access-Jwt-Assertion": "admin-token"}
        self.assertEqual(self.client.get("/api/admin/users", headers=admin).status_code, 200)

    def test_unregistered_identity_is_rejected_for_pages_and_api(self):
        denied_page = self.client.get(
            "/internal/auth/cloudflare",
            headers={
                "Cf-Access-Jwt-Assertion": "guest-token",
                "X-Original-Uri": "/backtest/",
            },
        )
        self.assertEqual(denied_page.status_code, 403)
        self.assertIn("text/html", denied_page.headers["content-type"])
        self.assertIn("帳號尚未開通", denied_page.text)
        self.assertIn("申請開通", denied_page.text)
        self.assertIn("/api/access-requests", denied_page.text)
        self.assertIn("/cdn-cgi/access/logout", denied_page.text)
        self.assertEqual(denied_page.headers["cache-control"], "no-store")

        denied_api = self.client.get(
            "/api/me",
            headers=self.headers("cf-guest", "guest@example.com"),
        )
        self.assertEqual(denied_api.status_code, 403)
        self.assertEqual(
            denied_api.json()["detail"], "platform account is not registered"
        )

    def test_oversized_request_is_rejected_before_auth_and_json_parsing(self):
        response = self.client.post(
            "/api/access-requests",
            content=b"x" * (256 * 1024 + 1),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["detail"], "request body too large")
        self.assertEqual(response.json()["max_bytes"], 256 * 1024)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_chunked_oversized_request_is_also_rejected(self):
        def chunks():
            yield b"x" * (128 * 1024)
            yield b"x" * (128 * 1024 + 1)

        response = self.client.post(
            "/api/access-requests",
            content=chunks(),
            headers={
                **self.headers("cf-guest", "guest@example.com"),
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["detail"], "request body too large")

    def test_verified_access_request_email_length_is_limited(self):
        response = self.client.post(
            "/api/access-requests",
            headers=self.headers(
                "cf-long-email", f"{'a' * 250}@example.com"
            ),
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"], "a verified email is required"
        )

    def test_verified_guest_can_request_and_admin_can_approve(self):
        guest = self.headers("cf-guest", "guest@example.com")
        forward_auth = self.client.get(
            "/internal/auth/cloudflare",
            headers={
                "Cf-Access-Jwt-Assertion": "guest-token",
                "X-Original-Uri": "/api/access-requests",
            },
        )
        self.assertEqual(forward_auth.status_code, 204)
        self.assertEqual(
            forward_auth.headers["x-authenticated-email"],
            "guest@example.com",
        )

        submitted = self.client.post("/api/access-requests", headers=guest)
        self.assertEqual(submitted.status_code, 201)
        request_id = submitted.json()["request"]["request_id"]
        self.assertEqual(submitted.json()["request"]["status"], "pending")

        repeated = self.client.post("/api/access-requests", headers=guest)
        self.assertEqual(repeated.status_code, 201)
        self.assertEqual(
            repeated.json()["request"]["request_id"], request_id
        )

        reader = self.headers("cf-reader", "reader@example.com")
        denied = self.client.get(
            "/api/admin/access-requests", headers=reader
        )
        self.assertEqual(denied.status_code, 403)

        admin = self.headers("cf-admin", "admin@example.com")
        pending = self.client.get(
            "/api/admin/access-requests", headers=admin
        )
        self.assertEqual(pending.status_code, 200)
        self.assertEqual(len(pending.json()["requests"]), 1)
        self.assertEqual(
            pending.json()["requests"][0]["email"], "guest@example.com"
        )

        approved = self.client.post(
            f"/api/admin/access-requests/{request_id}/approve",
            headers=admin,
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["user"]["role"], "researcher")
        self.assertEqual(
            approved.json()["user"]["trading_mode"], "disabled"
        )

        admitted = self.client.get("/api/me", headers=guest)
        self.assertEqual(admitted.status_code, 200)
        self.assertEqual(admitted.json()["email"], "guest@example.com")
        self.assertTrue(admitted.json()["registered"])

        remaining = self.client.get(
            "/api/admin/access-requests", headers=admin
        )
        self.assertEqual(remaining.json()["requests"], [])

    def test_access_requests_are_rate_limited_per_verified_identity(self):
        first_guest = self.headers("cf-rate-1", "rate-1@example.com")
        for remaining in range(4, -1, -1):
            response = self.client.post(
                "/api/access-requests", headers=first_guest
            )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(
                response.headers["x-ratelimit-remaining"], str(remaining)
            )

        rejected = self.client.post(
            "/api/access-requests", headers=first_guest
        )
        self.assertEqual(rejected.status_code, 429)
        self.assertEqual(rejected.json()["scope"], "access_requests")
        self.assertEqual(rejected.headers["x-ratelimit-remaining"], "0")
        self.assertGreaterEqual(int(rejected.headers["retry-after"]), 1)
        self.assertEqual(rejected.headers["cache-control"], "no-store")

        other_guest = self.client.post(
            "/api/access-requests",
            headers=self.headers("cf-rate-2", "rate-2@example.com"),
        )
        self.assertEqual(other_guest.status_code, 201)
        self.assertEqual(other_guest.headers["x-ratelimit-remaining"], "4")

    def test_admin_can_reject_an_access_request(self):
        guest = self.headers("cf-second-guest", "second@example.com")
        submitted = self.client.post("/api/access-requests", headers=guest)
        request_id = submitted.json()["request"]["request_id"]
        admin = self.headers("cf-admin", "admin@example.com")

        rejected = self.client.post(
            f"/api/admin/access-requests/{request_id}/reject",
            headers=admin,
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json()["request"]["status"], "rejected")
        self.assertEqual(
            self.client.get(
                "/api/admin/access-requests", headers=admin
            ).json()["requests"],
            [],
        )
        denied = self.client.get("/api/me", headers=guest)
        self.assertEqual(denied.status_code, 403)

    def test_role_blocks_admin_api_and_protected_static_route(self):
        reader = self.headers("cf-reader", "reader@example.com")
        denied = self.client.get("/api/admin/users", headers=reader)
        self.assertEqual(denied.status_code, 403)

        protected_page = self.client.get(
            "/internal/auth/cloudflare",
            headers={
                "Cf-Access-Jwt-Assertion": "reader-token",
                "X-Original-Uri": "/settings/",
            },
        )
        self.assertEqual(protected_page.status_code, 403)

        ordinary_page = self.client.get(
            "/internal/auth/cloudflare",
            headers={
                "Cf-Access-Jwt-Assertion": "reader-token",
                "X-Original-Uri": "/backtest/",
            },
        )
        self.assertEqual(ordinary_page.status_code, 204)

        trade_page = self.client.get(
            "/internal/auth/cloudflare",
            headers={
                "Cf-Access-Jwt-Assertion": "reader-token",
                "X-Original-Uri": "/trade/",
            },
        )
        self.assertEqual(trade_page.status_code, 204)

        paper_page = self.client.get(
            "/internal/auth/cloudflare",
            headers={
                "Cf-Access-Jwt-Assertion": "reader-token",
                "X-Original-Uri": "/paper/",
            },
        )
        self.assertEqual(paper_page.status_code, 403)

    def test_admin_can_create_and_change_user_with_audit(self):
        admin = self.headers("cf-admin", "admin@example.com")
        created = self.client.post(
            "/api/admin/users",
            headers=admin,
            json={
                "email": "trader@example.com",
                "role": "trader",
                "status": "active",
                "trading_mode": "paper",
            },
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["trading_mode"], "paper")

        listed = self.client.get("/api/admin/users", headers=admin)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["users"]), 3)

        audit = self.client.get("/api/admin/audit", headers=admin)
        self.assertEqual(audit.status_code, 200)
        self.assertIn(
            "user.created", [event["action"] for event in audit.json()["events"]]
        )

    def test_admin_user_email_length_is_limited(self):
        response = self.client.post(
            "/api/admin/users",
            headers=self.headers("cf-admin", "admin@example.com"),
            json={"email": f"{'a' * 250}@example.com"},
        )

        self.assertEqual(response.status_code, 422)

    def test_health_hides_provider_details_from_regular_users(self):
        reader = self.headers("cf-reader", "reader@example.com")
        public_health = self.client.get("/api/health", headers=reader)
        self.assertEqual(public_health.status_code, 200)
        self.assertNotIn("market_data_provider", public_health.json())
        self.assertNotIn("queue_size", public_health.json())

        admin_health = self.client.get(
            "/api/admin/health",
            headers=self.headers("cf-admin", "admin@example.com"),
        )
        self.assertEqual(admin_health.status_code, 200)
        self.assertIn("market_data_provider", admin_health.json())
        self.assertIn("queue_size", admin_health.json())
        self.assertIn("queue_high_watermark", admin_health.json())
        self.assertIn("average_tick_processing_ms", admin_health.json())
        self.assertIn("system_status", admin_health.json())
        self.assertIn("host", admin_health.json())
        self.assertIn("rate_limiting", admin_health.json())
        self.assertEqual(
            admin_health.json()["request_limits"]["max_body_bytes"],
            256 * 1024,
        )
        self.assertEqual(
            admin_health.json()["rate_limiting"]["algorithm"],
            "sliding_window",
        )
        self.assertIn(
            "backtests", admin_health.json()["rate_limiting"]["scopes"]
        )
        self.assertIn("websocket_connections", admin_health.json())
        self.assertIn("average_database_write_ms", admin_health.json())
        self.assertEqual(
            admin_health.json()["paper_trading"]["status"], "healthy"
        )
        self.assertIn(
            "average_submission_ms", admin_health.json()["paper_trading"]
        )
        self.assertIn(
            "active_kill_switches", admin_health.json()["paper_trading"]
        )
        automation = admin_health.json()["automated_trading"]
        for key in (
            "active_runtimes", "armed_runtimes", "decisions",
            "executed_decisions", "skipped_decisions",
            "duplicate_decisions_blocked", "gap_risk_rejected",
            "recovery_locked_runtimes", "last_runtime_decision_time",
            "last_auto_order_time",
        ):
            self.assertIn(key, automation)
        self.assertEqual(
            admin_health.json()["live_execution"]["state"], "disabled"
        )
        self.assertEqual(
            admin_health.json()["live_execution"]["recovery_status"],
            "locked",
        )
        self.assertEqual(
            admin_health.json()["live_execution"]["dispatches"], 0
        )

    def test_websocket_requires_market_permission_and_identity(self):
        with self.client.websocket_connect(
            "/ws/market/TMF",
            headers=self.headers("cf-reader", "reader@example.com"),
        ) as socket:
            status = socket.receive_json()
            self.assertEqual(status["type"], "status")
            self.assertNotIn("market_data_provider", status)

        with self.assertRaises(WebSocketDisconnect) as caught, \
                self.client.websocket_connect("/ws/market/TMF"):
            pass
        self.assertEqual(caught.exception.code, 1008)

    def test_unknown_api_route_fails_closed_before_routing(self):
        response = self.client.get(
            "/api/not-yet-classified",
            headers=self.headers("cf-reader", "reader@example.com"),
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()

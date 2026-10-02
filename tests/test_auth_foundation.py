
from public_fixtures import InertProvider, PublicTestCase, PublicAsyncTestCase, synthetic_csv, services
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from tw_quant.auth import (
    AccessIdentity,
    AccessRequestStatus,
    AccessTokenError,
    AccountStatus,
    AuthorizationError,
    AuthService,
    Role,
    SQLiteAuthRepository,
    TradingMode,
)
from tw_quant.live.api import create_app
from tw_quant.live.feed import ReplayFeed
from tw_quant.live.settings import LiveSettings
from tw_quant.live.storage import SQLiteBarRepository

ROOT = Path(__file__).resolve().parents[1]


class AuthRepositoryTests(PublicTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = SQLiteAuthRepository(
            Path(self.temp.name) / "identity.sqlite3"
        )

    def tearDown(self):
        self.repository.close()
        self.temp.cleanup()

    def test_bootstrap_admin_binds_verified_cloudflare_identity(self):
        self.repository.bootstrap_admins(("Owner@Example.com",))
        user = self.repository.resolve_identity(
            AccessIdentity(subject="cf-user-1", email="owner@example.com")
        )

        self.assertIsNotNone(user)
        assert user is not None
        self.assertEqual(user.role, Role.ADMIN)
        self.assertEqual(user.status, AccountStatus.ACTIVE)
        self.assertEqual(user.trading_mode, TradingMode.DISABLED)
        self.assertEqual(user.access_subject, "cf-user-1")
        self.assertIn("admin.users.manage", user.permissions)
        self.assertNotIn("orders.live", user.permissions)

    def test_identity_does_not_auto_provision_uninvited_email(self):
        user = self.repository.resolve_identity(
            AccessIdentity(subject="unknown", email="unknown@example.com")
        )
        self.assertIsNone(user)
        self.assertEqual(self.repository.users(), [])

    def test_access_request_is_deduplicated_and_can_be_reviewed(self):
        admin = self.repository.create_user(
            "admin@example.com", role=Role.ADMIN
        )
        identity = AccessIdentity(
            subject="cf-applicant", email="Applicant@Example.com"
        )

        first = self.repository.submit_access_request(identity)
        repeated = self.repository.submit_access_request(identity)

        self.assertEqual(first.request_id, repeated.request_id)
        self.assertEqual(repeated.email, "applicant@example.com")
        self.assertEqual(repeated.status.value, "pending")
        self.assertEqual(len(self.repository.access_requests()), 1)

        rejected = self.repository.reject_access_request(
            first.request_id, actor_user_id=admin.user_id
        )
        self.assertEqual(rejected.status.value, "rejected")

        resubmitted = self.repository.submit_access_request(identity)
        self.assertEqual(resubmitted.request_id, first.request_id)
        self.assertEqual(resubmitted.status.value, "pending")

        approved, user = self.repository.approve_access_request(
            first.request_id, actor_user_id=admin.user_id
        )
        self.assertEqual(approved.status.value, "approved")
        self.assertEqual(user.email, "applicant@example.com")
        self.assertEqual(user.role, Role.RESEARCHER)
        self.assertEqual(user.trading_mode, TradingMode.DISABLED)
        self.assertEqual(user.access_subject, "cf-applicant")
        self.assertEqual(
            self.repository.access_requests(AccessRequestStatus.PENDING), []
        )
        self.assertEqual(
            self.repository.resolve_identity(identity).user_id, user.user_id
        )

        actions = [
            event["action"] for event in self.repository.audit_events()
        ]
        self.assertIn("access_request.submitted", actions)
        self.assertIn("access_request.rejected", actions)
        self.assertIn("access_request.approved", actions)

    def test_user_changes_are_audited_and_role_limits_are_enforced(self):
        admin = self.repository.create_user(
            "admin@example.com", role=Role.ADMIN
        )
        researcher = self.repository.create_user(
            "reader@example.com", actor_user_id=admin.user_id
        )
        updated = self.repository.update_user(
            researcher.user_id,
            role=Role.TRADER,
            status=AccountStatus.ACTIVE,
            trading_mode=TradingMode.PAPER,
            actor_user_id=admin.user_id,
        )

        self.assertEqual(updated.role, Role.TRADER)
        self.assertEqual(updated.trading_mode, TradingMode.PAPER)
        self.assertIn("orders.paper", updated.permissions)
        self.assertIn("orders.live", updated.permissions)
        with self.assertRaisesRegex(ValueError, "researcher"):
            self.repository.update_user(
                researcher.user_id,
                role=Role.RESEARCHER,
                status=AccountStatus.ACTIVE,
                trading_mode=TradingMode.LIVE,
            )
        self.assertEqual(
            [event["action"] for event in self.repository.audit_events()],
            ["user.created", "user.created", "user.updated"],
        )

    def test_admin_can_explicitly_enable_paper_but_not_live_trading(self):
        admin = self.repository.create_user(
            "admin@example.com", role=Role.ADMIN
        )
        enabled = self.repository.update_user(
            admin.user_id,
            role=Role.ADMIN,
            status=AccountStatus.ACTIVE,
            trading_mode=TradingMode.PAPER,
        )
        self.assertEqual(enabled.trading_mode, TradingMode.PAPER)
        self.assertIn("orders.paper", enabled.permissions)
        self.assertIn("positions.read.own", enabled.permissions)
        self.assertNotIn("orders.live", enabled.permissions)
        with self.assertRaisesRegex(ValueError, "admin.*live"):
            self.repository.update_user(
                admin.user_id,
                role=Role.ADMIN,
                status=AccountStatus.ACTIVE,
                trading_mode=TradingMode.LIVE,
            )

    def test_bootstrap_does_not_silently_promote_existing_user(self):
        self.repository.create_user("reader@example.com")
        with self.assertRaisesRegex(ValueError, "non-admin"):
            self.repository.bootstrap_admins(("reader@example.com",))

    def test_enforced_mode_rejects_suspended_and_unknown_accounts(self):
        user = self.repository.create_user("reader@example.com")
        self.repository.update_user(
            user.user_id,
            role=Role.RESEARCHER,
            status=AccountStatus.SUSPENDED,
            trading_mode=TradingMode.DISABLED,
        )
        service = AuthService(
            self.repository, authorization_mode="enforced"
        )
        with self.assertRaisesRegex(AuthorizationError, "suspended"):
            service.identify(
                AccessIdentity(subject="reader", email="reader@example.com")
            )
        with self.assertRaisesRegex(AuthorizationError, "not registered"):
            service.identify(
                AccessIdentity(subject="unknown", email="none@example.com")
            )


class AuthApiTests(PublicTestCase):
    def test_api_me_and_forward_auth_use_registered_account(self):
        class FakeAccessValidator:
            def authenticate(self, token):
                if token == "owner-token":
                    return AccessIdentity(
                        subject="cf-owner", email="owner@example.com"
                    )
                if token == "unknown-token":
                    return AccessIdentity(
                        subject="cf-unknown", email="unknown@example.com"
                    )
                raise AccessTokenError("invalid assertion")

        temp = tempfile.TemporaryDirectory()
        db_path = Path(temp.name) / "auth-api.sqlite3"
        bars = SQLiteBarRepository(db_path)
        identities = SQLiteAuthRepository(db_path)
        settings = LiveSettings(
            mode="mock",
            db_path=str(db_path),
            replay_csv=str(synthetic_csv()),
            replay_speed=1000,
            heartbeat_seconds=0.05,
            access_mode="cloudflare",
            cloudflare_access_team_domain="team.cloudflareaccess.com",
            cloudflare_access_audience="audience",
            authorization_mode="enforced",
            bootstrap_admin_emails=("owner@example.com",),
        )
        app = create_app(
            settings,
            feed=ReplayFeed(settings.replay_csv, speed=1000, loop=False),
            repository=bars,
            access_validator=FakeAccessValidator(),
            auth_repository=identities,
         strategy_provider=InertProvider())
        try:
            with TestClient(app) as client:
                response = client.get(
                    "/api/me",
                    headers={"Cf-Access-Jwt-Assertion": "owner-token"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["role"], "admin")
                self.assertTrue(response.json()["registered"])
                self.assertTrue(response.json()["authorization_enforced"])

                accepted = client.get(
                    "/internal/auth/cloudflare",
                    headers={"Cf-Access-Jwt-Assertion": "owner-token"},
                )
                self.assertEqual(accepted.status_code, 204)
                self.assertEqual(
                    accepted.headers["x-authenticated-role"], "admin"
                )

                denied = client.get(
                    "/internal/auth/cloudflare",
                    headers={"Cf-Access-Jwt-Assertion": "unknown-token"},
                )
                self.assertEqual(denied.status_code, 403)
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()

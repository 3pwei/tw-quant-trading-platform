import unittest
from unittest.mock import patch

from tw_quant.live.settings import LiveSettings


class LiveSettingsTests(unittest.TestCase):
    def test_cloudflare_mode_requires_team_domain_and_audience(self):
        settings = LiveSettings(access_mode="cloudflare")
        with self.assertRaisesRegex(ValueError, "CF_ACCESS_TEAM_DOMAIN"):
            settings.validate()

    def test_cloudflare_access_settings_load_from_environment(self):
        environment = {
            "PLATFORM_ENVIRONMENT": "development",
            "MARKET_ACCESS_MODE": "cloudflare",
            "CF_ACCESS_TEAM_DOMAIN": "example.cloudflareaccess.com",
            "CF_ACCESS_AUD": "audience-tag",
        }
        with patch.dict("os.environ", environment, clear=True):
            settings = LiveSettings.from_env()
        settings.validate()
        self.assertEqual(settings.access_mode, "cloudflare")
        self.assertEqual(
            settings.cloudflare_access_team_domain,
            "example.cloudflareaccess.com",
        )
        self.assertEqual(settings.cloudflare_access_audience, "audience-tag")

    def test_history_settings_load_and_validate(self):
        with patch.dict("os.environ", {
            "PLATFORM_ENVIRONMENT": "development",
            "MARKET_HISTORY_DAYS": "14",
            "MARKET_HISTORY_LIMIT": "800",
        }, clear=True):
            settings = LiveSettings.from_env()
        settings.validate()
        self.assertEqual(settings.history_days, 14)
        self.assertEqual(settings.history_limit, 800)

        with self.assertRaisesRegex(ValueError, "MARKET_HISTORY_DAYS"):
            LiveSettings(history_days=31).validate()

    def test_market_stale_threshold_loads_and_validates(self):
        with patch.dict(
            "os.environ", {
                "PLATFORM_ENVIRONMENT": "development",
                "MARKET_STALE_AFTER_SECONDS": "45",
            }, clear=True
        ):
            settings = LiveSettings.from_env()
        self.assertEqual(settings.stale_after_seconds, 45)
        with self.assertRaisesRegex(ValueError, "MARKET_STALE_AFTER_SECONDS"):
            LiveSettings(stale_after_seconds=0).validate()

    def test_market_data_settings_are_composed_not_flattened(self):
        with patch.dict("os.environ", {
            "PLATFORM_ENVIRONMENT": "development",
            "MARKET_DATA_PROVIDER": "replay",
            "MARKET_SYMBOL": "TMF",
        }, clear=True):
            settings = LiveSettings.from_env()
        self.assertEqual(settings.market_data.provider, "replay")
        self.assertEqual(settings.market_data.symbol, "TMF")
        # Compatibility properties keep existing deployments and callers valid.
        self.assertEqual(settings.mode, "mock")
        self.assertEqual(settings.symbol, "TMF")

    def test_authorization_foundation_loads_bootstrap_admins(self):
        with patch.dict("os.environ", {
            "PLATFORM_ENVIRONMENT": "development",
            "PLATFORM_AUTHORIZATION_MODE": "enforced",
            "PLATFORM_BOOTSTRAP_ADMIN_EMAILS": (
                "Owner@Example.com, second@example.com"
            ),
        }, clear=True):
            settings = LiveSettings.from_env()
        settings.validate()
        self.assertEqual(settings.authorization_mode, "enforced")
        self.assertEqual(
            settings.bootstrap_admin_emails,
            ("owner@example.com", "second@example.com"),
        )

    def test_enforced_authorization_requires_bootstrap_admin(self):
        settings = LiveSettings(authorization_mode="enforced")
        with self.assertRaisesRegex(
            ValueError, "PLATFORM_BOOTSTRAP_ADMIN_EMAILS"
        ):
            settings.validate()

    def test_environment_defaults_to_fail_closed_production(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = LiveSettings.from_env()
        self.assertEqual(settings.environment, "production")
        with self.assertRaisesRegex(
            ValueError, "production requires MARKET_ACCESS_MODE=cloudflare"
        ):
            settings.validate()

    def test_production_rejects_disabled_platform_authorization(self):
        settings = LiveSettings(
            environment="production",
            access_mode="cloudflare",
            cloudflare_access_team_domain="example.cloudflareaccess.com",
            cloudflare_access_audience="audience-tag",
            authorization_mode="disabled",
            bootstrap_admin_emails=("owner@example.com",),
        )
        with self.assertRaisesRegex(
            ValueError,
            "production requires PLATFORM_AUTHORIZATION_MODE=enforced",
        ):
            settings.validate()

    def test_secure_production_authorization_is_valid(self):
        LiveSettings(
            environment="production",
            access_mode="cloudflare",
            cloudflare_access_team_domain="example.cloudflareaccess.com",
            cloudflare_access_audience="audience-tag",
            authorization_mode="enforced",
            bootstrap_admin_emails=("owner@example.com",),
        ).validate()

    def test_rate_limits_load_from_environment(self):
        with patch.dict(
            "os.environ",
            {
                "PLATFORM_ENVIRONMENT": "development",
                "RATE_LIMIT_ACCESS_REQUESTS_PER_HOUR": "7",
                "RATE_LIMIT_BACKTESTS_PER_MINUTE": "8",
                "RATE_LIMIT_REPLAY_PREPARES_PER_MINUTE": "9",
                "RATE_LIMIT_ORDERS_PER_MINUTE": "40",
                "API_MAX_REQUEST_BODY_BYTES": "131072",
            },
            clear=True,
        ):
            settings = LiveSettings.from_env()
        settings.validate()
        self.assertEqual(settings.rate_limit_access_requests_per_hour, 7)
        self.assertEqual(settings.rate_limit_backtests_per_minute, 8)
        self.assertEqual(settings.rate_limit_replay_prepares_per_minute, 9)
        self.assertEqual(settings.rate_limit_orders_per_minute, 40)
        self.assertEqual(settings.max_request_body_bytes, 131072)

    def test_rate_limits_must_be_positive(self):
        with self.assertRaisesRegex(
            ValueError, "RATE_LIMIT_BACKTESTS_PER_MINUTE must be positive"
        ):
            LiveSettings(rate_limit_backtests_per_minute=0).validate()

    def test_request_body_limit_has_secure_bounds(self):
        for value in (1023, 1024 * 1024 + 1):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "API_MAX_REQUEST_BODY_BYTES"
            ):
                LiveSettings(max_request_body_bytes=value).validate()


if __name__ == "__main__":
    unittest.main()

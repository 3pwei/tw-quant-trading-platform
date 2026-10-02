import unittest

from tw_quant.live.api_routes.admin import _public_live_execution
from tw_quant.live.api_routes.live_auto import _public_runtime
from tw_quant.live.api_routes.live_canary import _public_status


class ProviderIdentityViewTests(unittest.TestCase):
    def test_live_auto_public_runtime_hides_internal_provider_identity(self):
        result = _public_runtime({
            "runtime_id": "runtime-1",
            "broker_name": "internal-provider",
            "account_id": "secret-account",
            "masked_account_id": "****1234",
        })
        self.assertEqual(result["provider_label"], "configured")
        self.assertNotIn("broker_name", result)
        self.assertNotIn("account_id", result)

    def test_live_canary_public_status_hides_internal_provider_identity(self):
        result = _public_status({
            "enabled": True,
            "broker_name": "internal-provider",
            "account_id": "secret-account",
            "masked_account_id": "****1234",
        })
        self.assertEqual(result["provider_label"], "configured")
        self.assertNotIn("broker_name", result)
        self.assertNotIn("account_id", result)

    def test_admin_health_hides_provider_identity_per_account(self):
        result = _public_live_execution({
            "state": "ready_read_only",
            "broker_accounts": [{
                "broker_name": "internal-provider",
                "account_id": "secret-account",
                "masked_account_id": "****1234",
            }],
        })
        account = result["broker_accounts"][0]
        self.assertEqual(account["provider_label"], "configured")
        self.assertNotIn("broker_name", account)
        self.assertNotIn("account_id", account)


if __name__ == "__main__":
    unittest.main()

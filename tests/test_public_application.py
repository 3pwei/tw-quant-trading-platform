"""Candidate-only HTTP product contracts using inert providers and synthetic rows."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient
from public_fixtures import InertProvider, PublicTestCase
from tw_quant.auth import AccessIdentity, AccessTokenError, Role, SQLiteAuthRepository
from tw_quant.live.api import create_app
from tw_quant.live.settings import LiveSettings
from tw_quant.live.storage import SQLiteBarRepository
from tw_quant.market import KBar, TAIPEI


class Feed:
    contract = "TEST1"
    async def start(self, _on_tick, on_status): on_status("connected")
    async def stop(self): pass
    async def heartbeat(self): return True


class Access:
    def authenticate(self, token):
        if token not in {"owner-a", "owner-b"}: raise AccessTokenError("invalid assertion")
        return AccessIdentity(token, token + "@example.invalid")


class PublicApplicationTests(PublicTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        path = Path(self.temporary.name) / "http.sqlite3"
        auth = SQLiteAuthRepository(path)
        auth.create_user("owner-a@example.invalid", role=Role.ADMIN)
        auth.create_user("owner-b@example.invalid", role=Role.RESEARCHER)
        repo = SQLiteBarRepository(path)
        at = datetime(2026, 8, 24, 15, 0, tzinfo=TAIPEI)
        repo.save(KBar("TEST", "TEST1", at, 100, 101, 99, 100, 1, "closed", "night", (at + timedelta(days=1)).date(), at, at, at, at, 0))
        settings = LiveSettings(environment="test", symbol="TEST", db_path=str(path),
            access_mode="cloudflare", cloudflare_access_team_domain="example.cloudflareaccess.com",
            cloudflare_access_audience="test", authorization_mode="enforced",
            bootstrap_admin_emails=("owner-a@example.invalid",))
        self.provider = InertProvider(composite=True)
        self.client = TestClient(create_app(settings, feed=Feed(), repository=repo,
            auth_repository=auth, access_validator=Access(), strategy_provider=self.provider))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    @staticmethod
    def headers(owner): return {"Cf-Access-Jwt-Assertion": owner}

    def test_health_market_and_catalog_contracts_are_broker_neutral(self):
        self.assertEqual(self.client.get("/health/live").json(), {"status": "ok"})
        response = self.client.get("/api/health", headers=self.headers("owner-b"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("market_data_provider", response.json())
        rows = self.client.get("/api/kbars?symbol=TEST", headers=self.headers("owner-b")).json()
        self.assertEqual(rows[0]["symbol"], "TEST")
        catalog = self.client.get("/api/strategies", headers=self.headers("owner-b")).json()
        self.assertEqual([item["key"] for item in catalog["strategies"]], ["scripted"])

    def test_user_parameter_values_and_backtest_history_are_owner_isolated(self):
        for owner, value in (("owner-a", 3), ("owner-b", 20)):
            response = self.client.put("/api/strategies/scripted", headers=self.headers(owner), json={"parameters": {"window": value}})
            self.assertEqual(response.status_code, 200, response.text)
        for owner, value in (("owner-a", 3), ("owner-b", 20)):
            response = self.client.get("/api/strategies", headers=self.headers(owner))
            self.assertEqual(response.json()["strategies"][0]["parameters"]["window"], value)
        response = self.client.post("/api/backtest-runs", headers=self.headers("owner-b"), json={"symbol": "TEST", "strategy": "scripted", "start": "2026-08-25", "end": "2026-08-25"})
        self.assertEqual(response.status_code, 201, response.text)
        run = response.json()["history_run_id"]
        self.assertEqual(response.json()["trades"], [])
        for method in (self.client.get, self.client.delete):
            self.assertEqual(method("/api/backtest-runs/" + run, headers=self.headers("owner-a")).status_code, 404)
        self.assertEqual(self.client.get("/api/backtest-runs", headers=self.headers("owner-a")).json()["runs"], [])
        self.assertEqual(self.client.get("/api/backtest-runs/" + run, headers=self.headers("owner-b")).status_code, 200)

    def test_unknown_key_and_private_topology_are_rejected(self):
        response = self.client.put("/api/strategies/unknown", headers=self.headers("owner-b"), json={"parameters": {"window": 1}})
        self.assertEqual(response.status_code, 404)
        response = self.client.post("/api/composite-strategies", headers=self.headers("owner-b"), json={"definition": {"unreviewed_topology": []}})
        self.assertEqual(response.status_code, 422)

    def test_composite_display_identity_versions_and_history_use_core_envelopes(self):
        owner = self.headers("owner-b")
        catalog = self.client.get("/api/composite-strategies", headers=owner)
        self.assertEqual(catalog.status_code, 200, catalog.text)
        definition = catalog.json()["template"]
        definition.update(name="Generic composite", composite_id="client-cannot-select-id", composite_version=900)
        definition["members"] = [{"member_id": "member-1", "strategy": "scripted", "strategy_version": "1", "parameter_schema_version": "1", "parameters": self.provider.values}]
        first = self.client.post("/api/composite-strategies", headers=owner, json={"definition": definition})
        self.assertEqual(first.status_code, 201, first.text)
        saved = first.json(); identifier = saved["id"]
        self.assertNotEqual(identifier, "client-cannot-select-id")
        self.assertEqual(saved["definition"]["composite_id"], identifier)
        self.assertEqual(saved["definition"]["composite_version"], 1)
        second = self.client.put("/api/composite-strategies/" + identifier, headers=owner, json={"definition": saved["definition"]})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["id"], identifier)
        self.assertEqual(second.json()["definition"]["composite_version"], 2)
        historical = self.client.get("/api/composite-strategies/" + identifier + "?version=1", headers=owner)
        self.assertEqual(historical.json()["definition"]["composite_version"], 1)
        self.assertEqual(self.client.get("/api/composite-strategies/" + identifier, headers=self.headers("owner-a")).status_code, 404)
        result = self.client.post("/api/backtest-runs", headers=owner, json={"symbol": "TEST", "strategy": "composite:" + identifier, "start": "2026-08-25", "end": "2026-08-25"})
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()["trades"], [])

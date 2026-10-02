"""CI-only FastAPI origin for the real Caddy forward_auth integration test."""
import tempfile
from pathlib import Path
from tw_quant.synthetic_data import write_ticks
from public_fixtures import InertProvider, services
from tw_quant.live.demo_backtest import DemoExecutionInput
from tw_quant.auth import AccessIdentity, AccessTokenError, Role, SQLiteAuthRepository
from tw_quant.live.api import create_app
from tw_quant.live.feed import ReplayFeed
from tw_quant.live.settings import LiveSettings
from tw_quant.live.storage import SQLiteBarRepository


class FixtureAccessValidator:
    def authenticate(self, token):
        identities = {
            "fixture-reader": AccessIdentity("fixture-reader", "reader@example.com"),
            "fixture-admin": AccessIdentity("fixture-admin", "admin@example.com"),
        }
        if token not in identities:
            raise AccessTokenError("invalid assertion")
        return identities[token]


temporary = tempfile.TemporaryDirectory(prefix="gateway-fixture-")
root = Path(temporary.name)
write_ticks(root / "synthetic.csv")
settings = LiveSettings(
    mode="mock", db_path=str(root / "gateway.sqlite3"),
    replay_csv=str(root / "synthetic.csv"),
    access_mode="cloudflare",
    cloudflare_access_team_domain="fixture.cloudflareaccess.com",
    cloudflare_access_audience="fixture-audience",
    authorization_mode="enforced",
    bootstrap_admin_emails=("admin@example.com",),
    environment="development",
)
identities = SQLiteAuthRepository(settings.db_path)
identities.create_user("reader@example.com", role=Role.RESEARCHER)
class InertDemoProvider:
    def case_catalog(self):
        return ({"case_id": "inert-case", "synthetic_data": True},)

    def case_execution_input(self, case_id):
        provider = InertProvider()
        return DemoExecutionInput(case_id, "analyze", services().request("scripted", [], provider.values))


app = create_app(
    settings,
    feed=ReplayFeed(settings.replay_csv, loop=False),
    repository=SQLiteBarRepository(settings.db_path),
    access_validator=FixtureAccessValidator(),
    strategy_provider=InertProvider(),
    demo_provider=InertDemoProvider(),
    auth_repository=identities,
)

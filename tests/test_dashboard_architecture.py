import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard" / "app"


class DashboardArchitectureTests(unittest.TestCase):
    def source(self, relative_path: str) -> str:
        return (DASHBOARD / relative_path).read_text(encoding="utf-8")

    def test_live_page_delegates_chart_and_socket_lifecycles(self):
        source = self.source("live/live-dashboard.tsx")
        self.assertIn("useMarketSocket", source)
        self.assertIn("useTradingChart", source)
        self.assertNotIn("new WebSocket", source)
        self.assertNotIn("createChart", source)

    def test_paper_page_delegates_account_polling(self):
        source = self.source("paper/paper-trading-dashboard.tsx")
        self.assertIn("usePaperAccount", source)
        self.assertNotIn("setInterval", source)
        self.assertNotIn("NEXT_PUBLIC_MARKET_API_URL", source)
        hook = self.source("paper/use-paper-account.ts")
        self.assertIn('from "./types"', hook)
        self.assertNotIn('from "./paper-trading-dashboard"', hook)

    def test_replay_page_delegates_chart_and_uses_shared_client(self):
        source = self.source("replay/replay-dashboard.tsx")
        self.assertIn("useReplayChart", source)
        self.assertIn("../lib/api-client", source)
        self.assertNotIn("createChart", source)
        self.assertNotIn("NEXT_PUBLIC_MARKET_API_URL", source)
        hook = self.source("replay/use-replay-chart.ts")
        self.assertIn('from "./types"', hook)
        self.assertNotIn('from "./replay-dashboard"', hook)

    def test_dashboard_behavior_tests_are_ci_gated(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        package = (ROOT / "dashboard/package.json").read_text(encoding="utf-8")
        self.assertIn("npm test", workflow)
        self.assertIn('"test":', package)

    def test_settings_shows_broker_neutral_read_only_health_without_actions(self):
        source = self.source("settings/system-health.tsx")
        self.assertIn("broker_accounts", source)
        self.assertIn("READY_READ_ONLY", source.replace("ready_read_only", "READY_READ_ONLY"))
        self.assertIn("POSITION MISMATCH", source)
        self.assertIn("UNKNOWN BROKER ORDER", source)
        for action in ("ARM LIVE", "BUY LIVE", "SELL LIVE", "CANCEL LIVE", "FLATTEN"):
            self.assertNotIn(action, source)


if __name__ == "__main__":
    unittest.main()

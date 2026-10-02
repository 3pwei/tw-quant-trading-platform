
from public_fixtures import InertProvider, PublicTestCase, PublicAsyncTestCase, synthetic_csv, services
import unittest

from tw_quant.backtest import run_strategy_backtest
from tw_quant.live.backtest import run_live_strategy_backtest
from tw_quant.live.models import KBar as LegacyKBar
from tw_quant.live.strategy_analysis import analyze_live_strategies
from tw_quant.market import KBar
from tw_quant.risk import RiskConfig, calculate_levels, triggered_exit
from tw_quant.strategy_registry import analyze_strategies


class SharedArchitectureTests(PublicTestCase):
    def test_legacy_imports_are_aliases_of_shared_domain_functions(self):
        self.assertIs(LegacyKBar, KBar)
        self.assertEqual(analyze_live_strategies([], ["scripted"]), analyze_strategies([], ["scripted"]))
        self.assertIs(run_live_strategy_backtest, run_strategy_backtest)

    def test_risk_levels_are_direction_aware(self):
        risk = RiskConfig(stop_loss_pct=0.01, take_profit_pct=0.02)
        long_levels = calculate_levels(100, 1, risk)
        short_levels = calculate_levels(100, -1, risk)
        self.assertEqual(long_levels.stop_loss_price, 99)
        self.assertEqual(long_levels.take_profit_price, 102)
        self.assertEqual(short_levels.stop_loss_price, 101)
        self.assertEqual(short_levels.take_profit_price, 98)

    def test_stop_loss_has_priority_when_same_bar_touches_both_levels(self):
        levels = calculate_levels(100, 1, RiskConfig(0.01, 0.02))
        self.assertEqual(
            triggered_exit(
                direction=1,
                open_price=100,
                high=103,
                low=98,
                levels=levels,
            ),
            (99, "stop_loss"),
        )


if __name__ == "__main__":
    unittest.main()

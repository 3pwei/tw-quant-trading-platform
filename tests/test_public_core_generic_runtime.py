from __future__ import annotations

from dataclasses import asdict
import os
import subprocess
import sys
import unittest


@unittest.skipUnless(
    True,
    "Public Core is installed only by the dedicated integration job",
)
class PublicCoreGenericRuntimeTests(unittest.TestCase):
    def test_selected_strategy_execution_and_risk_contracts_use_public_core(self) -> None:
        from tw_quant.execution.policy import SignalSimulationPolicy
        from tw_quant.execution.position_ledger import PositionLedger
        from tw_quant.risk.engine import RiskConfig
        from tw_quant_core.strategy import Decision, StrategyRuntime
        from tw_quant_core.execution import (
            PositionLedger as CorePositionLedger,
            SignalSimulationPolicy as CoreSignalSimulationPolicy,
        )
        from tw_quant_core.risk import RiskConfig as CoreRiskConfig
        from tw_quant_core.strategy import (
            Decision as CoreDecision,
            StrategyRuntime as CoreStrategyRuntime,
        )

        self.assertIs(StrategyRuntime, CoreStrategyRuntime)
        self.assertIs(Decision, CoreDecision)
        self.assertIs(PositionLedger, CorePositionLedger)
        self.assertIs(SignalSimulationPolicy, CoreSignalSimulationPolicy)
        self.assertIs(RiskConfig, CoreRiskConfig)

    def test_public_core_generic_behavior_remains_stable(self) -> None:
        from tw_quant.execution.policy import SignalSimulationPolicy
        from tw_quant.execution.position_ledger import PositionLedger
        from tw_quant.risk.engine import RiskConfig, calculate_levels, triggered_exit
        from tw_quant_core.strategy import Decision, StrategyRuntime

        class Strategy:
            def evaluate(self, bars):
                return Decision("buy", "test", len(bars))

        self.assertEqual(
            asdict(StrategyRuntime(Strategy()).on_closed_bar((object(),))),
            {"side": "buy", "reason": "test", "quantity": 1},
        )
        self.assertEqual(asdict(SignalSimulationPolicy("exit")), {"strategy_exit_reason": "exit"})
        self.assertEqual(PositionLedger(multiplier=10).open_positions(), ())
        levels = calculate_levels(20_000, 1, RiskConfig(0.01, 0.02))
        self.assertEqual(
            triggered_exit(
                direction=1,
                open_price=20_000,
                high=20_500,
                low=19_700,
                levels=levels,
            ),
            (levels.stop_loss_price, "stop_loss"),
        )

    def test_retired_environment_flag_cannot_restore_generic_legacy_code(self) -> None:
        env = os.environ.copy()
        env["TW_QUANT_CORE_ROLLBACK"] = "1"
        command = (
            "from tw_quant_core.strategy import StrategyRuntime; "
            "from tw_quant.execution.position_ledger import PositionLedger; "
            "from tw_quant.execution.policy import SignalSimulationPolicy; "
            "from tw_quant.risk.engine import RiskConfig; "
            "assert StrategyRuntime.__module__.startswith('tw_quant_core.'); "
            "assert PositionLedger.__module__.startswith('tw_quant_core.'); "
            "assert SignalSimulationPolicy.__module__.startswith('tw_quant_core.'); "
            "assert RiskConfig.__module__.startswith('tw_quant_core.')"
        )
        subprocess.run([sys.executable, "-c", command], env=env, check=True)


if __name__ == "__main__":
    unittest.main()

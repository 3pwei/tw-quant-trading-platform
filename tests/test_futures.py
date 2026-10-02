
from public_fixtures import InertProvider, PublicTestCase, PublicAsyncTestCase, synthetic_csv, services
import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from tw_quant.cli import run_futures_night
from tw_quant_core.futures import (
    FuturesCostConfig,
    taifex_bars_to_kbars,
    ticks_to_bars,
)


class FuturesDataTests(PublicTestCase):
    def test_micro_taiex_side_cost_uses_multiplier_and_tax(self):
        costs = FuturesCostConfig(
            multiplier=10,
            commission_per_side=10,
            tax_rate=0.00002,
            slippage_points=1,
        )
        commission, tax = costs.side_cost(45_000, contracts=1)
        self.assertEqual(commission, 10)
        self.assertEqual(tax, 9)

    def test_ticks_are_aggregated_to_five_minute_bars(self):
        ticks = pd.DataFrame({
            "timestamp": pd.to_datetime([
                "2026-08-24 15:00:01+08:00",
                "2026-08-24 15:04:59+08:00",
                "2026-08-24 15:05:00+08:00",
            ]),
            "price": [100, 103, 102],
            "volume": [1, 2, 3],
        })
        bars = ticks_to_bars(
            ticks,
            interval="5min",
            session_start="2026-08-24 15:00",
        )
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars.iloc[0]["open"], 100)
        self.assertEqual(bars.iloc[0]["high"], 103)
        self.assertEqual(bars.iloc[0]["close"], 103)
        self.assertEqual(bars.iloc[0]["volume"], 3)

    def test_imported_bars_use_canonical_kbar_and_night_trading_date(self):
        ticks = pd.DataFrame({
            "timestamp": pd.to_datetime([
                "2026-08-24 15:00:01+08:00",
                "2026-08-24 15:00:59+08:00",
            ]),
            "price": [100, 103],
            "volume": [1, 2],
        })
        frame = ticks_to_bars(
            ticks, interval="1min", session_start="2026-08-24 15:00"
        )
        bars = taifex_bars_to_kbars(
            frame, symbol="TMF", contract="TMF202609"
        )
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].contract, "TMF202609")
        self.assertEqual(bars[0].session, "night")
        self.assertEqual(bars[0].trading_date.isoformat(), "2026-08-25")
        self.assertEqual(bars[0].status, "closed")

    def test_futures_night_cli_delegates_to_shared_strategy_backtest(self):
        ticks = pd.DataFrame({
            "timestamp": pd.to_datetime([
                "2026-08-24 15:00:01+08:00",
                "2026-08-24 15:01:01+08:00",
            ]),
            "price": [100, 101],
            "volume": [1, 1],
        })
        result = {
            "bars": [{"timestamp": "2026-08-24T15:00:00+08:00"}],
            "trades": [],
            "equity": [{"timestamp": "2026-08-24T15:00:00+08:00", "equity": 100000}],
            "summary": {"trades": 0, "net_profit": 0.0},
        }
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                csv="unused.csv", product="TMF", contract_month="202609",
                session_start="2026-08-24 15:00",
                session_end="2026-08-25 05:00", strategy="scripted",
                initial_capital=100_000, contracts=1,
                contract_multiplier=10, commission_per_side=10,
                tax_rate=0.00002, slippage_points=1,
                output=str(Path(directory) / "result"),
            )
            with patch("tw_quant.cli.load_taifex_ticks", return_value=ticks), patch(
                "tw_quant.cli.run_strategy_backtest", return_value=result
            ) as shared_backtest:
                run_futures_night(args)
            shared_backtest.assert_called_once()
            self.assertEqual(shared_backtest.call_args.args[1], "scripted")
            self.assertTrue((Path(args.output) / "summary.json").exists())


if __name__ == "__main__":
    unittest.main()

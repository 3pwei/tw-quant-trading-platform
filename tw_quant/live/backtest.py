"""Backward-compatible backtest imports.

The historical runner is not a live-market responsibility and now lives in
:mod:`tw_quant.backtest`.
"""

from ..backtest import MAX_BACKTEST_DAYS, run_strategy_backtest, validate_date_range

run_live_strategy_backtest = run_strategy_backtest

__all__ = [
    "MAX_BACKTEST_DAYS",
    "run_live_strategy_backtest",
    "run_strategy_backtest",
    "validate_date_range",
]

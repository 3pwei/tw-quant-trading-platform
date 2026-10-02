from __future__ import annotations

from datetime import date

import pandas as pd

from tw_quant_core.futures_costs import FuturesCostConfig
from ..market import KBar, TIMEFRAME_LABELS, TIMEFRAME_MINUTES, validate_timeframe
from tw_quant_core.metrics import build_equity_curve, calculate_summary
from ..strategy_registry import (
    supported_strategies,
    analyze_strategies,
    generate_composite_signals,
)
from .event_runner import run_historical_events


MAX_BACKTEST_DAYS = 31
INITIAL_CAPITAL = 100_000.0


def validate_date_range(start: date, end: date) -> None:
    if start > end:
        raise ValueError("開始日期不可晚於結束日期")
    if (end - start).days + 1 > MAX_BACKTEST_DAYS:
        raise ValueError(f"回測區間最多 {MAX_BACKTEST_DAYS} 天")


def run_strategy_backtest(
    bars: list[KBar],
    strategy: str,
    start: date,
    end: date,
    *,
    initial_capital: float = INITIAL_CAPITAL,
    contracts: int = 1,
    costs: FuturesCostConfig | None = None,
    source: str = "行情資料庫（已收盤 1 分 K）",
    parameters: dict[str, object] | None = None,
    interval: str = "1m",
) -> dict[str, object]:
    validate_date_range(start, end)
    selected_interval = validate_timeframe(interval)
    strategy = strategy.lower()
    if strategy not in supported_strategies():
        raise ValueError(f"unsupported strategy: {strategy}")
    closed = [bar for bar in bars if bar.status == "closed"]
    if not closed:
        raise ValueError("所選區間沒有可用的已收盤 K 棒")

    analysis = analyze_strategies(
        closed,
        [strategy],
        force_close_last=True,
        parameters={strategy: parameters or {}},
        interval=selected_interval,
    )["strategies"][0]
    signals = analysis["signals"]
    cost_config = costs or FuturesCostConfig()
    event_run = run_historical_events(
        closed,
        signals,
        strategy_id=strategy,
        quantity=contracts,
        initial_capital=initial_capital,
        costs=cost_config,
        timeframe=selected_interval,
    )
    trades = list(event_run.trades)

    bar_frame = pd.DataFrame(
        [{"timestamp": bar.time, "close": bar.close} for bar in closed]
    )
    trade_frame = pd.DataFrame(trades)
    equity = build_equity_curve(trade_frame, initial_capital, bar_frame)
    summary = calculate_summary(trade_frame, equity, initial_capital)

    serialized_trades = []
    for trade in trades:
        item = dict(trade)
        item["trigger_time"] = trade["trigger_time"].isoformat(
            timespec="milliseconds"
        )
        item["entry_time"] = trade["entry_time"].isoformat(timespec="milliseconds")
        item["exit_time"] = trade["exit_time"].isoformat(timespec="milliseconds")
        serialized_trades.append(item)

    return {
        "metadata": {
            "symbol": closed[0].symbol,
            "display_name": "微型臺指期貨",
            "strategy": analysis["name"],
            "strategy_key": strategy,
            "interval": TIMEFRAME_LABELS[selected_interval],
            "interval_key": selected_interval,
            "date_range": f"{start.isoformat()} ～ {end.isoformat()}",
            "is_synthetic": False,
            "source": source,
        },
        "config": {
            "initial_capital": initial_capital,
            "quantity": contracts,
            "quantity_unit": "口",
            "parameters": dict(analysis["parameters"]),
            "bar_minutes": TIMEFRAME_MINUTES[selected_interval] or 0,
            "force_exit_time": "每交易時段結束",
            "commission_rate": 0,
            "commission_per_side": cost_config.commission_per_side,
            "sell_tax_rate": cost_config.tax_rate,
            "slippage_bps": 0,
            "slippage_points": cost_config.slippage_points,
            "contract_multiplier": cost_config.multiplier,
        },
        "summary": summary,
        "execution": {
            "engine": "deterministic_event_engine",
            "event_counts": event_run.event_counts,
        },
        "overlays": analysis.get("overlays", []),
        "visualization": analysis.get("visualization"),
        "bars": [
            {
                "timestamp": bar.time.isoformat(timespec="milliseconds"),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "contract": bar.contract,
                "session": bar.session,
                "trading_date": bar.trading_date.isoformat(),
            }
            for bar in closed
        ],
        "trades": serialized_trades,
        "equity": [
            {**row, "timestamp": pd.Timestamp(row["timestamp"]).isoformat()}
            for row in equity.to_dict(orient="records")
        ],
    }


def run_composite_backtest(
    bars: list[KBar],
    definition: dict[str, object],
    strategy_id: str,
    version: int,
    start: date,
    end: date,
    *,
    initial_capital: float = INITIAL_CAPITAL,
    contracts: int = 1,
    costs: FuturesCostConfig | None = None,
    source: str = "行情資料庫（已收盤 1 分 K）",
) -> dict[str, object]:
    """Backtest one immutable composite strategy version from source 1m bars."""
    validate_date_range(start, end)
    closed = sorted(
        (bar for bar in bars if bar.status == "closed"), key=lambda bar: bar.time
    )
    if not closed:
        raise ValueError("所選區間沒有可用的已收盤 K 棒")
    signals, trace = generate_composite_signals(closed, definition)
    cost_config = costs or FuturesCostConfig()
    event_run = run_historical_events(
        closed,
        signals,
        strategy_id=strategy_id,
        strategy_version=version,
        quantity=contracts,
        initial_capital=initial_capital,
        costs=cost_config,
        timeframe="1m",
    )
    trades = list(event_run.trades)

    frame = pd.DataFrame(
        [{"timestamp": bar.time, "close": bar.close} for bar in closed]
    )
    trade_frame = pd.DataFrame(trades)
    equity = build_equity_curve(trade_frame, initial_capital, frame)
    summary = calculate_summary(trade_frame, equity, initial_capital)
    return {
        "metadata": {
            "symbol": closed[0].symbol,
            "display_name": "微型臺指期貨",
            "strategy": definition["name"],
            "strategy_key": strategy_id,
            "strategy_version": version,
            "interval": "多週期",
            "interval_key": "multi",
            "date_range": f"{start.isoformat()} ～ {end.isoformat()}",
            "is_synthetic": False,
            "source": source,
        },
        "definition": definition,
        "config": {
            "initial_capital": initial_capital,
            "quantity": contracts,
            "quantity_unit": "口",
            "bar_minutes": 1,
            "force_exit_time": "每交易時段結束",
            "commission_rate": 0,
            "commission_per_side": cost_config.commission_per_side,
            "sell_tax_rate": cost_config.tax_rate,
            "slippage_bps": 0,
            "slippage_points": cost_config.slippage_points,
            "contract_multiplier": cost_config.multiplier,
        },
        "summary": summary,
        "execution": {
            "engine": "deterministic_event_engine",
            "event_counts": event_run.event_counts,
        },
        "bars": [{
            "timestamp": bar.time.isoformat(timespec="milliseconds"),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "contract": bar.contract,
            "session": bar.session,
            "trading_date": bar.trading_date.isoformat(),
        } for bar in closed],
        "trades": [{
            **trade,
            "trigger_time": trade["trigger_time"].isoformat(
                timespec="milliseconds"
            ),
            "entry_time": trade["entry_time"].isoformat(timespec="milliseconds"),
            "exit_time": trade["exit_time"].isoformat(timespec="milliseconds"),
        } for trade in trades],
        "equity": [
            {**row, "timestamp": pd.Timestamp(row["timestamp"]).isoformat()}
            for row in equity.to_dict(orient="records")
        ],
        "trace": trace,
    }

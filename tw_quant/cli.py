"""Public command facade. Trusted callers inject a Core provider or registry."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import pandas as pd
from tw_quant_core.data import load_bars
from tw_quant_core.futures import load_taifex_ticks, taifex_bars_to_kbars, ticks_to_bars
from tw_quant_core.futures_costs import FuturesCostConfig
from .backtest import run_strategy_backtest
from .level2_acceptance import run_level2_soak
from .maintenance import backup_sqlite, restore_sqlite
from .strategy_registry import RegistryStrategies, StrategyUnavailable, get_strategy_services, strategy_scope


def parameter_values(value: str) -> dict:
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("parameters must be a JSON object") from exc
    if not isinstance(result, dict):
        raise argparse.ArgumentTypeError("parameters must be a JSON object")
    return result


def print_summary(summary: dict) -> None:
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))


def write_result(result: dict, output_dir: str) -> None:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for key in ("bars", "trades", "equity"):
        pd.DataFrame(result[key]).to_csv(output / (key + ".csv"), index=False)
    (output / "summary.json").write_text(
        json.dumps(result["summary"], ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    print_summary(result["summary"])


def require_strategy(args):
    services = get_strategy_services()
    # Preflight before reading input or creating output. No provider template
    # or parameters are guessed; explicit parameters still require capability.
    values = services.template(args.strategy) if getattr(args, "parameters", None) is None else args.parameters
    services.normalize(args.strategy, values)
    from tw_quant_core.strategy import StrategyCapability
    services.require_capability(args.strategy, StrategyCapability.ANALYZE)
    return values


def run_backtest(args):
    values = require_strategy(args)
    frame = load_bars(args.csv, args.symbol)
    bars = taifex_bars_to_kbars(frame, symbol=args.symbol, contract=args.contract, interval="1min")
    run_bars(args, bars, values)


def run_bars(args, bars, values):
    if not bars:
        raise ValueError("no canonical bars in the requested input")
    result = run_strategy_backtest(
        bars, args.strategy, min(bar.trading_date for bar in bars), max(bar.trading_date for bar in bars),
        parameters=values, interval=getattr(args, "interval", "1m"), initial_capital=args.initial_capital,
        contracts=args.contracts, costs=FuturesCostConfig(
            multiplier=args.contract_multiplier, commission_per_side=args.commission_per_side,
            tax_rate=args.tax_rate, slippage_points=args.slippage_points,
        ), source="externally supplied CSV",
    )
    write_result(result, args.output)


def run_futures_night(args):
    values = require_strategy(args)
    ticks = load_taifex_ticks(args.csv, product=args.product, contract_month=args.contract_month,
        session_start=args.session_start, session_end=args.session_end)
    frame = ticks_to_bars(ticks, interval="1min", session_start=args.session_start, symbol=args.product)
    bars = taifex_bars_to_kbars(frame, symbol=args.product, contract=args.product + args.contract_month, interval="1min")
    run_bars(args, bars, values)


def run_demo(args):
    from .live.demo_backtest import DemoService
    from fastapi import HTTPException
    if args.demo_provider is None:
        raise StrategyUnavailable("Demo operation requires an injected DemoProvider")
    service = DemoService(args.demo_provider, get_strategy_services())
    try:
        print_summary(service.execute(args.case_id, "cli"))
    except HTTPException as exc:
        raise StrategyUnavailable(str(exc.detail)) from exc
    finally:
        service.close()


def run_level2_acceptance(args):
    report = asyncio.run(run_level2_soak(args.duration_seconds,
        tick_interval_seconds=args.tick_interval_seconds, database_path=args.database))
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    print(payload)
    if args.output:
        path = Path(args.output); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
    if not report.passed:
        raise SystemExit(1)


def build_parser():
    parser = argparse.ArgumentParser(description="Public Platform CLI; strategy operations require an injected provider")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog").set_defaults(func=lambda _args: print_summary({"strategies": get_strategy_services().catalog()}))
    for command, function in (("backtest", run_backtest), ("futures-night", run_futures_night)):
        sub = commands.add_parser(command)
        sub.add_argument("--csv", required=True)
        sub.add_argument("--strategy", required=True)
        sub.add_argument("--parameters", type=parameter_values)
        sub.add_argument("--interval", default="1m")
        sub.add_argument("--output", default="output/backtest")
        sub.add_argument("--initial-capital", type=float, default=100_000)
        sub.add_argument("--contracts", type=int, default=1)
        sub.add_argument("--contract-multiplier", type=float, default=10)
        sub.add_argument("--commission-per-side", type=float, default=10)
        sub.add_argument("--tax-rate", type=float, default=0.00002)
        sub.add_argument("--slippage-points", type=float, default=1)
        if command == "backtest":
            sub.add_argument("--symbol", required=True)
            sub.add_argument("--contract", required=True)
        else:
            sub.add_argument("--product", required=True)
            sub.add_argument("--contract-month", required=True)
            sub.add_argument("--session-start", required=True)
            sub.add_argument("--session-end", required=True)
        sub.set_defaults(func=function)
    demo = commands.add_parser("demo")
    demo.add_argument("--case-id", required=True)
    demo.set_defaults(func=run_demo)
    soak = commands.add_parser("level2-soak")
    soak.add_argument("--duration-seconds", type=float, default=60)
    soak.add_argument("--tick-interval-seconds", type=float, default=0.1)
    soak.add_argument("--database")
    soak.add_argument("--output")
    soak.set_defaults(func=run_level2_acceptance)
    backup = commands.add_parser("sqlite-backup")
    backup.add_argument("--source", required=True); backup.add_argument("--destination", required=True)
    backup.set_defaults(func=lambda args: print(backup_sqlite(args.source, args.destination)))
    restore = commands.add_parser("sqlite-restore")
    restore.add_argument("--backup", required=True); restore.add_argument("--target", required=True)
    restore.set_defaults(func=lambda args: print(restore_sqlite(args.backup, args.target)))
    return parser


def main(argv=None, *, strategy_provider=None, strategy_registry=None, demo_provider=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.demo_provider = demo_provider
    services = RegistryStrategies(strategy_registry, provider=strategy_provider)
    with strategy_scope(services):
        try:
            args.func(args)
        except (StrategyUnavailable, ValueError) as exc:
            parser.exit(2, str(exc) + "\n")

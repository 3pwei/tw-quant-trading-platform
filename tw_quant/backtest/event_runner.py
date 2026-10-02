from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

import pandas as pd

from ..events import (
    BarClosedEvent,
    DeterministicEventEngine,
    EventMetadata,
    SignalEvent,
    event_to_dict,
)
from ..execution import ResearchRiskGate, SimulatedExecutionPipeline
from tw_quant_core.futures_costs import FuturesCostConfig
from ..market import KBar


@dataclass(frozen=True)
class HistoricalEventRun:
    """Result of replaying historical strategy signals through the event pipeline."""

    trades: tuple[dict[str, object], ...]
    execution_events: tuple[dict[str, object], ...]
    event_counts: dict[str, int]


def _timestamp(value: object) -> datetime:
    result = pd.Timestamp(str(value)).to_pydatetime()
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("historical signal time must be timezone-aware")
    return result


def _bar_event(bar: KBar, timeframe: str) -> BarClosedEvent:
    return BarClosedEvent(
        meta=EventMetadata.create(
            kind="bar_closed",
            occurred_at=bar.time,
            source="historical_bars",
            source_key=f"{bar.contract}:{timeframe}:{bar.time.isoformat()}",
        ),
        symbol=bar.symbol,
        contract=bar.contract,
        timeframe=timeframe,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        session=bar.session,
        trading_date=bar.trading_date,
    )


def _signal_event(
    signal: Mapping[str, object],
    *,
    index: int,
    owner_id: str,
    strategy_id: str,
    strategy_version: int,
) -> SignalEvent:
    occurred_at = _timestamp(signal["time"])
    action = "enter" if signal["event"] == "entry" else "exit"
    metadata = EventMetadata.create(
        kind="signal",
        occurred_at=occurred_at,
        source="historical_strategy",
        source_key=(
            f"{owner_id}:{strategy_id}:{strategy_version}:{index}:"
            f"{action}:{signal['contract']}:{occurred_at.isoformat()}"
        ),
        owner_id=owner_id,
    )
    trading_date = signal.get("trading_date")
    return SignalEvent(
        meta=metadata,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        symbol=str(signal.get("symbol") or "TMF"),
        contract=str(signal["contract"]),
        direction=str(signal["direction"]),  # type: ignore[arg-type]
        action=action,
        reference_price=float(signal["price"]),
        reason=str(signal["reason"]),
        stop_loss_price=(
            float(signal["stop_loss_price"])
            if signal.get("stop_loss_price") is not None
            else None
        ),
        trading_date=(
            pd.Timestamp(str(trading_date)).date() if trading_date is not None else None
        ),
    )


def _excursion(
    bars: Sequence[KBar],
    *,
    contract: str,
    entry_time: datetime,
    exit_time: datetime,
    entry_price: float,
    direction: str,
    multiplier: float,
    quantity: int,
) -> tuple[float, float]:
    favorable = 0.0
    adverse = 0.0
    for bar in bars:
        if bar.contract != contract or not entry_time <= bar.time <= exit_time:
            continue
        favorable = max(
            favorable,
            bar.high - entry_price if direction == "long" else entry_price - bar.low,
        )
        adverse = min(
            adverse,
            bar.low - entry_price if direction == "long" else entry_price - bar.high,
        )
    scale = multiplier * quantity
    return favorable * scale, adverse * scale


def run_historical_events(
    bars: Sequence[KBar],
    signals: Sequence[Mapping[str, object]],
    *,
    strategy_id: str,
    strategy_version: int = 1,
    owner_id: str = "historical-backtest",
    quantity: int = 1,
    initial_capital: float = 100_000.0,
    costs: FuturesCostConfig | None = None,
    timeframe: str = "1m",
) -> HistoricalEventRun:
    """Execute Backtest/Replay signals through the production-shaped event chain.

    Strategy calculations remain the source of entry, exit and risk prices. This
    adapter replaces the legacy direct signal pairing with typed Signal, Order,
    Risk, Fill and Position events. ``signal_price`` is intentionally restricted
    to deterministic historical execution; Paper Trading continues to use a
    future market bar and account-level risk gate.
    """

    closed = sorted(
        (bar for bar in bars if bar.status == "closed"), key=lambda item: item.time
    )
    resolved_costs = costs or FuturesCostConfig()
    engine = DeterministicEventEngine()
    pipeline = SimulatedExecutionPipeline(
        costs=resolved_costs,
        default_quantity=quantity,
        execution_timing="signal_price",
        allow_signal_price_execution=True,
        risk_gate=ResearchRiskGate(),
    )
    pipeline.install(engine)

    signals_by_time: dict[
        datetime, list[tuple[int, Mapping[str, object]]]
    ] = defaultdict(list)
    for index, signal in enumerate(signals):
        signals_by_time[_timestamp(signal["time"])].append((index, signal))

    consumed: set[int] = set()
    for bar in closed:
        engine.publish(_bar_event(bar, timeframe))
        engine.run()
        for index, signal in signals_by_time.get(bar.time, []):
            engine.publish(
                _signal_event(
                    signal,
                    index=index,
                    owner_id=owner_id,
                    strategy_id=strategy_id,
                    strategy_version=strategy_version,
                )
            )
            engine.run()
            consumed.add(index)
    if len(consumed) != len(signals):
        raise ValueError("historical signals must align with a closed K bar")

    entries = [signal for signal in signals if signal["event"] == "entry"]
    exits = [signal for signal in signals if signal["event"] == "exit"]
    serialized: list[dict[str, object]] = []
    for index, trade in enumerate(pipeline.ledger.trades):
        entry = entries[index]
        exit_signal = exits[index] if index < len(exits) else {}
        mfe, mae = _excursion(
            closed,
            contract=trade.contract,
            entry_time=trade.entry_time,
            exit_time=trade.exit_time,
            entry_price=trade.entry_price,
            direction=trade.direction,
            multiplier=resolved_costs.multiplier,
            quantity=trade.quantity,
        )
        serialized.append(
            {
                "strategy": str(entry.get("strategy") or strategy_id),
                "contract": trade.contract,
                "session": str(entry.get("session") or ""),
                "trading_date": str(entry.get("trading_date") or trade.trading_date),
                "direction": trade.direction,
                "trigger_time": _timestamp(
                    entry.get("trigger_time") or entry["time"]
                ),
                "entry_time": trade.entry_time,
                "exit_time": trade.exit_time,
                "quantity": trade.quantity,
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "stop_loss_price": float(entry["stop_loss_price"]),
                "take_profit_price": float(entry["take_profit_price"]),
                "gross_pnl": trade.gross_pnl,
                "commission": trade.commission,
                "tax": trade.tax,
                "total_cost": trade.total_cost,
                "net_pnl": trade.net_pnl,
                "return_pct": trade.net_pnl / initial_capital * 100,
                "holding_minutes": max(
                    1.0, (trade.exit_time - trade.entry_time).total_seconds() / 60
                ),
                "mfe": mfe,
                "mae": mae,
                "exit_reason": trade.exit_reason,
                "entry_reason": str(
                    entry.get("trigger_reason") or entry.get("reason")
                    or "signal_confirmed"
                ),
                "entry_context": dict(entry.get("context") or {}),
                "exit_context": dict(exit_signal.get("context") or {}),
            }
        )

    execution_kinds = {"signal", "order_intent", "risk_decision", "fill", "position"}
    execution_events = tuple(
        event_to_dict(record.event)
        for record in engine.history()
        if record.event.kind in execution_kinds
    )
    counts = Counter(record.event.kind for record in engine.history())
    return HistoricalEventRun(
        trades=tuple(serialized),
        execution_events=execution_events,
        event_counts=dict(sorted(counts.items())),
    )

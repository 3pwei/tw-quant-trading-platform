import assert from "node:assert/strict";
import test from "node:test";

import {
  backtestChartTime,
  barsForTrade,
  scopeKeyForTrade,
  tradeFocusRange,
  tradesForScope,
  hasDiagnostics,
  type BacktestBar,
  type BacktestTrade,
} from "../app/backtest/trade-chart-model.ts";
import { prepareStrategySeries } from "../app/backtest/strategy-series.ts";
import type { StrategySeries } from "../app/backtest/trade-chart-model.ts";

const bars: BacktestBar[] = Array.from({ length: 60 }, (_, index) => ({
  timestamp: new Date(Date.UTC(2026, 8, 9, 7, index)).toISOString(),
  open: 100 + index,
  high: 101 + index,
  low: 99 + index,
  close: 100 + index,
  volume: 1,
  contract: "TMFU6",
  session: "night",
  trading_date: "2026-09-10",
}));

const trade = (entry: number, session = "night"): BacktestTrade => ({
  direction: "long",
  entry_time: bars[entry].timestamp,
  exit_time: bars[entry + 2].timestamp,
  entry_price: 100,
  exit_price: 102,
  net_pnl: 2,
  contract: "TMFU6",
  session,
  trading_date: "2026-09-10",
});

test("intraday charts share bars and trades only inside one session", () => {
  const selected = trade(20);
  const dayTrade = trade(30, "day");
  assert.equal(barsForTrade(bars, selected, "5m").length, 60);
  assert.deepEqual(tradesForScope([selected, trade(40), dayTrade], selected, "5m"), [selected, trade(40)]);
  assert.match(scopeKeyForTrade(selected, "5m"), /2026-09-10:night$/);
});

test("daily charts share a contract range without session grouping", () => {
  const first = trade(10, "night");
  const second = trade(30, "day");
  assert.deepEqual(tradesForScope([first, second], first, "1d"), [first, second]);
  assert.equal(barsForTrade(bars, first, "1d").length, 60);
});

test("daily chart time uses trading date instead of the previous night timestamp", () => {
  const nightBar = {
    ...bars[0],
    timestamp: "2026-09-09T15:00:00+08:00",
    trading_date: "2026-09-10",
  };
  assert.equal(
    backtestChartTime(nightBar, true),
    Date.parse("2026-09-10T00:00:00Z") / 1000,
  );
  assert.equal(
    backtestChartTime(nightBar, false),
    Date.parse(nightBar.timestamp) / 1000,
  );
});

test("trade focus keeps context and a useful minimum width", () => {
  assert.deepEqual(tradeFocusRange(bars, trade(20)), { from: 3, to: 38 });
  assert.deepEqual(tradeFocusRange(bars, trade(0)), { from: 0, to: 35 });
  assert.equal(tradeFocusRange([], trade(0)), null);
});

test("legacy chart payloads without diagnostics remain safe", () => {
  assert.equal(hasDiagnostics(undefined), false);
  assert.equal(hasDiagnostics({
    schema_version: 1,
    strategy: { key: "legacy", name: "Legacy" },
    parameters: [],
    panels: [],
    overlays: [],
    diagnostics: [],
  }), false);
});

test("generic diagnostic series are detected without strategy-specific branching", () => {
  assert.equal(hasDiagnostics({
    schema_version: 1,
    strategy: { key: "rsi_mean_reversion", name: "RSI" },
    parameters: [],
    panels: [{ key: "strategy", label: "策略診斷", order: 2 }],
    overlays: [],
    diagnostics: [{
      key: "rsi", label: "RSI", panel: "strategy", type: "line",
      points: [{ time: bars[0].timestamp, value: 28.4 }],
    }],
  }), true);
});

test("generic strategy preparation keeps grouped channel segments independent", () => {
  const definition: StrategySeries = {
    key: "dow_upper",
    label: "Upper",
    panel: "price",
    type: "line",
    points: [
      { time: bars[0].timestamp, value: 100, group: "A" },
      { time: bars[1].timestamp, value: 101, group: "A" },
      { time: bars[2].timestamp, value: 110, group: "B" },
      { time: bars[3].timestamp, value: 111, group: "B" },
    ],
  };

  const prepared = prepareStrategySeries(definition);

  assert.deepEqual(prepared.map(item => item.groupKey), ["A", "B"]);
  assert.deepEqual(prepared.map(item => item.points.length), [2, 2]);
});

test("threshold preparation follows full range and replay cursor endpoints", () => {
  const definition: StrategySeries = {
    key: "rsi_oversold",
    label: "Oversold",
    panel: "strategy",
    type: "threshold",
    points: [],
    metadata: { value: 30 },
  };

  const full = prepareStrategySeries(definition, {
    thresholdRange: { from: bars[0].timestamp, to: bars[3].timestamp },
  });
  const replay = prepareStrategySeries(definition, {
    thresholdRange: { from: bars[0].timestamp, to: bars[1].timestamp },
  });

  assert.deepEqual(full[0].points.map(point => point.time), [bars[0].timestamp, bars[3].timestamp]);
  assert.deepEqual(replay[0].points.map(point => point.time), [bars[0].timestamp, bars[1].timestamp]);
  assert.deepEqual(replay[0].points.map(point => point.value), [30, 30]);
});

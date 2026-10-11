import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { demoErrorMessage, DemoRequestError, loadDemoCases, prepareDemoResult, runDemoCase } from "../app/demo/demo-client.ts";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
const demoPayload = {
  case_id: "synthetic-ma-day", synthetic_data: true, simulated_trades: true, performance_claim: false,
  date_range: "2026-07-06 ～ 2026-07-08",
  config: { initial_capital: 100000, commission_per_side: 20, slippage_points: 1, contract_multiplier: 10 },
  summary: { net_profit: 300, return_pct: .3, ending_equity: 100300, max_drawdown: 100, max_drawdown_pct: .1, win_rate_pct: 100, profit_factor: null, daily_sharpe: null, total_cost: 40 },
  equity: [{ timestamp: "2026-07-06T09:00:00+08:00", equity: 100000, net_pnl: 0, peak: 100000, drawdown: 0, drawdown_pct: 0 }],
  bars: [{ timestamp: "2026-07-06T09:00:00+08:00", trading_date: "2026-07-06", open: 1, high: 2, low: 1, close: 2, volume: 4 }],
  trades: [{ entry_time: "2026-07-06T09:01:00+08:00", exit_time: "2026-07-06T09:02:00+08:00", entry_price: 1, exit_price: 2, direction: "long", net_pnl: 300, total_cost: 40, holding_minutes: 1, mfe: 400, mae: -50 }],
  visualization: { strategy: { key: "public-demo", name: "展示策略" }, parameters: [], overlays: [], diagnostics: [] },
};

test("Demo POST sends only case_id to the isolated endpoint", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = async (input, init) => {
    calls.push({ url: String(input), init });
    return new Response(JSON.stringify(demoPayload), { status: 200 });
  };
  const result = await runDemoCase("synthetic-ma-day");
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /\/api\/demo\/backtests$/);
  assert.equal(calls[0].init?.method, "POST");
  assert.equal(calls[0].init?.cache, "no-store");
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { case_id: "synthetic-ma-day" });
  assert.deepEqual(result.visualization.parameters, []);
});

test("Demo response retains chart markers while private details stay redacted", () => {
  const result = prepareDemoResult({ ...demoPayload, case_id: "a" }, "a");
  assert.equal(result.trades[0].trade_index, 0);
  assert.deepEqual(result.visualization.diagnostics, []);
  assert.deepEqual(result.visualization.parameters, []);
  assert.equal(result.bars.length, 1);
  assert.equal(result.summary.net_profit, 300);
  assert.equal(result.trades[0].net_pnl, 300);
  assert.equal(result.bars[0].trading_date, "2026-07-06");
  assert.throws(() => prepareDemoResult({ ...result, performance_claim: true }, "a"), DemoRequestError);
  assert.throws(() => prepareDemoResult({ ...result, equity: null }, "a"), DemoRequestError);
});

test("Demo cases and rate limit have safe UI messages", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ cases: [{ id: "a", label: "合成", synthetic_data: true, simulated_trades: true }] }), { status: 200 });
  assert.equal((await loadDemoCases())[0].id, "a");
  globalThis.fetch = async () => new Response("", { status: 429 });
  await assert.rejects(runDemoCase("a"), { kind: "limited" });
  assert.match(demoErrorMessage(new DemoRequestError("limited")), /頻繁/);
});

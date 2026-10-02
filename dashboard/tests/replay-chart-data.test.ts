import assert from "node:assert/strict";
import test from "node:test";
import { buildReplayChartFrame, toReplayChartTime } from "../app/replay/replay-chart-data.ts";
import type { ReplaySnapshot, ReplayTradingState } from "../app/replay/types.ts";

const times = ["2026-09-01T00:00:00Z", "2026-09-01T00:01:00Z", "2026-09-01T00:02:00Z"];
const bars = times.map((time, index) => ({
  time,
  end_time: new Date(Date.parse(time) + 60_000).toISOString(),
  open: 20_000 + index,
  high: 20_010 + index,
  low: 19_990 + index,
  close: 20_005 + index,
  volume: 10 + index,
  contract: "TMFA6",
  session: "night" as const,
  trading_date: "2026-09-01",
  no_trade: false,
}));
const trading = {
  session_id: "session",
  snapshot_id: "snapshot",
  mode: "replay",
  isolated_from_live_paper: true,
  cursor: 1,
  bar_count: 3,
  virtual_time: times[1],
  rewound: false,
  account: { realized_pnl: 0, open_contracts: 1, trades: 1 },
  positions: [],
  orders: [],
  fills: [{
    fill_id: "fill",
    contract: "TMFA6",
    side: "buy",
    quantity: 1,
    price: 20_006,
    commission: 1,
    tax: 0,
    slippage: 1,
    purpose: "entry",
    meta: { occurred_at: "2026-09-01T00:00:30Z" },
  }],
} satisfies ReplayTradingState;
const snapshot = {
  snapshot_id: "snapshot",
  created_at: times[0],
  symbol: "TMF",
  trading_date: "2026-09-01",
  session: "night",
  interval: "1m",
  interval_name: "1 分 K",
  bars,
  strategies: [{
    key: "orb",
    name: "ORB",
    color: "#fff",
    signals: [{
      strategy: "orb",
      event: "entry",
      direction: "long",
      time: "2026-09-01T00:01:30Z",
      price: 20_007,
      stop_loss_price: 19_950,
      take_profit_price: 20_100,
      reason: "test",
    }],
  }],
  trading_session: trading,
} satisfies ReplaySnapshot;

test("cursor exposes only bars and events that have occurred", () => {
  const first = buildReplayChartFrame(snapshot, trading, 0);
  assert.equal(first.candles.length, 1);
  assert.equal(first.markers.length, 1);
  assert.match(first.markers[0].text ?? "", /^REPLAY/);

  const second = buildReplayChartFrame(snapshot, trading, 1);
  assert.equal(second.candles.length, 2);
  assert.equal(second.markers.length, 2);
  assert.equal(second.markers[0].time, toReplayChartTime(times[0]));
  assert.equal(second.markers[1].time, toReplayChartTime(times[1]));

  const formatted = buildReplayChartFrame(snapshot, trading, 1, value => `P${value}`);
  assert.match(formatted.markers[0].text ?? "", /P20006$/);
  assert.match(formatted.markers[1].text ?? "", /P20007$/);
});

test("cursor is clamped to the snapshot bounds", () => {
  assert.equal(buildReplayChartFrame(snapshot, trading, 99).candles.length, 3);
  assert.equal(buildReplayChartFrame(snapshot, trading, -10).candles.length, 1);
});

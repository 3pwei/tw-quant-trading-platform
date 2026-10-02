import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import type { Account, CurrentUser, MarketHealth } from "../app/paper/types.ts";
import { automationState, canUsePaperAuto, paperFillAppearance, runtimeActions } from "../app/trade/paper-auto-policy.ts";
import type { TradingRuntime } from "../app/trade/paper-auto-types.ts";

const trader: CurrentUser = {
  role: "trader",
  trading_mode: "paper",
  permissions: ["strategy.read.own", "orders.paper"],
};
const account = {
  kill_switch_active: false,
  recovery_status: "healthy",
} as Account;
const market = {
  service_status: "healthy",
  trading_block_reason: null,
} as MarketHealth;
const runtime = {
  runtime_id: "runtime-1",
  status: "paused",
  recovery_issue: null,
} as TradingRuntime;

test("Observe remains available without paper-order permission", () => {
  assert.equal(canUsePaperAuto({ ...trader, permissions: ["strategy.read.own"] }), false);
  assert.equal(automationState(null, null, null).label, "OBSERVE");
});

test("armed, paused, recovery, market and kill-switch states are explicit", () => {
  assert.equal(automationState({ ...runtime, status: "armed" }, account, market).permitsEntry, true);
  assert.equal(automationState(runtime, account, market).label, "PAUSED");
  assert.equal(automationState({ ...runtime, status: "recovery_locked" }, account, market).label, "RECOVERY LOCK");
  assert.equal(automationState(runtime, { ...account, kill_switch_active: true }, market).label, "KILL SWITCH");
  assert.equal(automationState(runtime, account, { ...market, service_status: "market_stale", trading_block_reason: "market_stale" }).label, "MARKET STALE");
});

test("controls expose pause for armed and resume for paused without a live action", () => {
  assert.deepEqual(runtimeActions({ ...runtime, status: "armed" }, trader), ["pause", "stop"]);
  assert.deepEqual(runtimeActions(runtime, trader), ["arm", "stop"]);
  assert.equal(runtimeActions(runtime, trader).includes("live" as never), false);
});

test("auto and manual chart fills have distinct labels and shapes", () => {
  const manual = paperFillAppearance({ order_source: "manual", purpose: "entry", side: "buy" });
  const autoEntry = paperFillAppearance({ order_source: "strategy_auto", purpose: "entry", side: "buy" });
  const autoExit = paperFillAppearance({ order_source: "strategy_auto", purpose: "exit", side: "sell" });
  assert.equal(manual.prefix, "MANUAL");
  assert.equal(autoEntry.prefix, "AUTO");
  assert.notEqual(manual.color, autoEntry.color);
  assert.equal(autoExit.shape, "square");
});

test("the shared trade workspace exposes safe mobile controls without a live mode", () => {
  const workspace = readFileSync(new URL("../app/paper/paper-trading-dashboard.tsx", import.meta.url), "utf8");
  const styles = readFileSync(new URL("../app/globals.css", import.meta.url), "utf8");
  assert.match(workspace, /OBSERVE/);
  assert.match(workspace, /MANUAL PAPER/);
  assert.match(workspace, /PAPER AUTO/);
  assert.match(workspace, /手動緊急平倉/);
  assert.doesNotMatch(workspace, /workspaceMode === "live"/);
  assert.match(styles, /paper-auto-controls\{position:sticky/);
});

import assert from "node:assert/strict";
import test from "node:test";
import { loadPaperAccountData } from "../app/paper/paper-account-loader.ts";
import type { Account, CurrentUser, PaperPosition } from "../app/paper/types.ts";

const researcher: CurrentUser = {
  role: "researcher",
  trading_mode: "disabled",
  permissions: [],
};

test("a user without position access does not request account ledgers", async () => {
  const calls: string[] = [];
  const data = await loadPaperAccountData(async <T>(path: string) => {
    calls.push(path);
    return researcher as T;
  });
  assert.deepEqual(calls, ["/api/me"]);
  assert.deepEqual(data, {
    user: researcher,
    account: null,
    positions: [],
    orders: [],
    fills: [],
  });
});

test("an authorized poll returns one consistent overlay snapshot", async () => {
  const user: CurrentUser = {
    role: "trader",
    trading_mode: "paper",
    permissions: ["positions.read.own"],
  };
  const account = {
    realized_pnl: 0,
    open_contracts: 1,
    reserved_contracts: 0,
    trades: 1,
    kill_switch_active: false,
    kill_switch_reason: null,
    cooldown_until: null,
    recovery_status: "healthy",
    recovery_issues: [],
    risk_limits: {
      max_position_contracts: 2,
      max_risk_per_trade: 10_000,
      max_daily_loss: 50_000,
      max_trades_per_day: 20,
      max_consecutive_losses: 3,
      cooldown_minutes: 30,
    },
  } satisfies Account;
  const position = {
    strategy_id: "manual",
    strategy_version: 1,
    symbol: "TMF",
    contract: "TMFA6",
    quantity: 1,
    average_price: 20_000,
    opened_at: null,
    realized_pnl: 0,
    unrealized_pnl: 10,
    total_cost: 1,
  } satisfies PaperPosition;
  const responses: Record<string, unknown> = {
    "/api/me": user,
    "/api/paper/account": { account, positions: [position] },
    "/api/paper/orders": { orders: [] },
    "/api/paper/fills?limit=100": { fills: [] },
  };
  const calls: string[] = [];
  const data = await loadPaperAccountData(async <T>(path: string) => {
    calls.push(path);
    return responses[path] as T;
  });
  assert.deepEqual(new Set(calls), new Set(Object.keys(responses)));
  assert.equal(data.account, account);
  assert.deepEqual(data.positions, [position]);
});

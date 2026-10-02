import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const panel = readFileSync(
  new URL("../app/trade/live-shadow-panel.tsx", import.meta.url), "utf8",
);

test("Live Shadow is visible but has no live trading actions", () => {
  assert.match(panel, /Live Shadow/);
  assert.match(panel, /Would Submit/);
  assert.match(panel, /masked_account_id/);
  assert.doesNotMatch(panel, /broker_name|Shioaji|永豐/i);
  assert.match(panel, /沒有 Broker dispatch/);
  assert.doesNotMatch(panel, /ARM LIVE|BUY LIVE|SELL LIVE|CANCEL LIVE|FLATTEN LIVE/);
  assert.doesNotMatch(panel, /POST|DELETE|PUT/);
});

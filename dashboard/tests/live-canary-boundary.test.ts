import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const panel = readFileSync(new URL("../app/trade/live-canary-panel.tsx", import.meta.url), "utf8");

test("manual live canary UI requires separate ARM and explicit real-order confirmation", () => {
  assert.match(panel, /I_UNDERSTAND_MANUAL_LIVE_CANARY/);
  assert.match(panel, /ARM LIVE CANARY/);
  assert.match(panel, /REAL ORDER/);
  assert.match(panel, /Idempotency-Key/);
  assert.match(panel, /REDUCE ONLY/);
});

test("manual live canary UI has no strategy auto and flatten is guardian-only", () => {
  assert.doesNotMatch(panel, /LIVE AUTO/);
  assert.doesNotMatch(panel, /FLATTEN ALL/);
  assert.match(panel, /EMERGENCY FLATTEN · REDUCE ONLY/);
  assert.match(panel, /POSITION PROTECTION IS PLATFORM-MANAGED/);
  assert.match(panel, /DO NOT RETRY/);
  assert.doesNotMatch(panel, /broker_name|Shioaji|永豐/i);
});

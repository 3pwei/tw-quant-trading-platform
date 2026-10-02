import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const sources = [
  "../app/settings/page.tsx",
  "../app/settings/system-health.tsx",
  "../app/trade/live-auto-panel.tsx",
  "../app/trade/live-canary-panel.tsx",
  "../app/trade/live-shadow-panel.tsx",
  "../app/live/live-dashboard.tsx",
].map(path => readFileSync(new URL(path, import.meta.url), "utf8")).join("\n");

test("user-facing provider identity is vendor neutral", () => {
  assert.doesNotMatch(sources, /Shioaji|永豐|broker_name/i);
  assert.match(sources, /Execution Provider/);
});

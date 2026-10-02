import assert from "node:assert/strict";
import test from "node:test";
import { createInitialTradeSelection } from "../app/live/trade-selection.ts";

test("the trade workspace starts without selecting strategies", () => {
  const selection = createInitialTradeSelection();

  assert.equal(selection.symbol, "TMF");
  assert.equal(selection.interval, "1m");
  assert.deepEqual(selection.strategies, []);
});

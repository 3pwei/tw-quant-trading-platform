import assert from "node:assert/strict";
import test from "node:test";
import {
  closeStrategyMenuWhenOutside,
  type StrategyMenuElement,
} from "../app/live/strategy-menu.ts";

function menu(open: boolean, insideTarget: object): StrategyMenuElement {
  return {
    open,
    contains: (target: Node | null) => target === insideTarget,
  } as StrategyMenuElement;
}

test("clicking outside closes an open strategy menu", () => {
  const insideTarget = {};
  const element = menu(true, insideTarget);

  assert.equal(closeStrategyMenuWhenOutside(element, {} as EventTarget), true);
  assert.equal(element.open, false);
});

test("clicking inside keeps the strategy menu open for multi-select", () => {
  const insideTarget = {};
  const element = menu(true, insideTarget);

  assert.equal(
    closeStrategyMenuWhenOutside(element, insideTarget as EventTarget),
    false,
  );
  assert.equal(element.open, true);
});

test("outside clicks do not mutate an already closed menu", () => {
  const element = menu(false, {});

  assert.equal(closeStrategyMenuWhenOutside(element, {} as EventTarget), false);
  assert.equal(element.open, false);
});

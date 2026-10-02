import assert from "node:assert/strict";
import test from "node:test";
import { historyPageSlots } from "../app/history/history-pagination.ts";

const labels = (page: number, total: number) => historyPageSlots(page, total).map(slot =>
  slot.kind === "page" ? slot.number :
    slot.kind === "previous" ? "<" : slot.kind === "next" ? ">" :
      slot.kind === "jump" ? "Enter" : "",
);

test("nine equal slots show the requested first, middle and last page layouts", () => {
  assert.deepEqual(labels(1, 100), ["", 1, 2, 3, 4, ">", 100, "Enter", ""]);
  assert.deepEqual(labels(51, 100), [1, "<", 50, 51, 52, 53, ">", 100, "Enter"]);
  assert.deepEqual(labels(100, 100), ["", 1, "<", 97, 98, 99, 100, "Enter", ""]);
});

test("short histories and transitions never repeat page numbers", () => {
  for (const total of [1, 2, 4, 5, 8, 100]) {
    for (let page = 1; page <= total; page++) {
      const slots = historyPageSlots(page, total);
      const pages = slots.filter(slot => slot.kind === "page").map(slot => slot.number);
      assert.equal(slots.length, 9);
      assert.equal(new Set(pages).size, pages.length);
      assert.ok(pages.includes(page));
      assert.equal(slots.filter(slot => slot.kind === "jump").length, 1);
    }
  }
});

test("few pages keep the Enter jump directly beside the last page and center the group", () => {
  assert.deepEqual(labels(1, 1), ["", "", "", 1, "Enter", "", "", "", ""]);
  assert.deepEqual(labels(2, 3), ["", "", 1, 2, 3, "Enter", "", "", ""]);
  assert.deepEqual(labels(4, 4), ["", "", 1, 2, 3, 4, "Enter", "", ""]);
});

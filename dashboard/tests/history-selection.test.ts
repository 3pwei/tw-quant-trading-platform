import assert from "node:assert/strict";
import test from "node:test";
import {
  batchDeletionPayload,
  togglePageSelection,
  toggleRunSelection,
} from "../app/history/history-selection.ts";

test("individual history selection toggles without duplicating ids", () => {
  assert.deepEqual(toggleRunSelection([], "run-1"), ["run-1"]);
  assert.deepEqual(toggleRunSelection(["run-1"], "run-1"), []);
});

test("batch deletion uses explicit ids for an ordinary selection", () => {
  assert.deepEqual(
    batchDeletionPayload(["run-1", "run-2"]),
    { run_ids: ["run-1", "run-2"] },
  );
});

test("select all covers only the current page and allows individual cancellation", () => {
  const page = ["run-1", "run-2"];
  const selected = togglePageSelection([], page);
  assert.deepEqual(selected, page);
  assert.deepEqual(toggleRunSelection(selected, "run-2"), ["run-1"]);
  assert.deepEqual(togglePageSelection(["run-1"], page), page);
  assert.deepEqual(togglePageSelection(page, page), []);
});

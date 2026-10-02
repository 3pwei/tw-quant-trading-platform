import assert from "node:assert/strict";
import test from "node:test";
import { initialStrategyKeys, parameterDraft, parseParameterDraft, type PublicStrategy } from "../app/lib/strategy-catalog.ts";

test("initial selections follow server catalog order and never assume a concrete key", () => {
  assert.deepEqual(initialStrategyKeys([{ key: "server-beta" }, { key: "server-alpha" }]), ["server-beta"]);
  assert.deepEqual(initialStrategyKeys([{ key: "server-alpha" }, { key: "server-beta" }]), ["server-alpha"]);
});
test("unavailable or empty catalog has no fallback selection", () => {
  assert.deepEqual(initialStrategyKeys([]), []);
  assert.deepEqual(initialStrategyKeys([{ key: "" }]), []);
});
test("requested selection count is bounded and duplicates are removed", () => {
  assert.deepEqual(initialStrategyKeys([{ key: "a" }, { key: "a" }, { key: "b" }], 2), ["a", "b"]);
  assert.deepEqual(initialStrategyKeys([{ key: "a" }], 0), []);
});

const generic: PublicStrategy = { key: "inert", name: "test", parameters: { count: 0, enabled: false, label: "" }, parameter_schema: { schema_version: "1", fields: [{ name: "count", kind: "integer", required: true }, { name: "enabled", kind: "boolean", required: true }, { name: "label", kind: "string", required: false }, { name: "ratio", kind: "number", required: false }] } };
test("Core schema preserves zero/false and does not invent absent defaults", () => { assert.deepEqual(parameterDraft(generic), { count: "0", enabled: "false", label: "", ratio: "" }); assert.deepEqual(parseParameterDraft(generic, { count: "0", enabled: "false", label: "hello" }), { count: 0, enabled: false, label: "hello" }); });
test("invalid, missing and unsupported parameter types fail before submission", () => { for (const value of ["", "NaN", "Infinity", "1.5"]) assert.throws(() => parseParameterDraft(generic, { count: value, enabled: "true" })); assert.throws(() => parseParameterDraft(generic, { count: "1", enabled: "1" })); });

import assert from "node:assert/strict";
import { ESLint } from "eslint";

// Parse synthetic strings only: never execute or write vulnerable source files.
const scanner = new ESLint({ allowInlineConfig: false, overrideConfigFile: "eslint.security.config.mjs" });
const cases = [
  ["no-eval", "eval(input);"],
  ["no-implied-eval", "setTimeout('payload()', 10);"],
  ["no-new-func", "new Function(input);"],
  ["no-script-url", "const target = 'javascript:payload()'; void target;"],
  ["no-proto", "const value = item.__proto__; void value;"],
  ["no-extend-native", "Array.prototype.extra = function () {};"],
];
for (const [rule, source] of cases) {
  const [result] = await scanner.lintText(source, {
    filePath: "src/security-gate-probe.ts",
  });
  assert.ok(result.messages.some((message) => message.ruleId === rule && message.severity === 2), rule);
}
const [suppressed] = await scanner.lintText("/* eslint-disable no-eval */\neval(input);", {
  filePath: "src/security-gate-probe.ts",
});
assert.ok(suppressed.messages.some((message) => message.ruleId === "no-eval" && message.severity === 2));
const [safe] = await scanner.lintText("export const value = 1;", {
  filePath: "src/security-gate-probe.ts",
});
assert.equal(safe.errorCount + safe.warningCount, 0);
console.log("JS/TS SAST gate regression: six blocking rules and inline-suppression rejection passed");

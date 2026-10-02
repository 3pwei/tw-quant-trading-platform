import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
import securityRules from "./eslint-security-rules.mjs";

export default defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    rules: securityRules,
  },
  globalIgnores([".next/**", "out/**", "build/**"]),
]);

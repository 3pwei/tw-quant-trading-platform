import { defineConfig, globalIgnores } from "eslint/config";
import nextTs from "eslint-config-next/typescript";
import securityRules from "./eslint-security-rules.mjs";

export default defineConfig([
  {
    files: ["**/*.{js,mjs,cjs,ts,tsx}"],
    languageOptions: {
      ...nextTs.find((config) => config.languageOptions?.parser).languageOptions,
      globals: {
        setTimeout: "readonly",
        setInterval: "readonly",
        execScript: "readonly",
        window: "readonly",
        global: "readonly",
      },
    },
    rules: securityRules,
  },
  globalIgnores([".next/**", "out/**", "build/**"]),
]);

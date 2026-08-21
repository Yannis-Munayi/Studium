import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({ baseDirectory: dirname(fileURLToPath(import.meta.url)) });

/**
 * Lint configuration (spec §17's CI wiring).
 *
 * `next/core-web-vitals` plus the TypeScript rules. The additions below are the
 * ones this codebase specifically needs: streaming code is full of array
 * indexing, and `noUncheckedIndexedAccess` in tsconfig only helps if nobody
 * reaches for a non-null assertion to silence it.
 */
const config = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    ignores: [".next/**", "node_modules/**", "e2e/**", "next-env.d.ts"],
  },
  {
    rules: {
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      // §4: "No `any` types in application code."
      "@typescript-eslint/no-explicit-any": "error",
      "no-console": ["warn", { allow: ["warn", "error", "debug"] }],
    },
  },
  {
    // Tests index into fixtures constantly and assert on the result; requiring
    // a guard for each one would bury the assertion.
    files: ["tests/**/*.{ts,tsx}"],
    rules: {
      "@typescript-eslint/no-non-null-assertion": "off",
      "@typescript-eslint/no-explicit-any": "off",
    },
  },
];

export default config;

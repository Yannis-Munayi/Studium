import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

/**
 * Tier 1 — offline (spec §17).
 *
 * No network, no database, no API key. Everything here runs on any machine with
 * `npm ci`, which is what makes it the gate on every push.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, ".") },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}"],
    // e2e is Playwright's; running it here would start a browser inside jsdom.
    exclude: ["node_modules", ".next", "e2e"],
    coverage: {
      provider: "v8",
      include: ["lib/**/*.ts", "components/**/*.tsx"],
      reporter: ["text-summary", "json-summary"],
    },
  },
});

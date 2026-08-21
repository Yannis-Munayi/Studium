import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tiers (spec §17).
 *
 * Tier 2 runs against a mocked-LLM backend started inside the test process
 * (`e2e/mock-backend.ts`): a real server, real SSE over a real socket, no model
 * calls and no Postgres. Tier 3 runs against the real FastAPI app and spends
 * real money, so it never runs in CI and is gated on an explicit environment
 * variable.
 *
 * Both drive a production Next build rather than the dev server. The dev server
 * has different streaming behaviour, different error boundaries, and React's
 * development double-render -- so a green dev-server run is not evidence about
 * what a learner gets.
 */

const PORT = Number(process.env["PLAYWRIGHT_PORT"] ?? 3100);
const BASE_URL = `http://127.0.0.1:${PORT}`;
const MOCK_PORT = Number(process.env["STUDIUM_MOCK_PORT"] ?? 8099);

export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: false,
  forbidOnly: !!process.env["CI"],
  retries: process.env["CI"] ? 1 : 0,
  workers: 1,
  reporter: process.env["CI"] ? [["github"], ["list"]] : [["list"]],

  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },

  projects: [
    {
      name: "tier2",
      testMatch: /tier2\..*\.spec\.ts/,
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "tier3",
      testMatch: /tier3\..*\.spec\.ts/,
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  webServer: {
    // `next start` needs a build; the CI job runs one first. Locally,
    // `npm run build && npm run test:e2e`.
    command: `npx next start --port ${PORT}`,
    url: BASE_URL,
    reuseExistingServer: !process.env["CI"],
    timeout: 120_000,
    env: {
      // Tier 2 points at the mock that `globalSetup` has already bound to
      // MOCK_PORT; Tier 3 points at the real FastAPI app. The proxy reads this
      // at module load, which is why the mock's port is fixed rather than
      // assigned by the OS.
      STUDIUM_BACKEND_ORIGIN:
        process.env["STUDIUM_RUN_PAID_TESTS"] === "1"
          ? (process.env["STUDIUM_BACKEND_ORIGIN"] ?? "http://127.0.0.1:8000")
          : `http://127.0.0.1:${MOCK_PORT}`,
      // The sign-in placeholder refuses to run in a production build without
      // this. Setting it here is a statement that these are tests, not a
      // deployment -- see the header of `lib/auth.ts`.
      STUDIUM_ALLOW_UNAUTHENTICATED: "1",
      NODE_ENV: "production",
    },
  },
});

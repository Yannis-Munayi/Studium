import { test as base, expect, type APIRequestContext, type Page } from "@playwright/test";
import { MOCK_ORIGIN } from "./global-setup";

/**
 * Playwright fixtures (spec §17, "Fixtures").
 *
 * §17 asks for "an authenticated user, a seeded lambda calculus subject, and a
 * session in each mode", with fixtures resetting between tests. The learner is
 * a cookie (see `lib/auth.ts` for why that is all it can be today); the subject
 * and sessions come from the mock backend, which resets per test.
 */

export const TEST_LEARNER_ID = "0f8fad5b-d9cb-469f-a165-70867728950e";

export interface MockControl {
  configure: (options: Record<string, unknown>) => Promise<void>;
  reset: () => Promise<void>;
  state: () => Promise<{ interrupts: string[]; turns: Array<{ text: string; primitive: string | null }> }>;
}

interface Fixtures {
  /** A page that already carries the learner cookie. */
  learnerPage: Page;
  mock: MockControl;
}

export const test = base.extend<Fixtures>({
  mock: async ({ request }, use) => {
    const control = makeControl(request);
    await control.reset();
    await use(control);
    await control.reset();
  },

  learnerPage: async ({ page, context }, use) => {
    await context.addCookies([
      {
        name: "studium-learner",
        value: TEST_LEARNER_ID,
        domain: "127.0.0.1",
        path: "/",
      },
    ]);
    await use(page);
  },
});

function makeControl(request: APIRequestContext): MockControl {
  return {
    configure: async (options) => {
      const response = await request.post(`${MOCK_ORIGIN}/__control`, { data: options });
      expect(response.ok()).toBeTruthy();
    },
    reset: async () => {
      const response = await request.post(`${MOCK_ORIGIN}/__control/reset`);
      expect(response.ok()).toBeTruthy();
    },
    state: async () => {
      const response = await request.get(`${MOCK_ORIGIN}/__state`);
      expect(response.ok()).toBeTruthy();
      return response.json();
    },
  };
}

export { expect };

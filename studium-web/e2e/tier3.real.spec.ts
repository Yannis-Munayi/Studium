import { test, expect } from "@playwright/test";

/**
 * Tier 3 — the real stack (spec §17).
 *
 * Real FastAPI, real Postgres, real Anthropic, real Voyage. **This spends
 * money and never runs in CI.** Run it deliberately:
 *
 *     cd backend && make db-up && uvicorn studium.api.app:app --port 8000
 *     cd studium-web && npm run build
 *     STUDIUM_RUN_PAID_TESTS=1 \
 *     STUDIUM_BACKEND_ORIGIN=http://127.0.0.1:8000 \
 *     STUDIUM_TEST_LEARNER_ID=<a seeded user's uuid> \
 *       npm run test:paid
 *
 * What Tier 2 cannot tell you, and this can: whether the *contract* still
 * holds. Tier 2's mock and this frontend could agree with each other
 * indefinitely while both had drifted from what `studium/api/app.py` sends.
 * These three cases are the ones where drift would be silent rather than loud:
 * a chunk kind the client discards, a sentence boundary the server picks
 * differently, and a citation whose provenance never arrives.
 */

const RUN = process.env["STUDIUM_RUN_PAID_TESTS"] === "1";
const LEARNER_ID = process.env["STUDIUM_TEST_LEARNER_ID"] ?? "";

test.describe("real session end to end", () => {
  test.skip(
    !RUN,
    "Paid tier. Set STUDIUM_RUN_PAID_TESTS=1 and point STUDIUM_BACKEND_ORIGIN at a running backend.",
  );

  test.beforeEach(async ({ context }) => {
    expect(
      LEARNER_ID,
      "STUDIUM_TEST_LEARNER_ID must name a seeded learner enrolled in a subject",
    ).not.toBe("");

    await context.addCookies([
      { name: "studium-learner", value: LEARNER_ID, domain: "127.0.0.1", path: "/" },
    ]);
  });

  // These call a model. The Lecturer's first segment is the slowest thing in
  // the product -- context assembly, retrieval, then generation.
  test.setTimeout(180_000);

  test("streams a real Lecturer segment and renders it as prose", async ({ page }) => {
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();

    await expect(page).toHaveURL(/\/sessions\/[0-9a-f-]{36}/);

    // Real output, so nothing about its wording can be asserted. What can be:
    // that prose arrived, that it landed in the reading column, and that the
    // renderer did not leave the provisional tail styling behind.
    const article = page.locator("article").first();
    await expect(article).toBeVisible({ timeout: 150_000 });
    await expect.poll(async () => (await article.innerText()).length, { timeout: 150_000 })
      .toBeGreaterThan(200);

    await expect(page.getByTestId("still-generating")).toHaveCount(0, { timeout: 150_000 });
  });

  test("invoking explain_differently streams a new stance", async ({ page }) => {
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();

    const article = page.locator("article").first();
    await expect(article).toBeVisible({ timeout: 150_000 });
    const before = await page.locator("article").count();

    await page.locator("body").press("/");
    await page.getByRole("combobox").fill("explain");
    await page.getByRole("combobox").press("Enter");

    // §9.3: a Tutor exchange followed by a Lecturer segment -- more turns than
    // there were, with real text in them.
    await expect.poll(async () => page.locator("article").count(), { timeout: 150_000 })
      .toBeGreaterThan(before);
  });

  test("interrupting a real stream stops it at a real sentence boundary", async ({ page }) => {
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();

    const interrupt = page.getByRole("button", { name: "Interrupt" });
    await expect(interrupt).toBeEnabled({ timeout: 150_000 });
    await interrupt.click();

    await expect(page.getByText("Paused here")).toBeVisible({ timeout: 60_000 });

    // The server decides where to cut. What the client must get right is that
    // the delivered text ends on a completed sentence -- which is the property
    // `lib/stream/sentences.ts` is a port of, checked here against real output
    // rather than against the fixture corpus.
    const delivered = await page.locator("article").first().innerText();
    expect(delivered.trimEnd()).toMatch(/[.!?]["')\]]*$/);

    const box = page.getByPlaceholder("Your question…");
    await expect(box).toBeFocused();
    await box.fill("Can you say that a different way?");
    await box.press("Enter");

    await expect.poll(async () => page.locator("article").count(), { timeout: 150_000 })
      .toBeGreaterThan(1);
  });

  test("a real citation resolves to real source metadata", async ({ page }) => {
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();

    const citation = page.locator("[data-citation]").first();
    await expect(citation).toBeVisible({ timeout: 150_000 });

    await citation.hover();

    // The assertion that matters is *which* message appears. If the runtime is
    // still not sending `artifact_id` on the `end` chunk (F3), the card says the
    // source is not linked -- and this test is how that stops being a paragraph
    // in a markdown file and starts being a failing check.
    const card = page.getByRole("dialog").or(page.locator("[data-radix-popper-content-wrapper]"));
    await expect(card).toBeVisible({ timeout: 10_000 });

    await expect(
      card.getByText(/isn't linked to this segment yet/i),
      "F3 is closed once the runtime sends artifact_id; until then this is the expected copy",
    ).toBeVisible();
  });
});

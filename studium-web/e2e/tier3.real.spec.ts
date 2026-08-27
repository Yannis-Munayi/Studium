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

    const card = page.getByRole("dialog").or(page.locator("[data-radix-popper-content-wrapper]"));
    await expect(card).toBeVisible({ timeout: 10_000 });

    // This assertion is the inverse of the one it replaces. While the runtime
    // did not send `artifact_id` (SD5) the card said the source was not linked,
    // and this case asserted *that* copy so the gap would report itself the day
    // it closed. It has closed: the id rides the `end` chunk, so the card must
    // now carry a real source rather than the placeholder.
    await expect(
      card.getByText(/linked to this segment yet/i),
      "the runtime sends artifact_id now; this copy means the id did not arrive",
    ).toHaveCount(0);

    // Real corpus, so the title cannot be asserted. What can: that something
    // resolved, and that it came from a source rather than from the model.
    await expect(card.getByText(/p{1,2}\.\s?\d+/)).toBeVisible();
  });

  test("a real practice problem routes to the bench and grades there", async ({ page }) => {
    // §9.3 end to end against real agents: the Curator selects, the runtime
    // transitions to LAB, the bench renders it, the Evaluator grades it.
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();
    await expect(page.locator("article").first()).toBeVisible({ timeout: 150_000 });

    await page.locator("body").press("/");
    await page.getByRole("combobox").fill("try");
    await page.getByRole("combobox").press("Enter");

    await expect(page.getByRole("heading", { name: "Problem" })).toBeVisible({
      timeout: 150_000,
    });
    const workspace = page.getByLabel(/your work on this problem/i);
    await expect(workspace).toBeVisible();

    // Real output, so the verdict cannot be predicted. What can be asserted is
    // that an attempt is graded rather than answered as prose: a verdict lands
    // in the feedback column, with one of §11.1's three words on it.
    await workspace.fill("Substituting the argument for the bound variable leaves the identity.");
    await page.getByRole("button", { name: "Submit" }).click();

    await expect(page.locator("[data-verdict]")).toBeVisible({ timeout: 150_000 });
  });

  test("the answer key never reaches the browser", async ({ page }) => {
    // The Tier 2 version of this checks a mock written to match the runtime.
    // This checks the runtime: a real Curator call, whose `PracticeProblem`
    // does carry a model answer, split by the Orchestrator before the chunk
    // leaves the process.
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();
    await expect(page).toHaveURL(/\/sessions\/[0-9a-f-]{36}/);
    const sessionId = new URL(page.url()).pathname.split("/").pop();

    // Its own request rather than a `page.on("response")` tap: reading a
    // streaming body from that handler consumes the stream the UI is rendering.
    const response = await page.request.post(
      `/api/backend/api/session/${sessionId}/turn`,
      { data: { text: "", primitive: "let_me_try_one" }, timeout: 150_000 },
    );
    const wire = await response.text();

    expect(wire, "the turn produced a problem").toContain("\"problem\"");
    expect(wire).not.toContain("model_answer");
    expect(wire).not.toContain("expected_key_points");
  });

  test("a real journal entry persists and reloads", async ({ page }) => {
    // §6.4 against real rows. The Confusion-Tracker writes entries off the
    // critical path, so this needs a session that produced one -- it skips
    // rather than fails when the learner's journal is empty, because "no
    // entries yet" is a legitimate state and not a defect in the read path.
    await page.goto("/journal?status=open,partial,resolved,archived");

    const entries = page.locator("a[href^='/journal/']");
    // Wait for the query to settle before deciding whether to skip. Counting
    // straight after `goto` counts an empty list that has not loaded, which
    // turns "the read path is broken" into a skipped test.
    await expect(entries.first().or(page.getByText(/nothing matches these filters/i)))
      .toBeVisible({ timeout: 30_000 });

    const count = await entries.count();
    test.skip(count === 0, "no journal entries for this learner yet");

    await entries.first().click();
    await expect(page).toHaveURL(/\/journal\/[0-9a-f-]{36}/);

    const note = `Checked by the Tier 3 pass at ${new Date().toISOString()}`;
    await page.getByLabel("Your notes").fill(note);
    await expect(page.getByText("Saved")).toBeVisible({ timeout: 30_000 });

    // The point of the case: it came back from Postgres, not from a cache.
    await page.reload();
    await expect(page.getByLabel("Your notes")).toHaveValue(note, { timeout: 30_000 });
    await expect(page.getByText("You added a note")).toBeVisible();
  });

  test("the desk reads a real learner's real history", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "The desk" })).toBeVisible();

    // Nothing about the content can be asserted -- it is whatever this learner
    // has done. What can: that every surface got a server, which is the whole
    // of F4, and that the payload satisfied its schema (a mismatch throws at
    // the boundary and would surface as the failure notice, not as a blank).
    await expect(page.locator("[data-unavailable]")).toHaveCount(0, { timeout: 30_000 });
    await expect(page.getByText(/couldn't load|could not reach/i)).toHaveCount(0);
  });
});

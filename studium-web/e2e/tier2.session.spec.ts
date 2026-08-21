import { test, expect, TEST_LEARNER_ID } from "./fixtures";

/**
 * Tier 2 — the session lifecycle and the interrupt round trip (spec §17).
 *
 * Real browser, real Next production build, real SSE over a real socket. The
 * only thing mocked is the model. That combination is what makes these tests
 * worth their runtime: the streaming behaviour under test is timing-dependent
 * and socket-dependent, and neither survives a jsdom simulation.
 */

test.describe("session lifecycle", () => {
  test("signs in and lands on the desk", async ({ page }) => {
    await page.goto("/sign-in");

    await page.getByLabel("Learner id").fill(TEST_LEARNER_ID);
    await page.getByRole("button", { name: "Continue" }).click();

    await expect(page).toHaveURL("/");
    await expect(page.getByRole("heading", { level: 1, name: "The desk" })).toBeVisible();
  });

  test("rejects an id that is not a uuid without setting a cookie", async ({ page, context }) => {
    await page.goto("/sign-in");
    await page.getByLabel("Learner id").fill("alice");
    await page.getByRole("button", { name: "Continue" }).click();

    await expect(page).toHaveURL(/\/sign-in/);
    const cookies = await context.cookies();
    expect(cookies.find((c) => c.name === "studium-learner")).toBeUndefined();
  });

  test("starts a session and streams the first segment into the classroom", async ({
    learnerPage: page,
    mock,
  }) => {
    await page.goto("/sessions/new");

    await page.getByRole("button", { name: "Begin" }).click();

    await expect(page).toHaveURL(/\/sessions\/[0-9a-f-]{36}/);

    // The scripted text arrives across several SSE frames and has to end up as
    // one continuous paragraph, not five.
    await expect(page.getByText(/leftmost-outermost/)).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(/Beta reduction is the rule/)).toBeVisible();

    const state = await mock.state();
    expect(state.turns.length).toBeGreaterThanOrEqual(1);
  });

  test("renders a citation marker as a superscript with a hover card", async ({
    learnerPage: page,
  }) => {
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();

    // §8.2: the marker is visible immediately; resolution is lazy.
    const citation = page.locator("[data-citation]").first();
    await expect(citation).toBeVisible({ timeout: 15_000 });
    await expect(citation).toHaveText("1");
  });

  test("stops the session at the budget cap without navigating", async ({
    learnerPage: page,
    mock,
  }) => {
    await mock.configure({ budgetExceeded: true });
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();

    // §7.1: "the learner stays on the start form with the error prominently
    // placed", and the copy is the backend's own, verbatim.
    await expect(page).toHaveURL(/\/sessions\/new/);
    // Scoped past the two always-present live regions (the app announcer and
    // Next's own route announcer), which are also role="alert".
    const error = page.getByRole("alert").filter({ hasText: "usage limit" });
    await expect(error).toContainText(/daily usage limit/i);
    await expect(error).toContainText(/progress is saved/i);
  });

  test("shows the runtime's degradation copy when a turn degrades", async ({
    learnerPage: page,
    mock,
  }) => {
    await mock.configure({
      degradeWith: {
        text: "There's high demand right now, so responses are slower than usual.",
        reason: "rate_limit",
      },
    });

    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();

    // The chunk kind spec §8.1 omits. A consumer built to that list drops this.
    // `.first()` past the announcer, which also receives it (§13.4 sends
    // degradation to the assertive channel as well as to the screen).
    await expect(page.getByText(/high demand right now/).first()).toBeVisible({ timeout: 15_000 });
    // §21: the learner never sees the mechanism.
    await expect(page.getByText("rate_limit")).toHaveCount(0);
  });

  test("recovers from a mid-stream drop and says so", async ({ learnerPage: page, mock }) => {
    await mock.configure({ dropAfterChunks: 2 });

    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();

    // §15.3's copy, and the retry affordance beside it.
    await expect(page.getByText(/lost connection to the tutor|can't reach the tutor/i)).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
  });
});

test.describe("the interrupt round trip (§8.3)", () => {
  test("interrupting stops the stream and hands focus to the question box", async ({
    learnerPage: page,
    mock,
  }) => {
    // Slow enough that the interrupt lands mid-stream rather than after it.
    await mock.configure({ chunkDelayMs: 400 });

    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();

    const interrupt = page.getByRole("button", { name: "Interrupt" });
    await expect(interrupt).toBeEnabled({ timeout: 15_000 });
    await interrupt.click();

    // §8.3 step 4: the pause is marked where the sentence closed.
    await expect(page.getByText("Paused here")).toBeVisible({ timeout: 20_000 });

    // §8.3 step 5: focus moves and the placeholder changes.
    await expect(page.getByPlaceholder("Your question…")).toBeFocused();

    // The interrupt actually reached the backend, on its own POST.
    const state = await mock.state();
    expect(state.interrupts.length).toBe(1);
  });

  test("Space interrupts from the document, and types a space in the box", async ({
    learnerPage: page,
    mock,
  }) => {
    await mock.configure({ chunkDelayMs: 400 });
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();

    await expect(page.getByRole("button", { name: "Interrupt" })).toBeEnabled({ timeout: 15_000 });

    await page.locator("body").press("Space");
    await expect(page.getByText("Paused here")).toBeVisible({ timeout: 20_000 });

    const box = page.getByPlaceholder("Your question…");
    await box.fill("");
    await box.press("Space");
    await box.pressSequentially("a");
    // Inside an input, Space types a space -- it does not re-interrupt.
    await expect(box).toHaveValue(" a");
  });

  test("Escape before typing cancels the interrupt", async ({ learnerPage: page, mock }) => {
    await mock.configure({ chunkDelayMs: 400 });
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();

    await expect(page.getByRole("button", { name: "Interrupt" })).toBeEnabled({ timeout: 15_000 });
    await page.getByRole("button", { name: "Interrupt" }).click();

    const box = page.getByPlaceholder("Your question…");
    await expect(box).toBeFocused({ timeout: 20_000 });

    const before = (await mock.state()).turns.length;
    await box.press("Escape");

    // §8.3 "Cancellation": the lecture resumes rather than parking on a card,
    // and no Tutor turn carrying learner text is started.
    await expect
      .poll(async () => (await mock.state()).turns.length, { timeout: 20_000 })
      .toBe(before + 1);
    const state = await mock.state();
    expect(state.turns.at(-1)?.text).toBe("");
    expect(state.turns.at(-1)?.primitive).toBeNull();
  });

  test("a question after an interrupt starts a Tutor turn", async ({ learnerPage: page, mock }) => {
    await mock.configure({ chunkDelayMs: 400 });
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();

    await expect(page.getByRole("button", { name: "Interrupt" })).toBeEnabled({ timeout: 15_000 });
    await page.getByRole("button", { name: "Interrupt" }).click();

    const box = page.getByPlaceholder("Your question…");
    await expect(box).toBeFocused({ timeout: 20_000 });
    await box.fill("Why leftmost-outermost?");
    await box.press("Enter");

    // The learner's own words are in the transcript before the answer arrives.
    await expect(page.getByText("Why leftmost-outermost?")).toBeVisible();

    await expect
      .poll(async () => (await mock.state()).turns.length, { timeout: 20_000 })
      .toBeGreaterThanOrEqual(2);

    const state = await mock.state();
    expect(state.turns.at(-1)?.text).toBe("Why leftmost-outermost?");
  });
});

test.describe("the command palette (§9)", () => {
  test("opens on / and invokes a primitive as a turn", async ({ learnerPage: page, mock }) => {
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();
    await expect(page.getByText(/Beta reduction is the rule/)).toBeVisible({ timeout: 15_000 });

    await page.locator("body").press("/");
    await expect(page.getByRole("combobox")).toBeFocused();

    await page.getByRole("combobox").fill("prove");
    await page.getByRole("combobox").press("Enter");

    await expect
      .poll(async () => (await mock.state()).turns.map((t) => t.primitive), { timeout: 20_000 })
      .toContain("prove_it_to_me");
  });

  test("shows the contextual primitives for the current state", async ({ learnerPage: page }) => {
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();
    await expect(page.getByText(/Beta reduction is the rule/)).toBeVisible({ timeout: 15_000 });

    // §9.2's LECTURING row.
    await expect(page.getByRole("button", { name: "Explain differently" })).toBeVisible();
    await expect(page.getByRole("button", { name: "I'm lost" })).toBeVisible();
  });
});

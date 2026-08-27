import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures";

/**
 * Tier 2 — citations that resolve, and a primitive that reaches the bench.
 *
 * Two closures with one thing in common: both were built, tested and inert.
 * The citation UI rendered every marker and could resolve none of them, because
 * no artifact id reached the client (SD5). The bench was reachable by route and
 * by props and no code path put a real problem in it (F15). Neither gap was
 * visible to a unit test — the marker rendered, the bench rendered — so these
 * are the cases that would have caught them, and the ones that keep them shut.
 */

test.describe("citation resolution (§10)", () => {
  test("a hover card resolves to real source metadata", async ({ learnerPage: page }) => {
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();

    const citation = page.locator("[data-citation]").first();
    await expect(citation).toBeVisible({ timeout: 15_000 });

    // Until the runtime sent `artifact_id` on the `end` chunk there was nothing
    // to look up, and the card said so. This is the assertion that the gap is
    // closed: the id arrives, the lazy fetch fires, the source appears.
    await citation.hover();

    const card = page.locator("[data-radix-popper-content-wrapper]");
    await expect(card).toBeVisible({ timeout: 10_000 });
    await expect(card).toContainText("An Introduction to Functional Programming");
    await expect(card).toContainText("pp. 42");
    await expect(card.getByText(/linked to this segment yet/i)).toHaveCount(0);
  });

  test("the card opens on keyboard focus, not only on hover", async ({ learnerPage: page }) => {
    // §10.2's wording is mouse-shaped; a keyboard learner needs the same card.
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();

    // Wait for the segment to finish before focusing. The renderer rebuilds the
    // marker's node as the sentence around it grows (F14 holds back a trailing
    // suffix that could still become a marker), and focus placed on a node that
    // is then replaced goes to the body instead.
    await expect(page.getByText(/leftmost-outermost/)).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("still-generating")).toHaveCount(0, { timeout: 15_000 });

    const citation = page.locator("[data-citation]").first();
    await expect(citation).toBeVisible();
    await citation.focus();

    await expect(page.locator("[data-radix-popper-content-wrapper]")).toBeVisible({
      timeout: 10_000,
    });
  });
});

test.describe("let_me_try_one routes to the bench (§9.3, §6.3)", () => {
  /** Invoke the primitive through the palette, as §9.1 has a learner do. */
  async function askForAProblem(page: Page): Promise<void> {
    await page.locator("body").press("/");
    await page.getByRole("combobox").fill("try");
    await page.getByRole("combobox").press("Enter");
  }

  test("the primitive replaces the reading column with the workspace", async ({
    learnerPage: page,
    mock,
  }) => {
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();
    await expect(page.getByText(/Beta reduction is the rule/)).toBeVisible({ timeout: 15_000 });

    await askForAProblem(page);

    await expect
      .poll(async () => (await mock.state()).turns.map((t) => t.primitive), { timeout: 20_000 })
      .toContain("let_me_try_one");

    // §6.3: the two-column workspace with the Curator's problem in it — not the
    // problem rendered as streamed prose, which is what it used to be.
    await expect(page.getByRole("heading", { name: "Problem" })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText(/normal form, showing each step/i)).toBeVisible();
    await expect(page.getByLabel(/your work on this problem/i)).toBeVisible();

    // The message input belongs to the classroom, not the bench.
    await expect(page.getByPlaceholder("Type to ask something…")).toHaveCount(0);
  });

  test("a wrong answer keeps the problem and a right one leaves the bench", async ({
    learnerPage: page,
  }) => {
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();
    await expect(page.getByText(/Beta reduction is the rule/)).toBeVisible({ timeout: 15_000 });

    await askForAProblem(page);

    const workspace = page.getByLabel(/your work on this problem/i);
    await expect(workspace).toBeVisible({ timeout: 20_000 });

    // First attempt: graded incorrect. §7 keeps the session in LAB, so the
    // question has to still be there for the learner about to try again.
    await workspace.fill("It reduces to itself.");
    await page.getByRole("button", { name: "Submit" }).click();

    await expect(page.getByText(/which one is outermost/i)).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText("Not quite.")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Problem" })).toBeVisible();

    // Second attempt: correct, the session leaves LAB, and so does the bench.
    await workspace.fill("It reduces to the identity function.");
    await page.getByRole("button", { name: "Submit" }).click();

    await expect(page.getByRole("heading", { name: "Problem" })).toHaveCount(0, {
      timeout: 20_000,
    });
    await expect(page.getByPlaceholder("Type to ask something…")).toBeVisible();
  });

  test("the hint is available and is not given away", async ({ learnerPage: page }) => {
    // §11.2: one hint at a time, and a collapsed one is not in the DOM at all.
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();
    await expect(page.getByText(/Beta reduction is the rule/)).toBeVisible({ timeout: 15_000 });

    await askForAProblem(page);
    await expect(page.getByRole("heading", { name: "Problem" })).toBeVisible({ timeout: 20_000 });

    await expect(page.getByText(/start with the outermost redex/i)).toHaveCount(0);
    await page.getByRole("button", { name: "Show hint" }).click();
    await expect(page.getByText(/start with the outermost redex/i)).toBeVisible();
  });

  test("the answer key never reaches the browser", async ({ learnerPage: page }) => {
    // §11.2 withholds the model answer until the learner has attempted. The
    // runtime strips it from the chunk; this checks the *wire*, because a
    // component that never draws it is a weaker guarantee than a browser that
    // never receives it.
    //
    // Driven as its own request rather than by watching the classroom's: reading
    // a streaming response body from a `page.on("response")` handler consumes
    // the stream the UI is still rendering from, and the lecture never arrives.
    // This goes through the same proxy, with the same cookie, and returns the
    // whole SSE body once the turn closes.
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();
    await expect(page).toHaveURL(/\/sessions\/[0-9a-f-]{36}/);
    const sessionId = new URL(page.url()).pathname.split("/").pop();

    const response = await page.request.post(
      `/api/backend/api/session/${sessionId}/turn`,
      { data: { text: "", primitive: "let_me_try_one" } },
    );
    const wire = await response.text();

    expect(wire, "the problem itself did travel").toContain("normal form");
    expect(wire).not.toContain("model_answer");
    expect(wire).not.toContain("expected_key_points");
  });
});

import { test, expect } from "./fixtures";

/**
 * Tier 2 — the desk, the journal, and the session-close modal (§6.1, §6.4, §6.5).
 *
 * These three surfaces shipped ahead of the endpoints behind them and rendered
 * a "not connected yet" state instead of data (F4). §17's Tier 2 case "Journal
 * CRUD: create, view, edit, resolve entries; verify optimistic updates and
 * rollback" could not run at all — the optimistic machinery was unit-tested and
 * had never been driven against a server, which is exactly the condition under
 * which the `onMutate` bug in F19 survived review.
 *
 * Now they have servers, and this is that case.
 */

test.describe("the desk (§6.1)", () => {
  test("answers what I did last, what is next, and what is still open", async ({
    learnerPage: page,
  }) => {
    await page.goto("/");

    await expect(page.getByRole("heading", { level: 1, name: "The desk" })).toBeVisible();
    await expect(page.getByText("Beta-reduction").first()).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("The Church-Rosser theorem")).toBeVisible();
    await expect(page.getByText(/treats reduction order as significant/i)).toBeVisible();
  });

  test("no surface is still reporting itself unbuilt", async ({ learnerPage: page }) => {
    // `data-unavailable` is what `SurfaceUnavailable` marks itself with. An
    // empty count is the whole of F4 in one assertion.
    await page.goto("/");
    await expect(page.getByText("Beta-reduction").first()).toBeVisible({ timeout: 15_000 });
    await expect(page.locator("[data-unavailable]")).toHaveCount(0);
  });

  test("the mastery summary reads as a table for a screen reader", async ({
    learnerPage: page,
  }) => {
    // §13.1: a chart needs a text equivalent, and here the bars *are* the table.
    await page.goto("/");
    const table = page.getByRole("table", { name: /mastery by concept/i });
    await expect(table).toBeVisible({ timeout: 15_000 });
    // The decayed posterior — the number the Curator sequenced against (F18) —
    // rather than the raw one beside it.
    await expect(table).toContainText("0.74");
    await expect(table).not.toContainText("0.82");
  });
});

test.describe("journal CRUD (§6.4, §12.1, §17)", () => {
  test("view, filter, edit, resolve", async ({ learnerPage: page, mock }) => {
    void mock;
    await page.goto("/journal");

    const entry = page.getByText(/treats reduction order as significant/i);
    await expect(entry).toBeVisible({ timeout: 15_000 });

    // §14.3 makes the URL the source of truth for filter state.
    await page.getByLabel("Search entries").fill("nothing matches this");
    await expect(page.getByText(/nothing matches these filters/i)).toBeVisible({
      timeout: 15_000,
    });
    await page.getByLabel("Search entries").fill("");
    await expect(entry).toBeVisible({ timeout: 15_000 });

    await entry.click();
    await expect(page).toHaveURL(/\/journal\/[0-9a-f-]{36}/);

    // §12.1: the note autosaves on a debounce and says so when it lands.
    await page.getByLabel("Your notes").fill("Reduction order changes cost, not result.");
    await expect(page.getByText("Saved")).toBeVisible({ timeout: 15_000 });

    // §6.4's history timeline, in words rather than in enum values (F17).
    await expect(page.getByText("You added a note")).toBeVisible({ timeout: 15_000 });

    // Resolve, through the confirmation §6.4 asks for. The optimistic update
    // (§14.1) is what makes the pill change before the PATCH returns — and the
    // detail cache is patched separately from the list cache, because mapping
    // over the detail object is what F19 was.
    await page.getByRole("button", { name: "Mark resolved" }).first().click();
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "Mark resolved" })
      .click();

    await expect(page.getByText("Resolved").first()).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("Marked resolved")).toBeVisible({ timeout: 15_000 });
  });

  // `mock` is requested for its reset, not for its methods: the previous case
  // resolves the seeded entry, and a journal test that did not reset would be
  // reading the last test's writes.
  test("the hypothesis is not shown to the learner", async ({ learnerPage: page, mock }) => {
    void mock;
    // Data layer §11 keeps the Tracker's inference off the learner's screen.
    // Frontend §6.4 asks for it with framing; the projection wins (F16).
    await page.goto("/journal");
    await expect(page.getByText(/treats reduction order as significant/i)).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByText(/the system's guess/i)).toHaveCount(0);
  });

  test("a status filter survives a reload", async ({ learnerPage: page, mock }) => {
    void mock;
    await page.goto("/journal?status=resolved");
    // The seeded entry is open, so a resolved-only filter is legitimately empty.
    await expect(page.getByText(/nothing matches these filters/i)).toBeVisible({
      timeout: 15_000,
    });

    await page.reload();
    await expect(page.getByText(/nothing matches these filters/i)).toBeVisible({
      timeout: 15_000,
    });
  });

  test("an entry that is not there says so without blaming the learner", async ({
    learnerPage: page,
  }) => {
    await page.goto("/journal/00000000-0000-4000-8000-000000000000");
    await expect(page.getByText(/isn't here/i)).toBeVisible({ timeout: 15_000 });
  });
});

test.describe("the session close modal (§6.5)", () => {
  test("reads back the summary, the deltas, the threads and what is next", async ({
    learnerPage: page,
  }) => {
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();
    await expect(page.getByText(/Beta reduction is the rule/)).toBeVisible({ timeout: 15_000 });

    await page.getByRole("button", { name: "Close session" }).click();
    // `hasText` past the dialog's own dismiss control, which is an icon button
    // labelled "Close" with no text content.
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "Close", exact: true })
      .filter({ hasText: "Close" })
      .click();

    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText(/worked through beta-reduction/i, { timeout: 20_000 });
    // §6.5: "beta-reduction: 0.65 → 0.82".
    await expect(dialog).toContainText("0.65");
    await expect(dialog).toContainText("0.82");
    await expect(dialog).toContainText("Confluence is still not settled.");
    await expect(dialog).toContainText("The Church-Rosser theorem");
  });

  test("cannot be dismissed by clicking outside it", async ({ learnerPage: page }) => {
    // §6.5: "the summary is the closing beat of a session and merits explicit
    // acknowledgment". Escape and Done work; a stray click does not.
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();
    await expect(page.getByText(/Beta reduction is the rule/)).toBeVisible({ timeout: 15_000 });

    await page.getByRole("button", { name: "Close session" }).click();
    // `hasText` past the dialog's own dismiss control, which is an icon button
    // labelled "Close" with no text content.
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "Close", exact: true })
      .filter({ hasText: "Close" })
      .click();

    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText(/worked through beta-reduction/i, { timeout: 20_000 });

    await page.mouse.click(5, 5);
    await expect(dialog).toBeVisible();
  });
});

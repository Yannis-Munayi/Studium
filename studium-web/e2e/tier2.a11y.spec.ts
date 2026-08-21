import AxeBuilder from "@axe-core/playwright";
import { test, expect, TEST_LEARNER_ID } from "./fixtures";

/**
 * Tier 2 — keyboard-only navigation and real-browser accessibility (spec §17,
 * §13.5).
 *
 * Two things here cannot be checked in jsdom and are checked nowhere else:
 *
 * **Colour contrast.** §13.1 requires 4.5:1 for text and 3:1 for UI. axe needs
 * computed styles and real layout to measure it, so the Tier 1 suite disables
 * the rule. This is where §16.2's palette is actually held to its numbers --
 * including the dark-mode values, which were chosen against the ratio rather
 * than derived by inverting lightness.
 *
 * **Focus not obscured (WCAG 2.4.11).** The floating action group sits over the
 * reading column. Whether a focused element is actually visible behind it is a
 * question about geometry, and jsdom reports every box as zero-sized.
 */

const SURFACES = [
  { path: "/", name: "the desk" },
  { path: "/sessions/new", name: "session start" },
  { path: "/journal", name: "the journal" },
  { path: "/settings", name: "settings" },
] as const;

function audit(page: import("@playwright/test").Page) {
  return new AxeBuilder({ page }).withTags([
    "wcag2a",
    "wcag2aa",
    "wcag21a",
    "wcag21aa",
    "wcag22aa",
  ]);
}

test.describe("WCAG 2.2 AA in a real browser", () => {
  for (const surface of SURFACES) {
    test(`${surface.name} has no violations in light mode`, async ({ learnerPage: page }) => {
      await page.emulateMedia({ colorScheme: "light" });
      await page.goto(surface.path);
      const results = await audit(page).analyze();
      expect(results.violations).toEqual([]);
    });

    test(`${surface.name} has no violations in dark mode`, async ({ learnerPage: page }) => {
      // §16.2's dark values are the ones most likely to fail contrast, because
      // they were written by hand rather than generated.
      await page.emulateMedia({ colorScheme: "dark" });
      await page.goto(surface.path);
      const results = await audit(page).analyze();
      expect(results.violations).toEqual([]);
    });
  }

  test("the classroom has no violations while streaming", async ({ learnerPage: page, mock }) => {
    await mock.configure({ chunkDelayMs: 300 });
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();
    await expect(page.getByText(/Beta reduction is the rule/)).toBeVisible({ timeout: 15_000 });

    const results = await audit(page).analyze();
    expect(results.violations).toEqual([]);
  });

  test("the command palette has no violations while open", async ({ learnerPage: page }) => {
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();
    await expect(page.getByText(/Beta reduction is the rule/)).toBeVisible({ timeout: 15_000 });

    await page.locator("body").press("/");
    await expect(page.getByRole("combobox")).toBeFocused();

    const results = await audit(page).analyze();
    expect(results.violations).toEqual([]);
  });
});

test.describe("keyboard-only navigation (§13.5)", () => {
  test("completes desk → session start → classroom → palette → journal", async ({
    learnerPage: page,
  }) => {
    // §13.5 names this exact path. Any surface that cannot complete it fails.
    await page.goto("/");

    // The skip link is the first tab stop.
    await page.keyboard.press("Tab");
    await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();

    // Reach the "Begin studying" action without a mouse.
    await page.goto("/sessions/new");
    const begin = page.getByRole("button", { name: "Begin" });
    await begin.focus();
    await page.keyboard.press("Enter");

    await expect(page).toHaveURL(/\/sessions\/[0-9a-f-]{36}/);
    await expect(page.getByText(/Beta reduction is the rule/)).toBeVisible({ timeout: 15_000 });

    // The palette, from the keyboard, and back out again.
    await page.locator("body").press("/");
    await expect(page.getByRole("combobox")).toBeFocused();
    await page.keyboard.press("Escape");

    // Navigation to the journal, by keyboard.
    await page.goto("/journal");
    await expect(page.getByRole("heading", { level: 1, name: "Confusion journal" })).toBeVisible();
  });

  test("the help overlay opens on ? from any surface", async ({ learnerPage: page }) => {
    // §13.3 (WCAG 3.2.6): help is in the same place everywhere.
    for (const path of ["/", "/journal", "/settings"]) {
      await page.goto(path);
      await page.locator("body").press("?");
      await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
      await page.keyboard.press("Escape");
    }
  });

  test("a focused control near the bottom is not hidden by the action group", async ({
    learnerPage: page,
    mock,
  }) => {
    // WCAG 2.4.11. The floating group is fixed bottom-right over the reading
    // column, which is exactly the geometry the criterion is about.
    await mock.configure({ chunkDelayMs: 50 });
    await page.goto("/sessions/new");
    await page.getByRole("button", { name: "Begin" }).click();
    await expect(page.getByText(/leftmost-outermost/)).toBeVisible({ timeout: 15_000 });

    const input = page.getByPlaceholder(/type to ask something/i);
    await input.focus();

    const box = await input.boundingBox();
    const viewport = page.viewportSize();
    expect(box).not.toBeNull();
    expect(viewport).not.toBeNull();

    // The focused field's own box must be inside the viewport.
    expect(box!.y).toBeGreaterThanOrEqual(0);
    expect(box!.y + box!.height).toBeLessThanOrEqual(viewport!.height + 1);

    // And it must not be fully covered: the element at its left edge is the
    // input itself, not the floating group sitting on top of it.
    const atPoint = await page.evaluate(
      ([x, y]) => {
        const element = document.elementFromPoint(x as number, y as number);
        return element?.id ?? element?.tagName ?? null;
      },
      [box!.x + 8, box!.y + box!.height / 2],
    );
    expect(atPoint).toBe("message-input");
  });
});

test.describe("reduced motion (§13.1, §16.4)", () => {
  test("collapses transitions when the learner asks for it", async ({ learnerPage: page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/");

    const duration = await page.evaluate(() => {
      const probe = document.createElement("div");
      probe.style.transition = "opacity 300ms";
      document.body.append(probe);
      const value = getComputedStyle(probe).transitionDuration;
      probe.remove();
      return value;
    });

    // §16.4: "everything above collapses to instantaneous". Not zero -- some
    // components use an animation's completion to drive state, and removing it
    // strands them. Compared as a number: the browser serialises 0.01ms as
    // "1e-05s", and asserting the string would pin a formatting detail.
    expect(Number.parseFloat(duration)).toBeLessThan(0.001);
    expect(Number.parseFloat(duration)).toBeGreaterThan(0);
  });
});

test.describe("theme (§19, question 4)", () => {
  test("honours the system preference and the override, with no flash", async ({ page, context }) => {
    await context.addCookies([
      { name: "studium-learner", value: TEST_LEARNER_ID, domain: "127.0.0.1", path: "/" },
    ]);

    await page.emulateMedia({ colorScheme: "dark" });
    await page.goto("/settings");

    // System preference, honoured with no explicit attribute set.
    await expect(page.locator("html")).not.toHaveAttribute("data-theme", "light");

    // Click the label, which is what a learner clicks: the radio itself is
    // `sr-only`, so it is reachable by keyboard and by assistive technology but
    // has no clickable box of its own.
    await page.locator('label:has(input[name="theme"][value="light"])').click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

    // The inline script in layout.tsx applies the stored choice before paint,
    // so a reload does not flash the other theme.
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  });
});

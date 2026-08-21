import { describe, expect, it } from "vitest";
import { auditCount, expectNoAxeViolations, renderWithProviders, WCAG_22_AA_TAGS } from "./helpers";

/**
 * A check on the accessibility check (spec §17's coverage-guard principle).
 *
 * `a11y.test.tsx` asserts that twenty-odd components produce zero violations.
 * That claim is only worth anything if the harness can produce a violation at
 * all -- a misconfigured rule set, a container that renders nothing, or an
 * `expect` that never runs would all report the same clean result.
 *
 * §17 already asks for a coverage guard on the suite ("if no components are
 * exercised, the suite fails rather than passing vacuously"). This is the other
 * half of the same argument: exercising components is not enough if the
 * detector is inert.
 */
describe("the axe harness", () => {
  it("fails an input with no label", async () => {
    const { container } = renderWithProviders(<input type="text" />);
    await expect(expectNoAxeViolations(container)).rejects.toThrow(/label|name/i);
  });

  it("fails a button with no accessible name", async () => {
    const { container } = renderWithProviders(<button type="button" />);
    await expect(expectNoAxeViolations(container)).rejects.toThrow();
  });

  it("fails an image with no text alternative", async () => {
    // §13.1 (WCAG 1.1.1). The lint rules below are the static checks for the
    // same requirement, disabled here because the markup is the fixture: this
    // test exists to confirm the *runtime* check catches what they catch.
    /* eslint-disable-next-line jsx-a11y/alt-text, @next/next/no-img-element */
    const { container } = renderWithProviders(<img src="/diagram.png" />);
    await expect(expectNoAxeViolations(container)).rejects.toThrow();
  });

  it("does NOT fail a skipped heading level, and that is not a gap in §13", async () => {
    // Worth pinning rather than assuming. axe classifies `heading-order` as
    // best-practice, not as a WCAG 2.2 AA failure, so the explicit tag set in
    // `helpers.tsx` deliberately excludes it. §13.1 still asks for a correct
    // hierarchy, which means axe is *not* the thing enforcing it -- the
    // assertion in `a11y.test.tsx` that a streamed `# Title` renders as an h3
    // is. If that assertion is ever deleted, nothing else catches it.
    const { container } = renderWithProviders(
      <div>
        <h1>Surface</h1>
        <h4>Skipped two levels</h4>
      </div>,
    );
    await expect(expectNoAxeViolations(container)).resolves.toBeUndefined();
  });

  it("passes markup that is actually correct", async () => {
    const { container } = renderWithProviders(
      <div>
        <label htmlFor="ok">Your answer</label>
        <input id="ok" type="text" />
      </div>,
    );
    await expect(expectNoAxeViolations(container)).resolves.toBeUndefined();
  });

  it("counts every audit, so the coverage guard cannot be fooled", async () => {
    const before = auditCount();
    const { container } = renderWithProviders(<p>Nothing wrong here.</p>);
    await expectNoAxeViolations(container);
    expect(auditCount()).toBe(before + 1);
  });

  it("runs the WCAG 2.2 AA tags rather than axe's defaults", () => {
    // axe's default set includes best-practice rules that are not WCAG
    // failures and excludes the 2.2 additions, so "axe passes" and "meets §13"
    // would be two different claims under it.
    expect(WCAG_22_AA_TAGS).toContain("wcag22aa");
    expect(WCAG_22_AA_TAGS).toContain("wcag2aa");
    expect(WCAG_22_AA_TAGS).toContain("wcag21aa");
  });
});

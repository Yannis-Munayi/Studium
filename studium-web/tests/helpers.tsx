import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderOptions, type RenderResult } from "@testing-library/react";
import axe, { type AxeResults, type Result } from "axe-core";
import { expect } from "vitest";
import type { ReactElement, ReactNode } from "react";
import { ToastProvider } from "@/components/ui/toast";

/** Render inside the providers real surfaces run under. */
export function renderWithProviders(ui: ReactElement, options?: RenderOptions): RenderResult {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
      mutations: { retry: false },
    },
    // A component under test that throws inside a query should fail the test,
    // not print a red wall from TanStack's default logger.
    logger: undefined,
  } as never);

  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <ToastProvider>{children}</ToastProvider>
      </QueryClientProvider>
    );
  }

  return render(ui, { wrapper: Wrapper, ...options });
}

/**
 * WCAG 2.2 AA rule set (spec §13, §13.5).
 *
 * Named explicitly rather than left to axe's defaults. The default set includes
 * best-practice rules that are not WCAG failures, and it excludes the 2.2
 * additions -- so "axe passes" and "meets §13" would be two different claims
 * under the default configuration.
 */
export const WCAG_22_AA_TAGS = [
  "wcag2a",
  "wcag2aa",
  "wcag21a",
  "wcag21aa",
  "wcag22aa",
] as const;

/** How many elements the a11y suite has actually put through axe. */
let auditedComponents = 0;

export function auditCount(): number {
  return auditedComponents;
}

/**
 * Run axe over a container and fail on any WCAG 2.2 AA violation.
 *
 * §13.5: "The failing rule and its remediation are surfaced in the test
 * output." axe's own message plus the offending markup is what makes a
 * violation actionable rather than merely reported.
 */
export async function expectNoAxeViolations(container: HTMLElement): Promise<void> {
  const results: AxeResults = await axe.run(container, {
    runOnly: { type: "tag", values: [...WCAG_22_AA_TAGS] },
    // Colour contrast needs real layout and computed styles. jsdom has neither,
    // so axe cannot evaluate it here and reports it as "incomplete" rather than
    // passing. §13.1's contrast requirement is checked against a real browser
    // in the Tier 2 Playwright pass instead, where it can actually be measured.
    rules: { "color-contrast": { enabled: false } },
  });

  auditedComponents += 1;

  if (results.violations.length > 0) {
    throw new Error(formatViolations(results.violations));
  }
  expect(results.violations).toEqual([]);
}

function formatViolations(violations: Result[]): string {
  return violations
    .map((violation) => {
      const nodes = violation.nodes
        .map((node) => `      ${node.html}\n      → ${node.failureSummary ?? ""}`)
        .join("\n");
      return [
        `${violation.id} (${violation.impact ?? "unknown"}): ${violation.help}`,
        `  ${violation.helpUrl}`,
        nodes,
      ].join("\n");
    })
    .join("\n\n");
}

// --- fixtures --------------------------------------------------------------

export const FIXTURE_SESSION_ID = "0f8fad5b-d9cb-469f-a165-70867728950e";
export const FIXTURE_ARTIFACT_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7";
export const FIXTURE_CHUNK_ID = "16fd2706-8baf-433b-82eb-8c7fada847da";

export const FIXTURE_CITATION = {
  marker: "P1",
  chunk_id: FIXTURE_CHUNK_ID,
  source_id: FIXTURE_ARTIFACT_ID,
  source_title: "An Introduction to Functional Programming",
  source_authors: ["Michaelson"],
  page_start: 42,
  page_end: 44,
  section_path: "Chapter 3 › 3.2 Beta Reduction",
  excerpt: "A redex is an application of a lambda abstraction to an argument.",
  excerpt_start_offset: 0,
  excerpt_end_offset: 64,
  source_deleted: false,
};

export const FIXTURE_JOURNAL_ENTRY = {
  id: FIXTURE_ARTIFACT_ID,
  learner_subject_id: FIXTURE_SESSION_ID,
  concept_id: FIXTURE_CHUNK_ID,
  concept_name: "Beta reduction",
  status: "open" as const,
  summary: "Not sure why leftmost-outermost is the normalising order.",
  summary_author: "tutor" as const,
  hypothesis: "Confusing evaluation order with termination.",
  learner_note: null,
  first_seen_at: "2026-08-18T10:00:00Z",
  last_touched_at: "2026-08-20T10:00:00Z",
};

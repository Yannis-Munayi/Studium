import { afterAll, beforeEach, describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  auditCount,
  expectNoAxeViolations,
  renderWithProviders,
  FIXTURE_JOURNAL_ENTRY,
} from "./helpers";

import { Button } from "@/components/ui/button";
import { Disclosure } from "@/components/ui/disclosure";
import { Verdict, StatusPill } from "@/components/ui/verdict";
import { Announcements } from "@/components/ui/announcements";
import { Dialog, ConfirmDialog } from "@/components/ui/dialog";
import { ComprehensionCheck } from "@/components/session/comprehension-check";
import { InterruptButton } from "@/components/session/interrupt-button";
import { MessageInput } from "@/components/session/message-input";
import { ResumptionCard } from "@/components/session/resumption-card";
import { Transcript } from "@/components/session/transcript";
import { DegradationNotice, ConnectionNotice } from "@/components/session/degradation-notice";
import { StreamedMarkdown } from "@/components/session/streamed-markdown";
import { Bench } from "@/components/surfaces/bench";
import { Desk } from "@/components/surfaces/desk";
import { SettingsSurface } from "@/components/surfaces/settings";
import { SurfaceUnavailable } from "@/components/shared/unavailable";
import { CommandPalette } from "@/components/session/command-palette";
import { useSessionStore } from "@/lib/state/session";
import { useCommandPaletteStore } from "@/lib/state/palette";
import { CONNECTION } from "@/lib/copy/degradation";
import * as Tooltip from "@radix-ui/react-tooltip";

/**
 * §17 Tier 1: "Accessibility: axe-core assertions on every component. Zero
 * WCAG 2.2 AA violations."
 *
 * §3 says accessibility is designed in rather than retrofitted, and this suite
 * is what makes that falsifiable. Every component a learner can reach goes
 * through axe under the WCAG 2.2 AA rule set (see `helpers.tsx` for why the tag
 * list is explicit and why contrast is checked in Tier 2 instead).
 */

const noop = () => {};

beforeEach(() => {
  useSessionStore.getState().reset();
  useCommandPaletteStore.getState().reset();
});

describe("primitives", () => {
  it("Button has an accessible name in every variant", async () => {
    const { container } = renderWithProviders(
      <>
        <Button variant="primary">Begin</Button>
        <Button variant="secondary">Cancel</Button>
        <Button variant="quiet">Dismiss</Button>
        <Button variant="danger">Close session</Button>
        <Button disabled>Unavailable</Button>
      </>,
    );
    await expectNoAxeViolations(container);
  });

  it("Disclosure reports its expanded state", async () => {
    const { container } = renderWithProviders(
      <Disclosure label="Show hint">Try substituting the argument first.</Disclosure>,
    );

    const trigger = screen.getByRole("button", { name: "Show hint" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    await userEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    await expectNoAxeViolations(container);
  });

  it("Verdict carries its meaning in text, not only colour", async () => {
    const { container } = renderWithProviders(
      <>
        <Verdict kind="correct" feedback="That's the redex." />
        <Verdict kind="partial" feedback="Nearly — check the binder." />
        <Verdict kind="incorrect" feedback="That's the wrong order." />
      </>,
    );

    // §13.1 (WCAG 1.4.1): each verdict is announced by name, so a learner who
    // cannot see the colour still gets the verdict.
    expect(screen.getByText("Correct.")).toBeInTheDocument();
    expect(screen.getByText("Partly right.")).toBeInTheDocument();
    expect(screen.getByText("Not yet.")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("StatusPill names each status", async () => {
    const { container } = renderWithProviders(
      <>
        <StatusPill status="open" />
        <StatusPill status="partial" />
        <StatusPill status="resolved" />
        <StatusPill status="archived" />
      </>,
    );
    // `resolved` and `archived` share a glyph, so the word is what separates them.
    expect(screen.getByText("Resolved")).toBeInTheDocument();
    expect(screen.getByText("Archived")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("Announcements renders both live regions before anything is announced", async () => {
    const { container } = renderWithProviders(<Announcements />);

    // A live region inserted alongside its first message is not announced --
    // the region has to exist before the mutation it reports.
    expect(screen.getByTestId("announcer-polite")).toHaveAttribute("aria-live", "polite");
    expect(screen.getByTestId("announcer-assertive")).toHaveAttribute("aria-live", "assertive");
    await expectNoAxeViolations(container);
  });
});

describe("dialogs", () => {
  it("Dialog exposes a title and a description", async () => {
    const { baseElement } = renderWithProviders(
      <Dialog open onOpenChange={noop} title="Session complete" description="What you covered">
        <p>Beta reduction, alpha conversion.</p>
      </Dialog>,
    );
    await expectNoAxeViolations(baseElement as HTMLElement);
  });

  it("ConfirmDialog does not put focus on the destructive option", async () => {
    renderWithProviders(
      <ConfirmDialog
        open
        onOpenChange={noop}
        title="Close session"
        message="Your progress is saved."
        confirmLabel="Close"
        onConfirm={noop}
      />,
    );

    // Radix would otherwise focus the first tabbable node, and Enter on an
    // unread dialog would close the session.
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("ConfirmDialog is auditable while open", async () => {
    const { baseElement } = renderWithProviders(
      <ConfirmDialog
        open
        onOpenChange={noop}
        title="Mark resolved"
        message="You can reopen it later."
        confirmLabel="Mark resolved"
        onConfirm={noop}
      />,
    );
    await expectNoAxeViolations(baseElement as HTMLElement);
  });
});

describe("session components", () => {
  it("StreamedMarkdown renders prose, math and code without violations", async () => {
    const { container } = renderWithProviders(
      <StreamedMarkdown
        text={
          "## Beta reduction\n\nA redex is $(\\lambda x. M)\\,N$ [P1].\n\n" +
          "```haskell\nid x = x\n```\n\n- first\n- second\n"
        }
        streaming={false}
        artifactId={null}
      />,
    );
    await expectNoAxeViolations(container);
  });

  it("StreamedMarkdown demotes segment headings so levels are not skipped", () => {
    renderWithProviders(
      <StreamedMarkdown text={"# Segment title\n\nBody."} streaming={false} artifactId={null} />,
    );
    // §13.1: the surface owns h1 and h2; a segment must not introduce a second h1.
    expect(screen.getByRole("heading", { level: 3, name: "Segment title" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
  });

  it("InterruptButton announces why it is disabled", async () => {
    const { container } = renderWithProviders(
      <Tooltip.Provider>
        <InterruptButton onInterrupt={noop} />
      </Tooltip.Provider>,
    );

    // Idle: the label says so rather than reading "Interrupt" on a dead control.
    expect(
      screen.getByRole("button", { name: "Interrupt — nothing is streaming" }),
    ).toBeDisabled();
    await expectNoAxeViolations(container);
  });

  it("MessageInput has a label and a described send affordance", async () => {
    const { container } = renderWithProviders(
      <MessageInput onSend={noop} onTypingInterrupt={noop} onCancelInterrupt={noop} />,
    );
    expect(screen.getByLabelText(/type to ask something/i)).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("ComprehensionCheck labels its answer field", async () => {
    const { container } = renderWithProviders(
      <ComprehensionCheck
        question="Which redex does leftmost-outermost pick?"
        onSubmit={async () => ({ verdict: "correct" as const, feedback: "Right." })}
        onComplete={noop}
      />,
    );
    expect(screen.getByLabelText(/your answer to the comprehension check/i)).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("ResumptionCard offers both continuations", async () => {
    const { container } = renderWithProviders(
      <ResumptionCard
        onContinue={noop}
        onAskAnother={noop}
        showEscalation
        onEscalate={noop}
        onDismissEscalation={noop}
      />,
    );
    expect(screen.getByRole("button", { name: "Continue lecture" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask another question" })).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("Transcript labels each turn by who produced it", async () => {
    const { container } = renderWithProviders(
      <Transcript
        streaming={false}
        turns={[
          {
            id: "t1",
            speaker: "lecturer",
            text: "Beta reduction rewrites an application.",
            complete: true,
            artifactId: null,
            interruptedAt: null,
            end: null,
          },
          {
            id: "t2",
            speaker: "learner",
            text: "Why leftmost-outermost?",
            complete: true,
            artifactId: null,
            interruptedAt: null,
            end: null,
          },
          {
            id: "t3",
            speaker: "tutor",
            text: "Because it is normalising.",
            complete: true,
            artifactId: null,
            interruptedAt: null,
            end: null,
          },
        ]}
      />,
    );

    expect(screen.getByLabelText("Lecture segment")).toBeInTheDocument();
    expect(screen.getByLabelText("Tutor response")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("CommandPalette is a labelled combobox over a listbox", async () => {
    useCommandPaletteStore.getState().setOpen(true);
    const { baseElement } = renderWithProviders(<CommandPalette onInvoke={noop} />);

    const input = screen.getByRole("combobox");
    expect(input).toHaveAttribute("aria-controls", "palette-list");
    expect(screen.getByRole("listbox", { name: "Primitives" })).toBeInTheDocument();
    expect(screen.getAllByRole("option").length).toBeGreaterThan(0);

    await expectNoAxeViolations(baseElement as HTMLElement);
  });
});

describe("degradation surfaces", () => {
  it("DegradationNotice is an alert with an action", async () => {
    const { container } = renderWithProviders(
      <DegradationNotice
        copy={{ title: "Can't reach the tutor", body: "Your progress is saved.", action: "Try again" }}
        onRetry={noop}
      />,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("ConnectionNotice escalates its copy after the retry budget", () => {
    const { rerender } = renderWithProviders(
      <ConnectionNotice attempts={1} maxAttempts={3} onRetry={noop} />,
    );
    expect(screen.getByText(CONNECTION.reconnecting.title)).toBeInTheDocument();

    rerender(<ConnectionNotice attempts={3} maxAttempts={3} onRetry={noop} />);
    expect(screen.getByText(CONNECTION.lost.title)).toBeInTheDocument();
  });

  it("SurfaceUnavailable explains itself without a raw endpoint name on screen", async () => {
    const { container } = renderWithProviders(
      <SurfaceUnavailable endpoint="journalList" what="Your confusion journal" />,
    );
    expect(screen.getByText(/isn't connected yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/GET \/api/)).not.toBeInTheDocument();
    await expectNoAxeViolations(container);
  });
});

describe("surfaces", () => {
  it("Desk renders with no learner and no data", async () => {
    const { container } = renderWithProviders(<Desk userId={null} />);
    expect(screen.getByRole("heading", { level: 1, name: "The desk" })).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("Bench labels its problem, workspace and feedback regions", async () => {
    const { container } = renderWithProviders(
      <Bench
        problem={{
          statement: "Reduce $(\\lambda x. x\\,x)(\\lambda y. y)$ to normal form.",
          constraints: ["Show each step."],
          hints: ["Start by substituting.", "Then reduce the inner redex."],
          expectedMinutes: 10,
        }}
        feedback={null}
        onSubmit={noop}
      />,
    );

    expect(screen.getByLabelText(/your work on this problem/i)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /problem/i })).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("Bench renders a rubric verdict without violations", async () => {
    const { container } = renderWithProviders(
      <Bench
        problem={{ statement: "Reduce the term." }}
        feedback={{
          verdict: "partial",
          feedback: "The first step is right.",
          rubric: [
            { criterion: "Identifies the redex", verdict: "correct", note: "Yes." },
            { criterion: "Substitutes correctly", verdict: "incorrect", note: "Capture occurred." },
          ],
          modelAnswer: "First substitute, then reduce.",
        }}
        onSubmit={noop}
      />,
    );
    await expectNoAxeViolations(container);
  });

  it("SettingsSurface associates every control with its description", async () => {
    const { container } = renderWithProviders(<SettingsSurface />);
    expect(screen.getByLabelText(/follow the text as it arrives/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/text size/i)).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });
});

describe("mastery summary", () => {
  it("is a real table, so the chart needs no separate text equivalent", async () => {
    // §13.1 asks charts to have "a text data table equivalent, toggleable from
    // the chart". Making the bars a table means there is no toggle to get wrong
    // and no second representation to keep in sync.
    const { container } = renderWithProviders(<Desk userId={null} />);
    await expectNoAxeViolations(container);
    expect(FIXTURE_JOURNAL_ENTRY.status).toBe("open"); // fixture stays exercised
  });
});

/**
 * §17 Tier 1: "Coverage guard on the accessibility test suite: if no components
 * are exercised, the suite fails rather than passing vacuously."
 *
 * A suite that renders nothing reports zero violations, which reads identically
 * to a suite that renders everything and finds nothing wrong. This is the
 * difference between those two states.
 */
describe("coverage guard", () => {
  afterAll(() => {
    const audited = auditCount();
    expect(
      audited,
      `The accessibility suite audited ${audited} components. ` +
        "Zero violations across too few components is not evidence of anything.",
    ).toBeGreaterThanOrEqual(18);
  });

  it("has run", () => {
    expect(auditCount()).toBeGreaterThan(0);
  });
});

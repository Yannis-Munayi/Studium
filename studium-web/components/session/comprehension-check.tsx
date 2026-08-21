"use client";

import { useEffect, useState } from "react";
import { CHECK } from "@/lib/copy/surfaces";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Disclosure } from "@/components/ui/disclosure";
import { Verdict, type VerdictKind } from "@/components/ui/verdict";

/**
 * The inline comprehension check (spec §6.2, §11.1).
 *
 * §6.2 is emphatic that this is "an inset card within the reading flow, not a
 * modal". That is not a styling preference: a modal would take focus away from
 * the lecture, and §7.2's two-attempt cycle ends by continuing the lecture --
 * so a modal would have to be dismissed to reveal the thing the learner is
 * being returned to.
 *
 * §11.1's "never punitive" rule is carried by the copy and by what is absent:
 * no score, no attempt counter, no progress penalty rendered anywhere.
 */

export interface CheckAttempt {
  verdict: VerdictKind;
  feedback: string;
  hint?: string | null;
  modelAnswer?: string | null;
}

/** §7.2's cycle: two attempts, then the model answer. */
export const MAX_ATTEMPTS = 2;

/** §7.2: "Feedback appears with a 300ms delay after the grade completes." */
const FEEDBACK_DELAY_MS = 300;

export function ComprehensionCheck({
  question,
  onSubmit,
  onComplete,
  onHintExpanded,
}: {
  question: string;
  /** Grades one attempt. Rejection is surfaced, not swallowed. */
  onSubmit: (answer: string, attempt: number) => Promise<CheckAttempt>;
  /** Called once the cycle ends, whatever the verdict (§7.2 step 3). */
  onComplete: (final: CheckAttempt) => void;
  onHintExpanded?: () => void;
}) {
  const [answer, setAnswer] = useState("");
  const [attempts, setAttempts] = useState<CheckAttempt[]>([]);
  const [grading, setGrading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [visible, setVisible] = useState<CheckAttempt | null>(null);

  const latest = attempts[attempts.length - 1] ?? null;
  const exhausted = attempts.length >= MAX_ATTEMPTS;
  const settled = latest?.verdict === "correct" || exhausted;

  // The 300ms is a beat between "Checking…" disappearing and the verdict
  // appearing, so the two do not swap in the same frame.
  useEffect(() => {
    if (!latest) return;
    const timer = setTimeout(() => setVisible(latest), FEEDBACK_DELAY_MS);
    return () => clearTimeout(timer);
  }, [latest]);

  useEffect(() => {
    if (visible && settled) onComplete(visible);
    // `onComplete` is intentionally not a dependency: a parent that rebuilds
    // the callback each render would fire the completion repeatedly.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, settled]);

  async function submit() {
    const text = answer.trim();
    if (!text || grading || settled) return;

    setGrading(true);
    setError(null);
    try {
      const result = await onSubmit(text, attempts.length + 1);
      setAttempts((prior) => [...prior, result]);
      setAnswer("");
    } catch {
      // §3: never a silent failure. An ungraded answer that looks graded is
      // worse than an error, because the learner moves on believing they were
      // right.
      setError("That answer couldn't be checked. Try submitting it again.");
    } finally {
      setGrading(false);
    }
  }

  return (
    <section
      aria-labelledby="check-heading"
      className="my-loose rounded border border-line bg-surface p-normal"
    >
      <h3 id="check-heading" className="font-sans text-sm font-semibold uppercase tracking-wide text-muted">
        {CHECK.heading}
      </h3>

      <p className="prose-reading mt-tight max-w-none">{question}</p>

      {!settled ? (
        <div className="mt-normal">
          <label htmlFor="check-answer" className="sr-only">
            Your answer to the comprehension check
          </label>
          <textarea
            id="check-answer"
            rows={3}
            value={answer}
            disabled={grading}
            placeholder={CHECK.answerPlaceholder}
            onChange={(event) => setAnswer(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) submit();
            }}
            className={cn(
              "w-full resize-y rounded border border-line bg-background px-3 py-2",
              "font-sans text-sm text-ink placeholder:text-muted",
            )}
          />

          {error ? (
            // §13.3 (WCAG 3.3.1): announced, and adjacent to the field.
            <p role="alert" className="mt-tight font-sans text-sm text-[var(--color-concern)]">
              {error}
            </p>
          ) : null}

          <div className="mt-tight flex justify-end">
            <Button variant="primary" onClick={submit} disabled={grading || !answer.trim()}>
              {grading ? CHECK.checking : CHECK.submit}
            </Button>
          </div>
        </div>
      ) : null}

      {visible ? (
        <div className="mt-normal border-t border-line pt-normal">
          <Verdict kind={visible.verdict} feedback={visible.feedback} />

          {/* A hint arrives with a wrong or partial first attempt (§7.2). */}
          {visible.hint && !settled ? (
            <Disclosure label={CHECK.showHint} onExpand={onHintExpanded} className="mt-normal">
              {visible.hint}
            </Disclosure>
          ) : null}

          {settled && visible.verdict !== "correct" && visible.modelAnswer ? (
            <div className="mt-normal">
              <p className="font-sans text-sm text-muted">{CHECK.modelAnswer}</p>
              <p className="prose-reading mt-tight max-w-none">{visible.modelAnswer}</p>
            </div>
          ) : null}
        </div>
      ) : grading ? (
        <p className="mt-normal font-sans text-sm text-muted" role="status">
          {CHECK.checking}
        </p>
      ) : null}
    </section>
  );
}

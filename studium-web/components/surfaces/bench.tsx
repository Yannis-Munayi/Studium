"use client";

import { useState } from "react";
import { BENCH } from "@/lib/copy/surfaces";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Disclosure } from "@/components/ui/disclosure";
import { Verdict, type VerdictKind } from "@/components/ui/verdict";
import { StreamedMarkdown } from "@/components/session/streamed-markdown";

/**
 * The bench (spec §6.3, §11.2).
 *
 * Two columns above 900px -- problem and workspace left, feedback and hints
 * right -- collapsing to workspace-above-feedback below it. The collapse order
 * matters: feedback that appears above the workspace on a narrow viewport pushes
 * the answer box off screen at the moment the learner wants to revise it.
 */

export interface Problem {
  statement: string;
  setup?: string | null;
  constraints?: string[];
  hints?: string[];
  expectedMinutes?: number | null;
}

export interface LabFeedback {
  verdict: VerdictKind;
  feedback: string;
  /** Per-criterion breakdown, for assessment attempts (§6.3). */
  rubric?: Array<{ criterion: string; verdict: VerdictKind; note: string }>;
  modelAnswer?: string | null;
}

export function Bench({
  problem,
  feedback,
  onSubmit,
  onHintExpanded,
  submitting = false,
  /** Monospace for code-adjacent subjects, serif for prose (§6.3). */
  workspaceStyle = "prose",
}: {
  problem: Problem;
  feedback: LabFeedback | null;
  onSubmit: (answer: string) => void;
  onHintExpanded?: (index: number) => void;
  submitting?: boolean;
  workspaceStyle?: "prose" | "code";
}) {
  const [answer, setAnswer] = useState("");
  // §11.2: "Hints appear one at a time — the second hint requires expanding a
  // 'Show another hint' disclosure below the first."
  const [hintsShown, setHintsShown] = useState(1);

  return (
    <div className="mx-auto max-w-5xl px-normal py-loose">
      <h1 className="sr-only">{BENCH.heading}</h1>

      <div className="grid gap-loose lg:grid-cols-2">
        <div className="flex flex-col gap-loose">
          <section aria-labelledby="problem-heading">
            <h2 id="problem-heading" className="font-sans text-sm font-semibold uppercase tracking-wide text-muted">
              {BENCH.problem}
            </h2>

            <div className="mt-tight rounded border border-line bg-surface p-normal">
              <StreamedMarkdown text={problem.statement} streaming={false} artifactId={null} />

              {problem.setup ? (
                <div className="mt-normal border-t border-line pt-normal">
                  <StreamedMarkdown text={problem.setup} streaming={false} artifactId={null} />
                </div>
              ) : null}

              {problem.constraints && problem.constraints.length > 0 ? (
                <ul className="mt-normal list-disc pl-5 font-sans text-sm text-muted">
                  {problem.constraints.map((constraint) => (
                    <li key={constraint}>{constraint}</li>
                  ))}
                </ul>
              ) : null}

              {problem.expectedMinutes ? (
                <p className="mt-normal font-sans text-xs text-muted">
                  {BENCH.expectedTime}: about {problem.expectedMinutes} minutes
                </p>
              ) : null}
            </div>
          </section>

          <section aria-labelledby="workspace-heading">
            <h2 id="workspace-heading" className="font-sans text-sm font-semibold uppercase tracking-wide text-muted">
              {BENCH.workspace}
            </h2>

            <label htmlFor="lab-answer" className="sr-only">
              Your work on this problem
            </label>
            <textarea
              id="lab-answer"
              rows={10}
              value={answer}
              disabled={submitting}
              onChange={(event) => setAnswer(event.target.value)}
              className={cn(
                "mt-tight w-full resize-y rounded border border-line bg-background px-3 py-2 text-ink",
                workspaceStyle === "code" ? "font-mono text-sm" : "font-serif text-base",
              )}
            />

            <div className="mt-tight flex justify-end">
              <Button
                variant="primary"
                disabled={submitting || !answer.trim()}
                onClick={() => onSubmit(answer.trim())}
              >
                {submitting ? BENCH.submitting : BENCH.submit}
              </Button>
            </div>
          </section>
        </div>

        <div className="flex flex-col gap-loose">
          <section aria-labelledby="feedback-heading">
            <h2 id="feedback-heading" className="font-sans text-sm font-semibold uppercase tracking-wide text-muted">
              {BENCH.feedback}
            </h2>

            <div className="mt-tight rounded border border-line p-normal">
              {submitting ? (
                <p role="status" className="font-sans text-sm text-muted">
                  {BENCH.submitting}
                </p>
              ) : feedback ? (
                <>
                  <Verdict kind={feedback.verdict} feedback={feedback.feedback} />

                  {feedback.rubric && feedback.rubric.length > 0 ? (
                    <ul className="mt-normal flex flex-col gap-tight border-t border-line pt-normal">
                      {feedback.rubric.map((criterion) => (
                        <li key={criterion.criterion}>
                          <p className="font-sans text-xs font-medium text-ink">
                            {criterion.criterion}
                          </p>
                          <Verdict kind={criterion.verdict} feedback={criterion.note} />
                        </li>
                      ))}
                    </ul>
                  ) : null}

                  {feedback.modelAnswer ? (
                    <div className="mt-normal border-t border-line pt-normal">
                      <p className="font-sans text-sm text-muted">A full answer looks like:</p>
                      <StreamedMarkdown
                        text={feedback.modelAnswer}
                        streaming={false}
                        artifactId={null}
                        className="mt-tight"
                      />
                    </div>
                  ) : null}
                </>
              ) : (
                <p className="font-sans text-sm text-muted">{BENCH.awaitingSubmission}</p>
              )}
            </div>
          </section>

          {problem.hints && problem.hints.length > 0 ? (
            <section aria-labelledby="hints-heading">
              <h2 id="hints-heading" className="sr-only">
                Hints
              </h2>
              <div className="flex flex-col">
                {problem.hints.slice(0, hintsShown).map((hint, index) => (
                  <Disclosure
                    key={hint}
                    label={index === 0 ? "Show hint" : "Show another hint"}
                    onExpand={() => {
                      onHintExpanded?.(index);
                      // Revealing the next disclosure only once this one is
                      // opened keeps §11.2's one-at-a-time rule true even for a
                      // learner clicking quickly.
                      setHintsShown((n) => Math.max(n, index + 2));
                    }}
                  >
                    {hint}
                  </Disclosure>
                ))}
              </div>
            </section>
          ) : null}
        </div>
      </div>
    </div>
  );
}

"use client";

import { useMutation } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { startSession } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { SESSION_START } from "@/lib/copy/surfaces";
import type { SessionMode } from "@/lib/api/schemas";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";

/**
 * Session start (spec §7.1).
 *
 * §5.3 puts starting a session on the pessimistic side of the optimistic-update
 * line: it is expensive, it can be refused by the budget gate, and navigating
 * optimistically would land the learner in a classroom with no session behind
 * it. So this waits, and shows why it is waiting.
 */

const MODES: Array<{ value: SessionMode; label: string; available: boolean }> = [
  { value: "lecture", label: SESSION_START.modes.lecture, available: true },
  { value: "tutorial", label: SESSION_START.modes.tutorial, available: true },
  { value: "lab", label: SESSION_START.modes.lab, available: true },
  // §7.1: review is deferred. Shown and disabled rather than hidden, so the
  // learner knows the mode exists and is not yet running.
  { value: "review", label: SESSION_START.modes.review, available: false },
];

const DURATIONS = [30, 45, 60, 90, 120] as const;

export function SessionStartForm({ userId }: { userId: string }) {
  const router = useRouter();
  const [mode, setMode] = useState<SessionMode>("lecture");
  const [minutes, setMinutes] = useState<number>(90);

  const start = useMutation({
    mutationFn: () =>
      startSession({ user_id: userId, mode, target_duration_minutes: minutes }),
    onSuccess: (session) => {
      // The mode the runtime says the session has, not the one this form sent.
      // §20 allows one active session per learner, so a learner with an
      // unfinished session gets that one back — and navigating with the
      // requested mode gave the classroom a lecture it was never going to be
      // handed, with every turn routing to the Tutor instead. See F22.
      const actual = session.mode ?? mode;
      router.push(`/sessions/${session.session_id}?mode=${actual}&minutes=${minutes}`);
    },
  });

  const budget = start.error instanceof ApiError ? start.error.budget : undefined;

  return (
    <div className="mx-auto max-w-lg px-normal py-generous">
      <h1 className="font-sans text-2xl font-semibold leading-[var(--leading-heading)]">
        {SESSION_START.heading}
      </h1>

      <form
        className="mt-loose flex flex-col gap-loose"
        onSubmit={(event) => {
          event.preventDefault();
          start.mutate();
        }}
      >
        <fieldset className="border-0 p-0">
          <legend className="font-sans text-sm font-medium text-ink">{SESSION_START.mode}</legend>
          <div className="mt-tight flex flex-col gap-tight">
            {MODES.map((option) => (
              <label
                key={option.value}
                className={cn(
                  "flex items-start gap-tight rounded border border-line p-3 font-sans text-sm",
                  option.available ? "cursor-pointer hover:bg-surface" : "cursor-not-allowed text-muted",
                  mode === option.value && option.available && "border-[var(--color-accent)]",
                )}
              >
                <input
                  type="radio"
                  name="mode"
                  value={option.value}
                  checked={mode === option.value}
                  disabled={!option.available}
                  onChange={() => setMode(option.value)}
                  className="mt-1"
                />
                <span>{option.label}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <div>
          <label htmlFor="minutes" className="font-sans text-sm font-medium text-ink">
            {SESSION_START.duration}
          </label>
          <select
            id="minutes"
            value={minutes}
            onChange={(event) => setMinutes(Number(event.target.value))}
            className="mt-tight w-full rounded border border-line bg-background px-3 py-2 font-sans text-sm text-ink"
          >
            {DURATIONS.map((value) => (
              <option key={value} value={value}>
                {value} minutes
              </option>
            ))}
          </select>
        </div>

        {/* §7.1's budget pre-flight: the learner stays on the form, and the
            error is prominent rather than a toast that scrolls away. */}
        {start.isError ? (
          <div
            role="alert"
            className="rounded border border-[var(--color-attention)] bg-surface p-normal"
          >
            <p className="font-sans text-sm text-ink">
              {budget
                ? // Already learner-facing copy from the backend, with the
                  // reset time localised there. Rendered verbatim (§15.4).
                  budget.message
                : start.error instanceof ApiError && start.error.kind === "not_found"
                  ? "You're not enrolled in a subject yet, so there's nothing to open a session on."
                  : "Couldn't start a session just now. Try again in a moment."}
            </p>
          </div>
        ) : null}

        <div>
          <Button variant="primary" disabled={start.isPending} asChild>
            <button type="submit">
              {start.isPending ? SESSION_START.preparing : SESSION_START.submit}
            </button>
          </Button>

          {start.isPending ? (
            <p role="status" className="mt-tight font-sans text-xs text-muted">
              {SESSION_START.slowStart}
            </p>
          ) : null}
        </div>
      </form>
    </div>
  );
}

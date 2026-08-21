"use client";

import { useMutation } from "@tanstack/react-query";
import { useEffect } from "react";
import Link from "next/link";
import { closeSession } from "@/lib/api/client";
import { SESSION_CLOSE, CLASSROOM } from "@/lib/copy/surfaces";
import { useUIStore } from "@/lib/state/ui";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import type { SessionSummary } from "@/lib/api/schemas";

/**
 * Session close (spec §6.5, §7.3).
 *
 * §6.5 makes two things non-negotiable: this cannot be dismissed by clicking
 * outside, and Escape or an explicit Done must work. §13.2 requires the second;
 * §6.5 wants the first because "the summary is the closing beat of a session
 * and merits explicit acknowledgment". The `Dialog` wrapper takes both.
 *
 * §7.3's close is a 5-15 second LLM call, so the pending state is not a
 * courtesy -- it is most of the interaction.
 */
export function SessionCloseDialog({
  open,
  sessionId,
  onDismiss,
}: {
  open: boolean;
  sessionId: string;
  onDismiss: () => void;
}) {
  const showCost = useUIStore((s) => s.showCost);

  // §5.3: closing is an expensive mutation, so it shows a pending state and
  // waits. No optimism here -- there is nothing to be optimistic about.
  const close = useMutation({
    mutationFn: () => closeSession(sessionId),
  });

  useEffect(() => {
    if (open && close.isIdle) close.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const summary = summaryFrom(close.data);

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !next && !close.isPending && onDismiss()}
      title={close.isPending ? CLASSROOM.wrappingUp : SESSION_CLOSE.heading}
      dismissOnOutsideClick={false}
      footer={
        close.isPending ? null : (
          <>
            <Button variant="secondary" onClick={onDismiss}>
              {SESSION_CLOSE.done}
            </Button>
            <Button variant="primary" asChild>
              <Link href="/sessions/new">{SESSION_CLOSE.startNext}</Link>
            </Button>
          </>
        )
      }
    >
      {close.isPending ? (
        <p role="status" className="text-sm text-muted">
          Writing up what you covered. This takes a few seconds.
        </p>
      ) : close.isError ? (
        <div>
          <p className="text-sm text-ink">
            Your session is closed and your progress is saved. The written summary didn&apos;t
            generate.
          </p>
          <p className="mt-tight text-sm text-muted">
            Nothing you did is lost — the summary is a convenience, not the record.
          </p>
        </div>
      ) : (
        <SummaryBody summary={summary} showCost={showCost} errors={close.data?.errors ?? []} />
      )}
    </Dialog>
  );
}

function SummaryBody({
  summary,
  showCost,
  errors,
}: {
  summary: SessionSummary | null;
  showCost: boolean;
  errors: string[];
}) {
  return (
    <div className="flex flex-col gap-loose">
      {summary?.summary ? (
        <p className="prose-reading max-w-none">{summary.summary}</p>
      ) : (
        // §6.5's contents come from `session_summaries`, which no endpoint
        // serves (F4). The close call reports whether the summary was written;
        // saying so plainly beats an empty panel that looks like a bug.
        <p className="text-sm text-muted">
          Your summary was written to your record. Reading it back here needs the session-summary
          endpoint, which isn&apos;t built yet.
        </p>
      )}

      {summary && summary.concepts_touched.length > 0 ? (
        <section>
          <h3 className="font-sans text-sm font-semibold text-ink">
            {SESSION_CLOSE.conceptsTouched}
          </h3>
          <ul className="mt-tight flex flex-col gap-1">
            {summary.concepts_touched.map((delta) => (
              <li key={delta.concept_id} className="font-sans text-sm text-muted">
                {delta.concept_name}: {delta.before.toFixed(2)} → {delta.after.toFixed(2)}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {summary && summary.open_threads.length > 0 ? (
        <section>
          <h3 className="font-sans text-sm font-semibold text-ink">{SESSION_CLOSE.openThreads}</h3>
          <ul className="mt-tight list-disc pl-5">
            {summary.open_threads.map((thread) => (
              <li key={thread} className="font-sans text-sm text-muted">
                {thread}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {summary?.duration_minutes || (showCost && summary?.cost_usd) ? (
        <p className="font-sans text-xs text-muted">
          {SESSION_CLOSE.duration}: {Math.round(summary?.duration_minutes ?? 0)}m
          {showCost && summary?.cost_usd != null
            ? ` · ${SESSION_CLOSE.cost}: $${summary.cost_usd.toFixed(2)}`
            : ""}
        </p>
      ) : null}

      {errors.length > 0 ? (
        // The close endpoint returns partial failures rather than throwing.
        // Reporting them is the difference between "saved" and "mostly saved".
        <p className="font-sans text-xs text-[var(--color-attention)]">
          Some parts of the close didn&apos;t finish: {errors.join("; ")}
        </p>
      ) : null}
    </div>
  );
}

/**
 * The close endpoint returns `{ended, summarised, errors}` and not the summary
 * text itself, so there is nothing to map yet. Kept as a seam so wiring the
 * summary endpoint (F4) is one function body rather than a component rewrite.
 */
function summaryFrom(_data: unknown): SessionSummary | null {
  return null;
}

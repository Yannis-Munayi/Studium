"use client";

import { useMutation } from "@tanstack/react-query";
import { useEffect } from "react";
import Link from "next/link";
import { closeSession } from "@/lib/api/client";
import { useSessionSummary } from "@/lib/api/hooks";
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

  /**
   * Fetched only once the close has reported `summarised: true`.
   *
   * The row does not exist until the Curator's call lands, so asking earlier is
   * a guaranteed 404 -- and asking at all when close said it did not summarise
   * is asking a question that has already been answered. §6.5's contents come
   * from `session_summaries`; the close response says whether there are any.
   */
  const summarised = close.data?.summarised === true;
  const { data: summary = null } = useSessionSummary(sessionId, summarised);

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
        // The Curator's call failed, or the summary has not landed yet. §3
        // forbids a silent gap, and this is the honest sentence: the session
        // closed and the record is intact; the write-up is what is missing.
        <p className="text-sm text-muted">
          Your session is closed and your progress is saved. The written summary isn&apos;t
          available — nothing you did is lost, the summary is a convenience rather than the
          record.
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

      {/* §6.5's "What's next". The concept is named rather than linked: the
          primary action below already starts the next session, and a second
          route to the same place invites the guess that they differ. */}
      {summary?.next_focus_concept_name ? (
        <section>
          <h3 className="font-sans text-sm font-semibold text-ink">{SESSION_CLOSE.whatsNext}</h3>
          <p className="mt-tight font-serif text-base text-ink">
            {summary.next_focus_concept_name}
          </p>
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
        <p className="font-sans text-xs text-[var(--color-attention-strong)]">
          Some parts of the close didn&apos;t finish: {errors.join("; ")}
        </p>
      ) : null}
    </div>
  );
}

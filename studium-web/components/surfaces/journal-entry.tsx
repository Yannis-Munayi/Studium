"use client";

import * as Tooltip from "@radix-ui/react-tooltip";
import { useEffect, useRef, useState } from "react";
import { useJournalEntry, useResolveJournalEntry, useSaveLearnerNote } from "@/lib/api/hooks";
import { isSurfaceUnavailable } from "@/lib/api/surfaces";
import { JOURNAL } from "@/lib/copy/surfaces";
import type { JournalEvent } from "@/lib/api/schemas";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { StatusPill } from "@/components/ui/verdict";
import { SurfaceUnavailable } from "@/components/shared/unavailable";
import { relativeAge } from "./journal";

/** §12.1's autosave debounce. */
const AUTOSAVE_DEBOUNCE_MS = 2000;

/**
 * One journal entry (spec §6.4, §12).
 *
 * §12.1 is the substance here: three text fields with three different owners,
 * and a UI that makes the difference visible. The learner's summary is theirs
 * to edit; the Tracker's hypothesis is an inference they can read and not
 * rewrite; their note is their own space. Blurring those would let the system's
 * guess about a learner's confusion read as the learner's own account of it.
 */
export function JournalEntryDetail({ entryId }: { entryId: string }) {
  const { data: entry, isLoading, error } = useJournalEntry(entryId);
  const resolve = useResolveJournalEntry();
  const saveNote = useSaveLearnerNote(entryId);

  const [note, setNote] = useState<string | null>(null);
  const [confirmingResolve, setConfirmingResolve] = useState(false);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (entry && note === null) setNote(entry.learner_note ?? "");
  }, [entry, note]);

  // Flush a pending save on unmount. Without it, navigating away within the
  // debounce window silently discards what was just typed -- and the "Saved"
  // indicator makes that worse, because the learner has been told not to worry.
  useEffect(
    () => () => {
      if (debounce.current) clearTimeout(debounce.current);
    },
    [],
  );

  if (isSurfaceUnavailable(error)) {
    return (
      <div className="mx-auto max-w-2xl px-normal py-loose">
        <SurfaceUnavailable endpoint="journalEntry" what="This journal entry" />
      </div>
    );
  }
  if (isLoading) return <p className="p-normal font-sans text-sm text-muted">Loading…</p>;
  if (!entry) return <p className="p-normal font-sans text-sm text-muted">Entry not found.</p>;

  return (
    <Tooltip.Provider delayDuration={300}>
      <article className="mx-auto max-w-2xl px-normal py-loose">
        <div className="flex items-start justify-between gap-normal">
          <h1 className="font-serif text-2xl font-semibold leading-[var(--leading-heading)]">
            {entry.summary}
          </h1>
          <StatusPill status={entry.status} />
        </div>

        <p className="mt-tight font-sans text-xs text-muted">
          {/* §12.1: the summary carries a "written by" attribution. */}
          {entry.summary_author === "learner" ? JOURNAL.writtenByYou : JOURNAL.writtenByTutor} ·{" "}
          {entry.concept_name ?? "No concept"} · {relativeAge(entry.last_touched_at)}
        </p>

        {entry.hypothesis ? (
          <section className="mt-loose rounded border border-line bg-surface p-normal">
            <div className="flex items-center gap-tight">
              <h2 className="font-sans text-sm font-semibold text-ink">{JOURNAL.hypothesis}</h2>
              <Tooltip.Root>
                <Tooltip.Trigger asChild>
                  <button
                    type="button"
                    className="rounded font-sans text-xs text-muted underline decoration-dotted"
                  >
                    why this was flagged
                  </button>
                </Tooltip.Trigger>
                <Tooltip.Portal>
                  <Tooltip.Content
                    side="top"
                    className="z-50 max-w-72 rounded border border-line bg-background px-3 py-2 font-sans text-xs"
                  >
                    {JOURNAL.hypothesisTooltip}
                  </Tooltip.Content>
                </Tooltip.Portal>
              </Tooltip.Root>
            </div>
            {/* Read-only, and not an editable field styled to look read-only:
                there is no input here at all (§12.1). */}
            <p className="mt-tight font-serif text-base text-ink">{entry.hypothesis}</p>
          </section>
        ) : null}

        <section className="mt-loose">
          <label htmlFor="learner-note" className="font-sans text-sm font-semibold text-ink">
            {JOURNAL.learnerNote}
          </label>
          <textarea
            id="learner-note"
            rows={6}
            value={note ?? ""}
            placeholder={JOURNAL.learnerNotePlaceholder}
            onChange={(event) => {
              const next = event.target.value;
              setNote(next);
              if (debounce.current) clearTimeout(debounce.current);
              debounce.current = setTimeout(() => saveNote.mutate(next), AUTOSAVE_DEBOUNCE_MS);
            }}
            className="mt-tight w-full resize-y rounded border border-line bg-background px-3 py-2 font-serif text-base text-ink"
          />
          <p role="status" className="mt-1 h-4 font-sans text-xs text-muted">
            {saveNote.isPending ? "Saving…" : saveNote.isSuccess ? JOURNAL.saved : ""}
          </p>
        </section>

        {entry.history.length > 0 ? (
          <section className="mt-loose">
            <h2 className="font-sans text-sm font-semibold text-ink">{JOURNAL.history}</h2>
            <ol className="mt-tight flex flex-col gap-1">
              {entry.history.map((event) => (
                <HistoryRow key={event.id} event={event} />
              ))}
            </ol>
          </section>
        ) : null}

        <div className="mt-loose flex flex-wrap gap-tight border-t border-line pt-normal">
          {entry.status === "open" || entry.status === "partial" ? (
            <Button variant="primary" onClick={() => setConfirmingResolve(true)}>
              {JOURNAL.markResolved}
            </Button>
          ) : (
            <Button variant="secondary">{JOURNAL.reopen}</Button>
          )}
          <Button variant="quiet">{JOURNAL.archive}</Button>
        </div>

        <ConfirmDialog
          open={confirmingResolve}
          onOpenChange={setConfirmingResolve}
          title={JOURNAL.markResolved}
          message={JOURNAL.confirmResolve}
          confirmLabel={JOURNAL.markResolved}
          onConfirm={() => resolve.mutate(entryId)}
        />
      </article>
    </Tooltip.Provider>
  );
}

function HistoryRow({ event }: { event: JournalEvent }) {
  return (
    <li className="font-sans text-sm text-muted">
      <time dateTime={event.at}>{relativeAge(event.at)}</time> — {event.kind}
    </li>
  );
}

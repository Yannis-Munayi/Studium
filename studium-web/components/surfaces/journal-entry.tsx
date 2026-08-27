"use client";

import * as Tooltip from "@radix-ui/react-tooltip";
import { useEffect, useRef, useState } from "react";
import {
  useJournalEntry,
  useResolveJournalEntry,
  useSaveLearnerNote,
  useSetJournalStatus,
} from "@/lib/api/hooks";
import { isSurfaceUnavailable } from "@/lib/api/surfaces";
import { ApiError } from "@/lib/api/errors";
import { JOURNAL } from "@/lib/copy/surfaces";
import type { JournalEvent } from "@/lib/api/schemas";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { StatusPill } from "@/components/ui/verdict";
import { SurfaceUnavailable } from "@/components/shared/unavailable";
import { ApiFailureNotice } from "@/components/session/degradation-notice";
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
  const setStatus = useSetJournalStatus(entryId);
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
  if (error) {
    // A 404 here also covers "not yours" — the backend refuses to distinguish
    // them, so neither does this. Everything else is a failure to reach the
    // server, which is a different sentence and a different remedy.
    const missing = error instanceof ApiError && error.kind === "not_found";
    return (
      <div className="mx-auto max-w-2xl px-normal py-loose">
        {missing ? (
          <p className="font-sans text-sm text-muted">{JOURNAL.notFound}</p>
        ) : (
          <ApiFailureNotice kind={error instanceof ApiError ? error.kind : "server_error"} />
        )}
      </div>
    );
  }
  if (isLoading) return <p className="p-normal font-sans text-sm text-muted">Loading…</p>;
  if (!entry) return <p className="p-normal font-sans text-sm text-muted">{JOURNAL.notFound}</p>;

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

        {/* §6.4's actions. Resolve confirms because it is the one that says
            "this is over"; the rest are one click, because reopening something
            you reopened by accident costs another click and nothing else. */}
        <div className="mt-loose flex flex-wrap gap-tight border-t border-line pt-normal">
          {entry.status === "open" || entry.status === "partial" ? (
            <>
              <Button
                variant="primary"
                disabled={setStatus.isPending}
                onClick={() => setConfirmingResolve(true)}
              >
                {JOURNAL.markResolved}
              </Button>
              {entry.status === "open" ? (
                <Button
                  variant="secondary"
                  disabled={setStatus.isPending}
                  onClick={() => setStatus.mutate("partial")}
                >
                  {JOURNAL.markPartial}
                </Button>
              ) : null}
            </>
          ) : (
            <Button
              variant="secondary"
              disabled={setStatus.isPending}
              onClick={() => setStatus.mutate("open")}
            >
              {JOURNAL.reopen}
            </Button>
          )}

          {entry.status === "archived" ? null : (
            <Button
              variant="quiet"
              disabled={setStatus.isPending}
              onClick={() => setStatus.mutate("archived")}
            >
              {JOURNAL.archive}
            </Button>
          )}
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

/**
 * §6.4's timeline, in words rather than in enum values.
 *
 * The kinds are `journal_event_kind` from data layer §6.7. Rendering them raw
 * put `partially_addressed` on screen for a learner reading their own history —
 * legible to whoever wrote the migration, and to nobody else.
 */
const EVENT_LABEL: Record<JournalEvent["kind"], string> = {
  created: "Opened",
  revisited: "Came up again",
  partially_addressed: "Partly worked through",
  resolved: "Marked resolved",
  reopened: "Reopened",
  archived: "Archived",
  hypothesis_updated: "The system revised its guess",
  learner_note_added: "You added a note",
};

function HistoryRow({ event }: { event: JournalEvent }) {
  return (
    <li className="font-sans text-sm text-muted">
      <time dateTime={event.at}>{relativeAge(event.at)}</time> — {EVENT_LABEL[event.kind]}
    </li>
  );
}

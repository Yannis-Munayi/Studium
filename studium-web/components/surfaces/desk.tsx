"use client";

import Link from "next/link";
import { useDesk } from "@/lib/api/hooks";
import { isSurfaceUnavailable } from "@/lib/api/surfaces";
import { DESK } from "@/lib/copy/surfaces";
import type { ConceptMastery, JournalEntry, RecentSession } from "@/lib/api/schemas";
import { Button } from "@/components/ui/button";
import { StatusPill } from "@/components/ui/verdict";
import { SurfaceUnavailable } from "@/components/shared/unavailable";
import { ApiFailureNotice } from "@/components/session/degradation-notice";
import { ApiError } from "@/lib/api/errors";

/**
 * The desk (spec §6.1).
 *
 * Answers three questions at a glance: what did I do last, what should I do
 * now, what is still open. The layout follows §6.1's 60/40 split above 900px
 * and stacks below it.
 */
export function Desk({ userId }: { userId: string | null }) {
  const { data, isLoading, error } = useDesk(userId);

  return (
    <div className="mx-auto max-w-5xl px-normal py-loose">
      <h1 className="font-sans text-2xl font-semibold leading-[var(--leading-heading)]">
        {DESK.heading}
      </h1>

      {/* Two columns at ~900px. The reading column elsewhere is narrower, but
          the desk is a scanning surface rather than a reading one. */}
      <div className="mt-loose grid gap-loose lg:grid-cols-[3fr_2fr]">
        <div className="flex flex-col gap-loose">
          <section aria-labelledby="continue-heading">
            <h2 id="continue-heading" className="sr-only">
              Continue
            </h2>
            <ContinueCard session={data?.open_session ?? null} loading={isLoading} error={error} />
          </section>

          <section aria-labelledby="recent-heading">
            <h2 id="recent-heading" className="font-sans text-sm font-semibold uppercase tracking-wide text-muted">
              {DESK.recentSessions}
            </h2>
            {isSurfaceUnavailable(error) ? (
              <div className="mt-tight">
                <SurfaceUnavailable endpoint="desk" what="Your session history" />
              </div>
            ) : (data?.recent_sessions.length ?? 0) === 0 ? (
              <p className="mt-tight font-sans text-sm text-muted">{DESK.noRecentSessions}</p>
            ) : (
              <ul className="mt-tight flex flex-col divide-y divide-[var(--color-border)]">
                {data?.recent_sessions.map((session) => (
                  <RecentSessionRow key={session.id} session={session} />
                ))}
              </ul>
            )}
          </section>

          <section aria-labelledby="syllabus-heading">
            <h2 id="syllabus-heading" className="font-sans text-sm font-semibold uppercase tracking-wide text-muted">
              {DESK.syllabusNext}
            </h2>
            <ol className="mt-tight flex flex-col gap-1">
              {(data?.syllabus_next ?? []).map((concept, index) => (
                // §6.1: not clickable in MVP — the map that would let a learner
                // jump ahead is deferred, and a link that reorders the syllabus
                // without the graph behind it would skip prerequisites.
                <li key={concept.id} className="font-sans text-sm text-muted">
                  {index + 1}. {concept.name}
                </li>
              ))}
            </ol>
          </section>
        </div>

        <aside className="flex flex-col gap-loose">
          <section aria-labelledby="journal-heading">
            <h2 id="journal-heading" className="font-sans text-sm font-semibold uppercase tracking-wide text-muted">
              {DESK.openEntries}
            </h2>
            {isSurfaceUnavailable(error) ? (
              <div className="mt-tight">
                <SurfaceUnavailable endpoint="journalList" what="Your journal" />
              </div>
            ) : (data?.open_journal_entries.length ?? 0) === 0 ? (
              <p className="mt-tight font-sans text-sm text-muted">{DESK.noOpenEntries}</p>
            ) : (
              <ul className="mt-tight flex flex-col gap-tight">
                {data?.open_journal_entries.slice(0, 5).map((entry) => (
                  <JournalRow key={entry.id} entry={entry} />
                ))}
              </ul>
            )}
          </section>

          {/* §6.1: the review queue is deferred but "reserves layout space", so
              the desk does not visibly rearrange when the module ships. */}
          <section aria-labelledby="review-heading">
            <h2 id="review-heading" className="font-sans text-sm font-semibold uppercase tracking-wide text-muted">
              {DESK.reviewQueue}
            </h2>
            <p className="mt-tight font-sans text-sm text-muted">{DESK.reviewDeferred}</p>
          </section>

          <section aria-labelledby="mastery-heading">
            <h2 id="mastery-heading" className="font-sans text-sm font-semibold uppercase tracking-wide text-muted">
              {DESK.mastery}
            </h2>
            <MasterySummary mastery={data?.mastery ?? []} />
          </section>
        </aside>
      </div>
    </div>
  );
}

function ContinueCard({
  session,
  loading,
  error,
}: {
  session: RecentSession | null;
  loading: boolean;
  error: unknown;
}) {
  if (loading) {
    return <p className="font-sans text-sm text-muted">Loading…</p>;
  }
  if (error && !isSurfaceUnavailable(error)) {
    return <ApiFailureNotice kind={error instanceof ApiError ? error.kind : "server_error"} />;
  }

  return (
    <div className="rounded border border-line bg-surface p-loose">
      {session ? (
        <>
          <p className="font-sans text-sm text-muted">Where you left off</p>
          <p className="mt-tight font-serif text-lg text-ink">
            {session.concepts_touched[0] ?? "Your last session"}
          </p>
          <Button variant="primary" className="mt-normal" asChild>
            <Link href={`/sessions/${session.id}`}>{DESK.continueSession}</Link>
          </Button>
        </>
      ) : (
        <>
          <p className="font-serif text-lg text-ink">{DESK.firstTime}</p>
          <Button variant="primary" className="mt-normal" asChild>
            <Link href="/sessions/new">{DESK.beginStudying}</Link>
          </Button>
        </>
      )}
    </div>
  );
}

function RecentSessionRow({ session }: { session: RecentSession }) {
  return (
    <li className="py-3">
      <div className="flex items-baseline justify-between gap-normal">
        <span className="font-sans text-sm text-ink">{session.mode}</span>
        <span className="font-sans text-xs text-muted">
          {session.duration_minutes ? `${Math.round(session.duration_minutes)}m` : "—"}
        </span>
      </div>
      {session.concepts_touched.length > 0 ? (
        <p className="mt-1 font-sans text-xs text-muted">{session.concepts_touched.join(", ")}</p>
      ) : null}
    </li>
  );
}

function JournalRow({ entry }: { entry: JournalEntry }) {
  return (
    <li>
      <Link
        href={`/journal/${entry.id}`}
        className="block rounded border border-line p-3 transition-colors duration-[var(--duration-hover)] hover:bg-surface"
      >
        <div className="flex items-start justify-between gap-tight">
          <p className="font-serif text-sm text-ink">{entry.summary}</p>
          <StatusPill status={entry.status} />
        </div>
        {entry.concept_name ? (
          <p className="mt-1 font-sans text-xs text-muted">{entry.concept_name}</p>
        ) : null}
      </Link>
    </li>
  );
}

/**
 * §6.1's mastery summary.
 *
 * §13.1 requires a chart to have "a text data table equivalent, toggleable from
 * the chart". Here the bars *are* a table -- a `<table>` with a width-scaled
 * cell per row -- so there is no toggle to get wrong and nothing to keep in
 * sync. A screen reader reads the numbers; everyone else sees the bars.
 *
 * Renders `p_known_decayed`, not `p_known`. The decayed value is what the
 * Curator sequences against and what the unlock gate reads (data layer C1), so
 * it is what "where you stand" means -- the raw posterior would show 0.9 on a
 * concept the system had already decided to revisit. See F18.
 */
function MasterySummary({ mastery }: { mastery: ConceptMastery[] }) {
  if (mastery.length === 0) {
    return <p className="mt-tight font-sans text-sm text-muted">No mastery recorded yet.</p>;
  }

  return (
    <table className="mt-tight w-full font-sans text-sm">
      <caption className="sr-only">Mastery by concept, as a probability from 0 to 1</caption>
      <thead className="sr-only">
        <tr>
          <th scope="col">Concept</th>
          <th scope="col">Mastery</th>
        </tr>
      </thead>
      <tbody>
        {mastery.map((concept) => (
          <tr key={concept.concept_id}>
            <th scope="row" className="py-1 pr-normal text-left font-normal text-muted">
              {concept.concept_name}
            </th>
            <td className="w-1/2 py-1">
              <span className="flex items-center gap-tight">
                <span aria-hidden className="h-2 flex-1 rounded-full bg-[var(--color-border)]">
                  <span
                    className="block h-full rounded-full bg-[var(--color-accent)]"
                    style={{ width: `${Math.round(concept.p_known_decayed * 100)}%` }}
                  />
                </span>
                <span className="w-10 text-right text-xs text-muted">
                  {concept.p_known_decayed.toFixed(2)}
                </span>
              </span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

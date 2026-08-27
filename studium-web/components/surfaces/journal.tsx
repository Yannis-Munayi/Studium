"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";
import { useJournalEntries } from "@/lib/api/hooks";
import { isSurfaceUnavailable } from "@/lib/api/surfaces";
import { JOURNAL } from "@/lib/copy/surfaces";
import type { JournalEntry, JournalStatus } from "@/lib/api/schemas";
import { StatusPill } from "@/components/ui/verdict";
import { SurfaceUnavailable } from "@/components/shared/unavailable";
import { ApiFailureNotice } from "@/components/session/degradation-notice";
import { ApiError } from "@/lib/api/errors";

const ALL_STATUSES: JournalStatus[] = ["open", "partial", "resolved", "archived"];
/** §6.4: "Default: Open + Partial." */
const DEFAULT_STATUSES: JournalStatus[] = ["open", "partial"];

/**
 * The journal browser (spec §6.4).
 *
 * §14.3 makes the URL the source of truth for filter state, so a filtered view
 * is a link a learner can bookmark and the browser's back button walks their
 * filter history. The alternative -- filters in a store -- gives you a back
 * button that leaves the journal entirely.
 */
export function Journal() {
  const router = useRouter();
  const params = useSearchParams();

  const selected = parseStatuses(params.get("status"));
  const search = params.get("q") ?? "";

  const { data, isLoading, error } = useJournalEntries({
    status: selected,
    ...(search ? { search } : {}),
  });

  const setParam = useCallback(
    (key: string, value: string | null) => {
      const next = new URLSearchParams(params.toString());
      if (value) next.set(key, value);
      else next.delete(key);
      router.replace(`/journal?${next}`, { scroll: false });
    },
    [params, router],
  );

  return (
    <div className="mx-auto max-w-5xl px-normal py-loose">
      <h1 className="font-sans text-2xl font-semibold leading-[var(--leading-heading)]">
        {JOURNAL.heading}
      </h1>

      <div className="mt-loose grid gap-loose lg:grid-cols-[16rem_1fr]">
        <aside aria-labelledby="filters-heading">
          <h2 id="filters-heading" className="font-sans text-sm font-semibold uppercase tracking-wide text-muted">
            {JOURNAL.filters}
          </h2>

          <fieldset className="mt-tight border-0 p-0">
            <legend className="font-sans text-xs text-muted">{JOURNAL.status}</legend>
            <div className="mt-tight flex flex-col gap-1">
              {ALL_STATUSES.map((status) => (
                <label key={status} className="flex items-center gap-tight font-sans text-sm">
                  <input
                    type="checkbox"
                    checked={selected.includes(status)}
                    onChange={(event) => {
                      const next = event.target.checked
                        ? [...selected, status]
                        : selected.filter((s) => s !== status);
                      setParam("status", next.length ? next.join(",") : null);
                    }}
                  />
                  <span className="capitalize">{status}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <div className="mt-normal">
            <label htmlFor="journal-search" className="font-sans text-xs text-muted">
              {JOURNAL.search}
            </label>
            <input
              id="journal-search"
              type="search"
              defaultValue={search}
              onChange={(event) => setParam("q", event.target.value || null)}
              className="mt-tight w-full rounded border border-line bg-background px-3 py-2 font-sans text-sm text-ink"
            />
          </div>
        </aside>

        <section aria-labelledby="entries-heading">
          <h2 id="entries-heading" className="sr-only">
            Entries
          </h2>

          {isSurfaceUnavailable(error) ? (
            <SurfaceUnavailable endpoint="journalList" what="Your confusion journal" />
          ) : error ? (
            <ApiFailureNotice kind={error instanceof ApiError ? error.kind : "server_error"} />
          ) : isLoading ? (
            <p className="font-sans text-sm text-muted">Loading…</p>
          ) : (data?.length ?? 0) === 0 ? (
            <p className="font-sans text-sm text-muted">{JOURNAL.empty}</p>
          ) : (
            <ul className="flex flex-col divide-y divide-[var(--color-border)]">
              {data?.map((entry) => (
                <EntryRow key={entry.id} entry={entry} />
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}

function EntryRow({ entry }: { entry: JournalEntry }) {
  return (
    <li>
      <Link
        href={`/journal/${entry.id}`}
        className="block py-normal transition-colors duration-[var(--duration-hover)] hover:bg-surface"
      >
        <div className="flex items-start justify-between gap-normal">
          <p className="font-serif text-base text-ink">{entry.summary}</p>
          <StatusPill status={entry.status} />
        </div>

        <p className="mt-1 font-sans text-xs text-muted">
          {entry.concept_name ?? "No concept"} · {relativeAge(entry.last_touched_at)}
        </p>

        {entry.hypothesis ? (
          <p className="mt-tight font-sans text-sm italic text-muted">
            {/* §6.4: the hypothesis preview carries its framing inline. A
                tooltip alone would leave a touch or screen-reader user reading
                the system's guess as the learner's own words. */}
            <span className="not-italic">The system&apos;s guess: </span>
            {entry.hypothesis}
          </p>
        ) : null}
      </Link>
    </li>
  );
}

function parseStatuses(raw: string | null): JournalStatus[] {
  if (!raw) return DEFAULT_STATUSES;
  const parsed = raw
    .split(",")
    .filter((s): s is JournalStatus => (ALL_STATUSES as string[]).includes(s));
  return parsed.length > 0 ? parsed : DEFAULT_STATUSES;
}

export function relativeAge(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const days = Math.floor((Date.now() - then) / 86_400_000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days} days ago`;
  const months = Math.floor(days / 30);
  return months === 1 ? "a month ago" : `${months} months ago`;
}

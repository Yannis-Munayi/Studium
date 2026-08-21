"use client";

import * as Popover from "@radix-ui/react-popover";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { fetchArtifactCitations } from "@/lib/api/client";
import type { Citation as CitationData } from "@/lib/api/schemas";
import { citationLabel, type CitationToken } from "@/lib/stream/citations";
import { cn } from "@/lib/cn";
import { Dialog } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

/**
 * Citation markers and their hover cards (spec §10).
 *
 * §10.2 wants hover *and* keyboard focus to open the card, with a 300ms open
 * delay and 100ms close delay. Radix Popover gives focus and Escape for free
 * but opens on click; Radix HoverCard gives the delays but not keyboard
 * activation. Neither alone satisfies §10.2, so this is a Popover driven by
 * hover timers as well as focus -- which is also what makes the card usable by
 * a learner navigating with a keyboard, who §10.2's mouse-shaped wording
 * otherwise leaves out.
 */

const OPEN_DELAY_MS = 300;
const CLOSE_DELAY_MS = 100;

export interface CitationProps {
  token: CitationToken;
  /**
   * The artifact whose citations resolve this marker.
   *
   * Null when the runtime has not told us which artifact the segment became --
   * which is currently always, see DIVERGENCES-FRONTEND.md F3. A marker with no
   * artifact still renders: §8.2 requires the marker visible the instant it
   * arrives, and the resolution is lazy by design.
   */
  artifactId: string | null;
}

export function Citation({ token, artifactId }: CitationProps) {
  const [open, setOpen] = useState(false);
  const [fullPassage, setFullPassage] = useState<CitationData | null>(null);
  const [timer, setTimer] = useState<ReturnType<typeof setTimeout> | null>(null);

  // §8.2: "fetched lazily on first hover". `enabled` gates on `open`, so a
  // lecture with forty markers makes zero requests until one is pointed at.
  const { data, isLoading, isError } = useQuery({
    queryKey: ["artifact", artifactId, "citations"],
    queryFn: () => fetchArtifactCitations(artifactId as string),
    enabled: open && artifactId !== null,
    // §5.3: immutable per artifact.
    staleTime: 60 * 60 * 1000,
  });

  const resolved = data?.citations.filter((c) => token.markers.includes(c.marker)) ?? [];
  const primary = resolved[0] ?? null;
  const retired = primary?.source_deleted ?? false;

  function schedule(next: boolean) {
    if (timer) clearTimeout(timer);
    setTimer(setTimeout(() => setOpen(next), next ? OPEN_DELAY_MS : CLOSE_DELAY_MS));
  }

  function immediately(next: boolean) {
    if (timer) clearTimeout(timer);
    setOpen(next);
  }

  return (
    <>
      <Popover.Root open={open} onOpenChange={setOpen}>
        <Popover.Trigger asChild>
          <button
            type="button"
            // §10.1: the aria-label carries the source, so a screen reader
            // announces "Citation 3: Michaelson, page 42" rather than "3".
            aria-label={describe(token, primary)}
            onMouseEnter={() => schedule(true)}
            onMouseLeave={() => schedule(false)}
            onFocus={() => immediately(true)}
            onBlur={() => schedule(false)}
            className={cn(
              "align-super font-serif text-[0.8em] leading-none",
              "transition-colors duration-[var(--duration-hover)]",
              retired
                ? // §10.3: muted, no underline. The marker stays legible --
                  // the citation is still a fact about what was written.
                  "cursor-default text-muted"
                : "text-accent hover:underline",
            )}
            data-citation={token.raw}
            data-retired={retired || undefined}
          >
            {citationLabel(token)}
          </button>
        </Popover.Trigger>

        <Popover.Portal>
          <Popover.Content
            side="top"
            align="start"
            sideOffset={6}
            collisionPadding={16}
            onMouseEnter={() => immediately(true)}
            onMouseLeave={() => schedule(false)}
            className={cn(
              "z-50 w-[min(26rem,calc(100vw-2rem))] rounded border border-line",
              "bg-surface p-normal font-sans text-sm shadow-sm",
            )}
          >
            <CitationCard
              citations={resolved}
              loading={isLoading}
              error={isError}
              unresolvable={artifactId === null}
              onReadFull={(citation) => {
                setFullPassage(citation);
                immediately(false);
              }}
            />
            <Popover.Arrow className="fill-[var(--color-border)]" />
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>

      {/* §10.2's "Read full passage" modal. */}
      <Dialog
        open={fullPassage !== null}
        onOpenChange={(next) => !next && setFullPassage(null)}
        title={fullPassage?.source_title ?? "Passage"}
        description={fullPassage ? sourceLine(fullPassage) : undefined}
      >
        <p className="prose-reading max-w-none whitespace-pre-wrap">
          {fullPassage?.excerpt ?? ""}
        </p>
      </Dialog>
    </>
  );
}

function CitationCard({
  citations,
  loading,
  error,
  unresolvable,
  onReadFull,
}: {
  citations: CitationData[];
  loading: boolean;
  error: boolean;
  unresolvable: boolean;
  onReadFull: (citation: CitationData) => void;
}) {
  if (unresolvable) {
    // Honest rather than blank. The marker is real and the passage behind it is
    // real; what is missing is the link between them (F3). Saying "loading"
    // here would be a spinner that never resolves, which §3 forbids by name.
    return (
      <p className="text-muted">
        The source for this passage isn&apos;t linked to this segment yet.
      </p>
    );
  }
  if (loading) return <p className="text-muted">Looking up the source…</p>;
  if (error) return <p className="text-muted">Couldn&apos;t load this source right now.</p>;
  if (citations.length === 0) return <p className="text-muted">No source recorded for this marker.</p>;

  return (
    <div className="flex flex-col gap-normal">
      {citations.map((citation) => (
        <div key={citation.chunk_id}>
          <p className="font-medium text-ink">{citation.source_title}</p>
          <p className="text-muted">{sourceLine(citation)}</p>

          {citation.source_deleted ? (
            // §10.3: the note replaces the excerpt. The backend withholds the
            // text as well, so this is a statement of fact, not a UI choice.
            <p className="mt-tight text-muted">
              This source is no longer available. The citation is preserved for historical
              reference.
            </p>
          ) : citation.excerpt ? (
            <>
              <p className="mt-tight font-serif text-ink">
                {truncate(citation.excerpt, 300)}
              </p>
              <Button
                variant="quiet"
                size="sm"
                className="mt-tight px-0"
                onClick={() => onReadFull(citation)}
              >
                Read full passage
              </Button>
            </>
          ) : null}
        </div>
      ))}
    </div>
  );
}

/** §10.2's author / page / section line. */
function sourceLine(citation: CitationData): string {
  const parts: string[] = [];
  if (citation.source_authors.length > 0) parts.push(citation.source_authors.join(", "));
  const pages = pageReference(citation);
  if (pages) parts.push(pages);
  if (citation.section_path) parts.push(citation.section_path);
  return parts.join(" · ");
}

/** "p. 42" for one page, "pp. 42–44" for a span (§10.2). */
export function pageReference(citation: Pick<CitationData, "page_start" | "page_end">): string | null {
  const { page_start: start, page_end: end } = citation;
  if (start === null) return null;
  if (end === null || end === start) return `p. ${start}`;
  return `pp. ${start}–${end}`;
}

function describe(token: CitationToken, citation: CitationData | null): string {
  const label = citationLabel(token);
  if (!citation) return `Citation ${label}`;
  const author = citation.source_authors[0] ?? citation.source_title;
  const page = pageReference(citation);
  return page ? `Citation ${label}: ${author}, ${page}` : `Citation ${label}: ${author}`;
}

function truncate(text: string, limit: number): string {
  return text.length <= limit ? text : `${text.slice(0, limit).trimEnd()}…`;
}

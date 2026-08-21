"use client";

import { useEffect, useRef, useState } from "react";
import { CLASSROOM } from "@/lib/copy/surfaces";
import type { RenderedTurn } from "@/lib/stream/types";
import { useUIStore } from "@/lib/state/ui";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { StreamedMarkdown } from "./streamed-markdown";

/**
 * The reading column (spec §6.2, §8.2).
 *
 * §8.2's scroll rule is the load-bearing part and it is the opposite of what
 * chat interfaces do: **the viewport does not auto-scroll.** A learner reading
 * paragraph two while paragraph four arrives keeps their place. What they get
 * instead is a "New content" affordance, and an opt-in preference for the
 * chat behaviour.
 */
export function Transcript({
  turns,
  streaming,
  children,
}: {
  turns: RenderedTurn[];
  streaming: boolean;
  /** Inline cards -- checks, resumption, degradation -- below the turns. */
  children?: React.ReactNode;
}) {
  const autoScroll = useUIStore((s) => s.autoScroll);
  const endRef = useRef<HTMLDivElement>(null);
  const [hasNewBelow, setHasNewBelow] = useState(false);

  // Watch a sentinel after the last turn. Intersection observation rather than
  // scroll maths: it survives the reading column growing, the window resizing,
  // and the browser's own scroll anchoring, none of which a computed offset
  // does.
  useEffect(() => {
    const node = endRef.current;
    if (!node) return;

    const observer = new IntersectionObserver(
      ([entry]) => setHasNewBelow(!(entry?.isIntersecting ?? true)),
      { rootMargin: "0px 0px -10% 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const lastLength = turns[turns.length - 1]?.text.length ?? 0;
  useEffect(() => {
    if (autoScroll) endRef.current?.scrollIntoView({ block: "end" });
  }, [autoScroll, lastLength]);

  return (
    <div className="relative">
      <div className="mx-auto flex max-w-[var(--container-measure)] flex-col gap-loose">
        {turns.map((turn) => (
          <TurnBlock key={turn.id} turn={turn} streaming={streaming} />
        ))}
        {children}
        <div ref={endRef} aria-hidden className="h-px" />
      </div>

      {hasNewBelow && streaming ? (
        <div className="pointer-events-none sticky bottom-normal flex justify-center">
          <Button
            variant="secondary"
            size="sm"
            className="pointer-events-auto bg-background/95 backdrop-blur-sm"
            onClick={() => endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" })}
          >
            {CLASSROOM.newContent}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function TurnBlock({ turn, streaming }: { turn: RenderedTurn; streaming: boolean }) {
  // A learner's own message is short and structural; running it through the
  // Markdown pipeline would let their asterisks become emphasis and their
  // backticks become code, neither of which they meant.
  if (turn.speaker === "learner") {
    return (
      <div className="turn-learner">
        <span className="sr-only">You asked: </span>
        <p className="whitespace-pre-wrap">{turn.text}</p>
      </div>
    );
  }

  return (
    <article
      className={cn(turn.speaker === "tutor" && "turn-tutor")}
      aria-label={turn.speaker === "tutor" ? "Tutor response" : "Lecture segment"}
    >
      <StreamedMarkdown
        text={turn.text}
        streaming={streaming && !turn.complete}
        artifactId={turn.artifactId}
      />

      {/* §8.3 step 4: "a subtle visual marker appears (thin horizontal rule)
          showing where the lecture paused". */}
      {turn.interruptedAt !== null ? (
        <div className="mt-normal flex items-center gap-tight" role="separator">
          <span className="h-px flex-1 bg-[var(--color-accent)]" />
          <span className="font-sans text-xs text-accent">{CLASSROOM.pausedHere}</span>
          <span className="h-px flex-1 bg-[var(--color-accent)]" />
        </div>
      ) : null}
    </article>
  );
}

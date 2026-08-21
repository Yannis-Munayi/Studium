"use client";

import { cn } from "@/lib/cn";
import { CorrectIcon, IncorrectIcon, PartialIcon, iconProps } from "./icons";

/**
 * Verdict indicator (spec §6.3, §11.1, §13.1).
 *
 * §13.1 (WCAG 1.4.1) forbids conveying information by colour alone, and §6.3
 * repeats it. The three signals -- colour, icon, and a word -- are welded
 * together here so no call site can render a verdict as a coloured dot: there
 * is no prop that would let it.
 */
export type VerdictKind = "correct" | "partial" | "incorrect";

const VERDICTS = {
  correct: {
    Icon: CorrectIcon,
    // §11.1's copy. The lead word does the work a colour would otherwise do.
    lead: "Nice.",
    label: "Correct",
    color: "var(--color-correct)",
  },
  partial: {
    Icon: PartialIcon,
    lead: "Close.",
    label: "Partly right",
    color: "var(--color-attention)",
  },
  incorrect: {
    Icon: IncorrectIcon,
    lead: "Not quite.",
    label: "Not yet",
    color: "var(--color-concern)",
  },
} as const;

export function Verdict({
  kind,
  feedback,
  className,
}: {
  kind: VerdictKind;
  feedback?: string;
  className?: string;
}) {
  const { Icon, lead, label, color } = VERDICTS[kind];
  return (
    <div
      className={cn("flex items-start gap-tight font-sans text-sm", className)}
      data-verdict={kind}
    >
      <span style={{ color }} className="mt-0.5 shrink-0">
        <Icon {...iconProps} />
      </span>
      <p className="text-ink">
        {/* The icon is aria-hidden, so the word carries the meaning for a
            screen reader as well as for a learner who cannot see the colour. */}
        <span className="sr-only">{label}. </span>
        <span style={{ color }} className="font-medium">
          {lead}
        </span>{" "}
        {feedback}
      </p>
    </div>
  );
}

/**
 * §6.4's journal status pill. Same rule: colour plus icon plus the word.
 *
 * `archived` and `resolved` share a glyph and differ in word and colour, which
 * is the case §13.1 is really about -- two states one glyph apart are only
 * distinguishable if something other than the glyph distinguishes them.
 */
export function StatusPill({ status }: { status: "open" | "partial" | "resolved" | "archived" }) {
  const styles = {
    open: { color: "var(--color-attention)", Icon: PartialIcon, label: "Open" },
    partial: { color: "var(--color-attention)", Icon: PartialIcon, label: "Partial" },
    resolved: { color: "var(--color-correct)", Icon: CorrectIcon, label: "Resolved" },
    archived: { color: "var(--color-text-secondary)", Icon: CorrectIcon, label: "Archived" },
  }[status];

  return (
    <span
      className="inline-flex items-center gap-1 rounded border border-line px-2 py-0.5 font-sans text-xs"
      style={{ color: styles.color }}
      data-status={status}
    >
      <styles.Icon size={12} strokeWidth={1.5} aria-hidden />
      {styles.label}
    </span>
  );
}

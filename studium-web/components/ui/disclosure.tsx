"use client";

import { useId, useState, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { ExpandIcon, denseIconProps } from "./icons";

/**
 * Progressive disclosure (spec §6.3, §11.2 — the hint mechanism).
 *
 * Native `<details>` would be fewer lines and would not do: §6.3 requires that
 * expanding a hint is *recorded* ("recorded to the trace so the reviewer can
 * see when hints were needed"), and `<details>` fires `toggle` after the fact
 * with no way to know whether it was the learner or a browser find-in-page
 * that opened it. An explicit button makes the signal a real interaction.
 */
export function Disclosure({
  label,
  children,
  onExpand,
  defaultOpen = false,
  className,
}: {
  label: string;
  children: ReactNode;
  /** Fired once, on the first expansion. §6.3's trace hook. */
  onExpand?: () => void;
  defaultOpen?: boolean;
  className?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [everOpened, setEverOpened] = useState(defaultOpen);
  const contentId = useId();

  return (
    <div className={cn("border-t border-line pt-tight", className)}>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={contentId}
        onClick={() => {
          const next = !open;
          setOpen(next);
          if (next && !everOpened) {
            setEverOpened(true);
            onExpand?.();
          }
        }}
        className={cn(
          "flex w-full items-center gap-tight py-1 text-left font-sans text-sm text-muted",
          "transition-colors duration-[var(--duration-hover)] hover:text-ink",
        )}
      >
        <ExpandIcon
          {...denseIconProps}
          className={cn(
            "transition-transform duration-[var(--duration-content)] ease-[var(--ease-studium)]",
            open && "rotate-180",
          )}
        />
        {label}
      </button>

      {/* Unmounted rather than hidden when closed: a hint left in the DOM is
          reachable by find-in-page and by a screen reader's browse mode, which
          hands out the answer to a learner who did not ask for it. */}
      {open ? (
        <div id={contentId} className="prose-reading pb-normal pl-6 pt-tight text-sm">
          {children}
        </div>
      ) : null}
    </div>
  );
}

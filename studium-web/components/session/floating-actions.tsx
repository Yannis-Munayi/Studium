"use client";

import * as Tooltip from "@radix-ui/react-tooltip";
import { buttonsFor, type Primitive } from "@/lib/copy/primitives";
import { useCommandPaletteStore } from "@/lib/state/palette";
import { useSessionStore } from "@/lib/state/session";
import { useUIStore } from "@/lib/state/ui";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { PaletteIcon, TimerIcon, denseIconProps } from "@/components/ui/icons";
import { InterruptButton } from "./interrupt-button";

/**
 * The bottom-right action group (spec §6.2, §9.2).
 *
 * §13.2 (WCAG 2.4.11, "Focus not obscured") is the constraint that shapes this:
 * a fixed element over the reading column can hide whatever the learner just
 * tabbed to. Two things handle it -- `scroll-margin-block-end` in `globals.css`
 * gives focused elements room to clear this group, and the group itself is
 * `pointer-events-none` at the container with its children opting back in, so
 * the gaps between buttons do not swallow clicks meant for the text beneath.
 */
export function FloatingActions({
  onInterrupt,
  onPrimitive,
  elapsedMinutes,
  costUsd,
}: {
  onInterrupt: () => void;
  onPrimitive: (primitive: Primitive) => void;
  elapsedMinutes: number;
  costUsd: number | null;
}) {
  const runtimeState = useSessionStore((s) => s.runtimeState);
  const openPalette = useCommandPaletteStore((s) => s.setOpen);
  const showCost = useUIStore((s) => s.showCost);
  const focusMode = useUIStore((s) => s.focusMode);

  const contextual = buttonsFor(runtimeState);

  return (
    <Tooltip.Provider delayDuration={300}>
      <div
        className={cn(
          "pointer-events-none fixed bottom-normal right-normal z-30 flex flex-col items-end gap-tight",
          // §19 open question 3's anticipated focus mode: the group fades but
          // stays reachable by keyboard, so hiding chrome never removes a
          // control (§3, §13.2).
          focusMode && "opacity-0 focus-within:opacity-100 hover:opacity-100",
          "transition-opacity duration-[var(--duration-surface)]",
        )}
      >
        <div
          className="pointer-events-auto flex items-center gap-tight rounded border border-line bg-background/95 px-2 py-1 font-sans text-xs text-muted backdrop-blur-sm"
          aria-label="Session status"
        >
          <TimerIcon {...denseIconProps} />
          <span>
            {/* A duration, not a countdown. §13.2 (WCAG 2.2.1) is about
                pressure; a clock counting down to a close creates it. */}
            <span className="sr-only">Elapsed: </span>
            {formatDuration(elapsedMinutes)}
          </span>
          {showCost && costUsd !== null ? (
            <>
              <span aria-hidden>·</span>
              <span>
                <span className="sr-only">Cost so far: </span>${costUsd.toFixed(2)}
              </span>
            </>
          ) : null}
        </div>

        {contextual.length > 0 ? (
          <div className="pointer-events-auto flex flex-wrap justify-end gap-tight">
            {contextual.map((primitive) => (
              <Button
                key={primitive.name}
                variant="secondary"
                size="sm"
                className="bg-background/95 backdrop-blur-sm"
                onClick={() => onPrimitive(primitive)}
              >
                {primitive.label}
              </Button>
            ))}
          </div>
        ) : null}

        <div className="pointer-events-auto flex items-center gap-tight">
          <Tooltip.Root>
            <Tooltip.Trigger asChild>
              <Button
                variant="secondary"
                className="bg-background/95 backdrop-blur-sm"
                onClick={() => openPalette(true)}
                aria-label="Open the primitive palette"
                aria-keyshortcuts="/ Control+K Meta+K"
              >
                <PaletteIcon {...denseIconProps} />
                <kbd className="font-mono text-xs">/</kbd>
              </Button>
            </Tooltip.Trigger>
            <Tooltip.Portal>
              <Tooltip.Content
                side="left"
                className="z-50 rounded border border-line bg-surface px-3 py-2 font-sans text-xs"
              >
                All primitives
              </Tooltip.Content>
            </Tooltip.Portal>
          </Tooltip.Root>

          <InterruptButton onInterrupt={onInterrupt} />
        </div>
      </div>
    </Tooltip.Provider>
  );
}

function formatDuration(minutes: number): string {
  if (minutes < 60) return `${Math.floor(minutes)}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${Math.floor(minutes % 60)}m`;
}

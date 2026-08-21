"use client";

import * as Tooltip from "@radix-ui/react-tooltip";
import { useEffect } from "react";
import { CLASSROOM } from "@/lib/copy/surfaces";
import { canInterrupt, useSessionStore } from "@/lib/state/session";
import { cn } from "@/lib/cn";
import { InterruptIcon, iconProps } from "@/components/ui/icons";

/**
 * The raise-your-hand control (spec §8.3).
 *
 * §3 calls interruption "a preserved control, not a hidden power-user feature",
 * so this is a visible, labelled button with a keyboard shortcut -- not a
 * gesture. Three states, all of them legible without colour: normal, armed
 * (filled, while a stream is interruptible), and disabled.
 */
export function InterruptButton({ onInterrupt }: { onInterrupt: () => void }) {
  const phase = useSessionStore((s) => s.phase);
  const runtimeState = useSessionStore((s) => s.runtimeState);
  const requested = useSessionStore((s) => s.interruptRequested);

  const armed = canInterrupt(phase, runtimeState);
  const disabled = !armed || requested;

  useSpaceToInterrupt(armed && !requested, onInterrupt);

  return (
    <Tooltip.Root>
      <Tooltip.Trigger asChild>
        <button
          type="button"
          onClick={onInterrupt}
          disabled={disabled}
          aria-keyshortcuts="Space"
          // The label changes with the state so a screen reader user learns
          // what the visual fill communicates, rather than hearing "Interrupt"
          // in a state where pressing it does nothing.
          aria-label={
            requested
              ? "Interrupt requested, finishing the sentence"
              : armed
                ? CLASSROOM.interrupt
                : "Interrupt — nothing is streaming"
          }
          data-armed={armed || undefined}
          className={cn(
            "inline-flex items-center gap-tight rounded border px-3 py-2 font-sans text-sm",
            "transition-colors duration-[var(--duration-hover)] ease-[var(--ease-studium)]",
            armed && !requested
              ? "border-transparent bg-[var(--color-accent-strong)] text-[var(--color-on-accent)]"
              : "border-line bg-background text-muted",
            requested && "border-[var(--color-accent)] text-accent",
            disabled && "cursor-not-allowed",
          )}
        >
          <InterruptIcon {...iconProps} />
          <span>{CLASSROOM.interrupt}</span>
          <kbd className="ml-1 rounded border border-current px-1 text-xs opacity-70">Space</kbd>
        </button>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content
          side="left"
          className="z-50 max-w-56 rounded border border-line bg-surface px-3 py-2 font-sans text-xs"
        >
          {CLASSROOM.interruptHint}
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

/**
 * §8.3's Space shortcut.
 *
 * "Space bar interrupts when the focus is anywhere in the document (not inside
 * a text input -- inside an input, Space types a space)." The exclusion has to
 * cover more than `<input>`: a `contenteditable`, a `<textarea>`, and any
 * button or checkbox, because Space is those controls' own activation key.
 * Stealing it from a button would break every other control on the surface to
 * make one control convenient.
 */
function useSpaceToInterrupt(enabled: boolean, onInterrupt: () => void): void {
  useEffect(() => {
    if (!enabled) return;

    function handler(event: KeyboardEvent) {
      if (event.key !== " " && event.code !== "Space") return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (isTypingContext(event.target)) return;

      event.preventDefault(); // otherwise the page scrolls
      onInterrupt();
    }

    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [enabled, onInterrupt]);
}

export function isTypingContext(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;

  // Both, not just the property: `isContentEditable` is computed from the
  // rendered tree and is `false` for a detached node. Every real keyboard event
  // comes from an attached one, so the property is what matters in the browser
  // -- but this predicate is also called directly, and a check that answers
  // differently depending on whether the node happens to be in the document is
  // a check nobody can reason about.
  if (target.isContentEditable) return true;
  const editable = target.getAttribute("contenteditable");
  if (editable !== null && editable !== "false") return true;

  const tag = target.tagName;
  if (tag === "TEXTAREA" || tag === "INPUT" || tag === "SELECT") return true;
  // Space activates these; intercepting it would break them.
  if (tag === "BUTTON" || tag === "A") return true;
  return target.getAttribute("role") === "textbox";
}

"use client";

import * as RadixDialog from "@radix-ui/react-dialog";
import { useEffect, useRef } from "react";
import { announce } from "@/lib/a11y/announcer";
import { PALETTE } from "@/lib/copy/surfaces";
import type { Primitive } from "@/lib/copy/primitives";
import { filterPrimitives, useCommandPaletteStore } from "@/lib/state/palette";
import { cn } from "@/lib/cn";
import { SearchIcon, iconProps } from "@/components/ui/icons";
import { isTypingContext } from "./interrupt-button";

/**
 * The primitive command palette (spec §9.1).
 *
 * Radix Dialog underneath, so focus trapping, restoration and Escape are not
 * hand-rolled (§13.2). The list is a `listbox` with `aria-activedescendant`
 * rather than roving focus: the input must keep focus while the arrow keys move
 * the selection, which is the pattern every command palette uses and the only
 * one where typing and navigating do not fight.
 */
export function CommandPalette({ onInvoke }: { onInvoke: (primitive: Primitive) => void }) {
  const open = useCommandPaletteStore((s) => s.open);
  const query = useCommandPaletteStore((s) => s.query);
  const focusedIndex = useCommandPaletteStore((s) => s.focusedIndex);
  const setOpen = useCommandPaletteStore((s) => s.setOpen);
  const setQuery = useCommandPaletteStore((s) => s.setQuery);
  const moveFocus = useCommandPaletteStore((s) => s.moveFocus);

  const matches = filterPrimitives(query);
  const focused = matches[focusedIndex] ?? null;
  const announcedFor = useRef<string | null>(null);

  useGlobalShortcuts(setOpen);

  // §9.1: "Screen reader announces ... the number of matching options as the
  // learner types." Guarded so it fires on a changed count, not per keystroke
  // -- a live region re-read on every character is unusable.
  useEffect(() => {
    if (!open) {
      announcedFor.current = null;
      return;
    }
    const key = `${matches.length}`;
    if (announcedFor.current === key) return;
    announcedFor.current = key;
    announce(PALETTE.countLabel(matches.length), "polite");
  }, [open, matches.length]);

  function invoke(primitive: Primitive) {
    setOpen(false);
    onInvoke(primitive);
  }

  return (
    <RadixDialog.Root open={open} onOpenChange={setOpen}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-40 bg-[color-mix(in_srgb,var(--color-text-primary)_35%,transparent)]" />
        <RadixDialog.Content
          aria-label="Tutorial primitives"
          className={cn(
            "fixed left-1/2 top-[15vh] z-50 w-[min(34rem,calc(100vw-2rem))] -translate-x-1/2",
            "overflow-hidden rounded border border-line bg-background",
          )}
        >
          <RadixDialog.Title className="sr-only">Tutorial primitives</RadixDialog.Title>
          <RadixDialog.Description className="sr-only">
            Search and run a tutorial primitive. Use the arrow keys to move, Enter to run.
          </RadixDialog.Description>

          <div className="flex items-center gap-tight border-b border-line px-normal py-3">
            <span className="text-muted">
              <SearchIcon {...iconProps} />
            </span>
            <input
              autoFocus
              type="text"
              role="combobox"
              aria-expanded
              aria-controls="palette-list"
              aria-autocomplete="list"
              {...(focused ? { "aria-activedescendant": `palette-option-${focused.name}` } : {})}
              value={query}
              placeholder={PALETTE.placeholder}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  moveFocus(1);
                } else if (event.key === "ArrowUp") {
                  event.preventDefault();
                  moveFocus(-1);
                } else if (event.key === "Enter" && focused) {
                  event.preventDefault();
                  invoke(focused);
                }
              }}
              className="flex-1 bg-transparent font-mono text-sm text-ink outline-none placeholder:text-muted"
            />
          </div>

          <ul id="palette-list" role="listbox" aria-label="Primitives" className="max-h-80 overflow-y-auto">
            {matches.length === 0 ? (
              <li className="px-normal py-3 font-sans text-sm text-muted">{PALETTE.empty}</li>
            ) : (
              matches.map((primitive, index) => (
                <li
                  key={primitive.name}
                  id={`palette-option-${primitive.name}`}
                  role="option"
                  aria-selected={index === focusedIndex}
                  // Not a <button>: an interactive element inside an option
                  // hides the option from the accessibility tree in several
                  // screen readers. The listbox owns keyboard activation and
                  // this handles the mouse.
                  onMouseDown={(event) => {
                    event.preventDefault(); // keep focus in the input
                    invoke(primitive);
                  }}
                  className={cn(
                    "cursor-pointer border-b border-line px-normal py-3 last:border-b-0",
                    index === focusedIndex && "bg-surface",
                  )}
                >
                  <div className="flex items-baseline justify-between gap-normal">
                    <span className="font-sans text-sm font-medium text-ink">{primitive.label}</span>
                    <kbd className="rounded border border-line px-1 font-mono text-xs text-muted">
                      {primitive.shortcut}
                    </kbd>
                  </div>
                  <p className="mt-0.5 font-sans text-xs text-muted">{primitive.description}</p>
                </li>
              ))
            )}
          </ul>
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}

/**
 * §9.1's `/` and Cmd/Ctrl+K.
 *
 * `/` is excluded inside a text field, for the same reason §8.3 excludes Space
 * there: a learner typing "and/or" into a question must get a slash. Cmd+K has
 * no such conflict and works everywhere.
 */
function useGlobalShortcuts(setOpen: (open: boolean) => void): void {
  useEffect(() => {
    function handler(event: KeyboardEvent) {
      const modifier = event.metaKey || event.ctrlKey;
      if (modifier && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen(true);
        return;
      }
      if (event.key === "/" && !modifier && !isTypingContext(event.target)) {
        event.preventDefault();
        setOpen(true);
      }
    }
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [setOpen]);
}

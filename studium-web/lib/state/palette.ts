/**
 * Command palette state (spec §14.2, `useCommandPaletteStore`).
 *
 * Ephemeral -- not persisted, reset on close. The filter lives here rather than
 * in the component because §9.1 requires the match count to be announced as the
 * learner types, and the announcer is outside the component tree.
 */
import { create } from "zustand";
import { PRIMITIVES, type Primitive } from "@/lib/copy/primitives";

interface PaletteState {
  open: boolean;
  query: string;
  /** Index into the filtered list, not into PRIMITIVES. */
  focusedIndex: number;

  setOpen: (open: boolean) => void;
  setQuery: (query: string) => void;
  moveFocus: (delta: number) => void;
  reset: () => void;
}

export const useCommandPaletteStore = create<PaletteState>((set, get) => ({
  open: false,
  query: "",
  focusedIndex: 0,

  setOpen: (open) => set(open ? { open } : { open: false, query: "", focusedIndex: 0 }),
  setQuery: (query) => set({ query, focusedIndex: 0 }),
  moveFocus: (delta) => {
    const count = filterPrimitives(get().query).length;
    if (count === 0) return;
    // Wraps, because a list this short with a hard stop at each end reads as
    // the arrow key having failed.
    set((s) => ({ focusedIndex: (s.focusedIndex + delta + count) % count }));
  },
  reset: () => set({ open: false, query: "", focusedIndex: 0 }),
}));

/**
 * §9.1's fuzzy match over name and description.
 *
 * Subsequence matching rather than a similarity score: "expdif" should find
 * "Explain differently", and a learner typing a prefix should never see the
 * list reorder underneath them, which is what scoring does. Ranking is by where
 * the match starts, so a label hit outranks a description hit.
 */
export function filterPrimitives(query: string): Primitive[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [...PRIMITIVES];

  const scored: Array<{ primitive: Primitive; rank: number }> = [];
  for (const primitive of PRIMITIVES) {
    const label = primitive.label.toLowerCase();
    const description = primitive.description.toLowerCase();

    if (label.startsWith(needle)) scored.push({ primitive, rank: 0 });
    else if (label.includes(needle)) scored.push({ primitive, rank: 1 });
    else if (isSubsequence(needle, label)) scored.push({ primitive, rank: 2 });
    else if (description.includes(needle)) scored.push({ primitive, rank: 3 });
  }

  return scored.sort((a, b) => a.rank - b.rank).map((s) => s.primitive);
}

function isSubsequence(needle: string, haystack: string): boolean {
  let index = 0;
  for (const character of haystack) {
    if (character === needle[index]) index += 1;
    if (index === needle.length) return true;
  }
  return needle.length === 0;
}

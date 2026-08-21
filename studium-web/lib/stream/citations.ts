/**
 * Citation marker parsing (spec §10.1).
 *
 * `[P3]` and `[P3-P5]` in streaming prose become `<Citation>` nodes. Pure and
 * separate from the renderer so the §17 Tier 1 case -- "given rendered text
 * with markers, produces correct component tree" -- is a data assertion rather
 * than a DOM one.
 */

/** §10.1's regex, anchored and grouped so the numbers come out. */
export const CITATION_PATTERN = /\[P(\d+)(?:-P(\d+))?\]/g;

export interface CitationToken {
  type: "citation";
  /** The literal marker as it appeared, e.g. `[P3-P5]`. */
  raw: string;
  /** First passage number. */
  from: number;
  /** Last passage number; equals `from` for a single marker. */
  to: number;
  /** Every marker this token resolves to: `[P3-P5]` -> `["P3","P4","P5"]`. */
  markers: string[];
}

export interface TextToken {
  type: "text";
  value: string;
}

export type CitationNode = TextToken | CitationToken;

/**
 * Split prose into text and citation tokens.
 *
 * A reversed range (`[P5-P3]`) is left as literal text rather than normalised.
 * Passage numbers are a contract with retrieval's `chunk_id` ordering, not a
 * formatting choice, so a marker that violates the ordering is a signal the
 * generator produced something wrong -- and quietly repairing it would hide
 * that from the review queue the backend maintains for exactly this.
 */
export function parseCitations(text: string): CitationNode[] {
  const nodes: CitationNode[] = [];
  let cursor = 0;

  CITATION_PATTERN.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = CITATION_PATTERN.exec(text)) !== null) {
    const [raw, fromRaw, toRaw] = match;
    const from = Number(fromRaw);
    const to = toRaw === undefined ? from : Number(toRaw);

    if (to < from) continue; // literal text; the loop's cursor is untouched

    if (match.index > cursor) {
      nodes.push({ type: "text", value: text.slice(cursor, match.index) });
    }

    const markers: string[] = [];
    for (let n = from; n <= to; n += 1) markers.push(`P${n}`);
    nodes.push({ type: "citation", raw, from, to, markers });
    cursor = match.index + raw.length;
  }

  if (cursor < text.length) nodes.push({ type: "text", value: text.slice(cursor) });
  return nodes;
}

/** The label §10.1 renders: `3` for a single marker, `3-5` for a range. */
export function citationLabel(token: CitationToken): string {
  return token.from === token.to ? String(token.from) : `${token.from}-${token.to}`;
}

/**
 * Whether a trailing fragment might still be growing into a marker.
 *
 * While a segment streams, `[P1` arrives before `]` does. Rendering the partial
 * as literal text and then swapping it for a superscript is a visible flicker
 * on every citation in every lecture, so the renderer holds back a suffix that
 * could still become one.
 */
export function partialMarkerLength(text: string): number {
  const match = /\[P?\d*(?:-P?\d*)?$/.exec(text);
  return match ? match[0].length : 0;
}

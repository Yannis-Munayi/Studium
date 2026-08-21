/**
 * Client-side sentence-boundary detection (spec §19, open question 1).
 *
 * The question the spec leaves open is whether the server's algorithm can be
 * reused client-side or whether the client should lean on server-provided
 * markers. **Answered here: reused, and deliberately not authoritative.**
 *
 * The server's detector (`orchestration/streaming.py::find_boundary`) decides
 * where a lecture actually stops when a learner interrupts. That decision has
 * consequences -- it is persisted as the resume anchor -- and it has two escape
 * hatches the client cannot have: an LLM fallback and a hard cut. This port
 * exists for one cosmetic job named in §8.2: knowing where the settled prose
 * ends so the trailing fragment can be styled as "still generating".
 *
 * So the port is faithful to the rule and silent about the escapes. If the two
 * disagree, the server wins and the client's only symptom is a fragment styled
 * as provisional for a moment longer than necessary. Encoding the escapes here
 * would make the client look authoritative about a decision it does not own.
 */

/**
 * Abbreviations whose trailing period is not a sentence end.
 *
 * Kept byte-identical to `ABBREVIATIONS` in `orchestration/streaming.py`. A
 * divergence here is a cosmetic flicker, not a correctness failure, but the two
 * lists drifting silently is how a cosmetic bug becomes unexplainable.
 */
export const ABBREVIATIONS: ReadonlySet<string> = new Set([
  "e.g", "i.e", "cf", "vs", "etc", "al", "fig", "eq", "ch", "sec", "no",
  "approx", "resp", "viz", "Dr", "Prof", "Mr", "Ms", "Mrs", "St", "Ph.D",
  "Thm", "Def", "Lem", "Cor", "Prop", "Ex",
]);

/**
 * `. ! ?` then closing quotes/brackets then whitespace, followed by something
 * that starts a new sentence. The lookahead set matches the server's:
 * capital, digit, opening bracket, or a LaTeX delimiter.
 */
const BOUNDARY = /([.!?])(["')\]]*)(\s+)(?=[A-Z0-9([$\\])/g;

/** Terminal punctuation at the very end of the buffer. */
const TRAILING = /[.!?]["')\]]*\s*$/;

const WORD_BEFORE = /([A-Za-z.]+)$/;

function precededByAbbreviation(text: string, periodIndex: number): boolean {
  const match = WORD_BEFORE.exec(text.slice(0, periodIndex));
  if (!match) return false;
  const token = (match[1] ?? "").replace(/\.+$/, "");
  if (ABBREVIATIONS.has(token)) return true;
  const parts = token.split(".");
  const last = parts[parts.length - 1];
  return last !== undefined && ABBREVIATIONS.has(last);
}

/**
 * Whether `index` sits inside a LaTeX math span.
 *
 * A period inside `$f(x) = 1.5$` is not a sentence end. Display spans are
 * counted first and removed before counting inline delimiters, so `$$` does not
 * register as two `$`.
 */
function insideMath(text: string, index: number): boolean {
  const before = text.slice(0, index);
  if (countOccurrences(before, "$$") % 2 === 1) return true;
  return countOccurrences(before.split("$$").join(""), "$") % 2 === 1;
}

function countOccurrences(haystack: string, needle: string): number {
  let count = 0;
  let at = haystack.indexOf(needle);
  while (at !== -1) {
    count += 1;
    at = haystack.indexOf(needle, at + needle.length);
  }
  return count;
}

/** Index just past the first real sentence end, or `null`. */
export function findBoundary(text: string): number | null {
  BOUNDARY.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = BOUNDARY.exec(text)) !== null) {
    const period = match.index;
    if (precededByAbbreviation(text, period) || insideMath(text, period)) continue;
    return match.index + match[0].length;
  }

  if (TRAILING.test(text)) {
    const stripped = text.replace(/\s+$/, "");
    const last = stripped.length - 1;
    if (last >= 0 && !precededByAbbreviation(stripped, last) && !insideMath(stripped, last)) {
      return text.length;
    }
  }
  return null;
}

/**
 * Split a streaming buffer into settled prose and the trailing fragment.
 *
 * `settled` is everything up to the last complete sentence; `pending` is what
 * follows it and gets the "still generating" treatment (§8.2). A buffer with no
 * boundary at all is entirely pending, which is correct for the first few
 * hundred milliseconds of a segment.
 */
export function splitAtLastBoundary(text: string): { settled: string; pending: string } {
  let cut = 0;
  let offset = 0;
  // Walk forward rather than searching backward: the backward search would need
  // its own abbreviation and math handling, and two implementations of one rule
  // is how they disagree.
  for (;;) {
    const next = findBoundary(text.slice(offset));
    if (next === null || next === 0) break;
    offset += next;
    cut = offset;
    if (offset >= text.length) break;
  }
  return { settled: text.slice(0, cut), pending: text.slice(cut) };
}

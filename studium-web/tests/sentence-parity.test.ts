import { describe, expect, it } from "vitest";
import { findBoundary } from "@/lib/stream/sentences";
import parity from "./fixtures/sentence-boundary-parity.json";

/**
 * The client's sentence detector against the server's (spec §19, question 1).
 *
 * `lib/stream/sentences.ts` is a port of
 * `backend/studium/orchestration/streaming.py::find_boundary`. A port that is
 * only *claimed* to match drifts the first time either side is edited, and the
 * symptom -- a fragment styled as provisional slightly too long -- is invisible
 * in review and nearly invisible in use.
 *
 * The fixture in `fixtures/sentence-boundary-parity.json` was produced by
 * running the Python function over these inputs and recording what it returned.
 * Regenerate it with `scripts/regenerate-parity-fixture.py` after any change to
 * either implementation; a diff in the fixture is the review signal.
 *
 * The two are allowed to disagree in exactly one direction, and the assertions
 * below encode it: the client may be *behind* the server (report no boundary
 * where the server found one), because the client's escapes -- the LLM fallback
 * and the hard cut -- are deliberately absent. It may never be *ahead*, because
 * that would mean styling text as settled that the server has not committed to.
 */
type ParityCase = { text: string; boundary: number | null };

const cases = parity as ParityCase[];

describe("sentence-boundary parity with the runtime", () => {
  it("has a fixture to check against", () => {
    // A fixture that silently emptied would make every assertion below vacuous.
    expect(cases.length).toBeGreaterThan(20);
  });

  it.each(cases.map((c) => [c.text, c.boundary] as const))(
    "agrees on %j",
    (text, expected) => {
      expect(findBoundary(text)).toBe(expected);
    },
  );

  it("never reports a boundary the server did not find", () => {
    // The one-directional rule, asserted as a property over the whole corpus
    // rather than case by case -- so a future fixture entry is covered by it
    // without anyone remembering to add the check.
    const ahead = cases.filter((c) => c.boundary === null && findBoundary(c.text) !== null);
    expect(ahead).toEqual([]);
  });
});

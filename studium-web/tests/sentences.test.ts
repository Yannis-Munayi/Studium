import { describe, expect, it } from "vitest";
import { findBoundary, splitAtLastBoundary, ABBREVIATIONS } from "@/lib/stream/sentences";

/**
 * §17 Tier 1: "Sentence-boundary detection on the client side (for interrupt
 * UI): given a stream of tokens, detects sentence completion correctly."
 *
 * The port's job is to agree with `orchestration/streaming.py::find_boundary`
 * on the rule. These cases are the same ones the server's own rule exists for:
 * abbreviations, decimals inside math, and a buffer that ends on a period with
 * no following sentence to anchor against.
 */
describe("findBoundary", () => {
  it("finds the end of a plain sentence", () => {
    const text = "Beta reduction rewrites an application. The next step is";
    const cut = findBoundary(text);
    expect(cut).not.toBeNull();
    expect(text.slice(0, cut!)).toBe("Beta reduction rewrites an application. ");
  });

  it("returns null while the first sentence is still arriving", () => {
    expect(findBoundary("Beta reduction rewrites an")).toBeNull();
  });

  it("treats a buffer ending on terminal punctuation as complete", () => {
    // The last sentence of a segment has no following capital to anchor on.
    expect(findBoundary("That is the whole rule.")).toBe("That is the whole rule.".length);
  });

  it.each([...ABBREVIATIONS].slice(0, 6))(
    "does not break after the abbreviation %s",
    (abbreviation) => {
      const text = `Consider ${abbreviation}. The identity function is next.`;
      const cut = findBoundary(text);
      // It must not cut immediately after the abbreviation's period.
      expect(cut).not.toBe(text.indexOf(`${abbreviation}.`) + abbreviation.length + 2);
    },
  );

  it("does not break on a decimal inside inline math", () => {
    const text = "We set $f(x) = 1.5$ and continue. Then we reduce.";
    const cut = findBoundary(text);
    expect(text.slice(0, cut!)).toBe("We set $f(x) = 1.5$ and continue. ");
  });

  it("does not break inside a display math block", () => {
    const text = "$$\\lambda x. x$$ is the identity. Next.";
    const cut = findBoundary(text);
    // The period inside `\lambda x. x` sits between the `$$` pair.
    expect(cut).toBeGreaterThan(text.indexOf("is the identity"));
  });

  it("breaks before a sentence starting with a digit or a bracket", () => {
    expect(findBoundary("First. 2 is next")).not.toBeNull();
    expect(findBoundary("First. (Second follows)")).not.toBeNull();
  });

  it("handles a closing quote after the terminal punctuation", () => {
    const text = 'He called it "reduction." The name stuck.';
    expect(findBoundary(text)).not.toBeNull();
  });
});

describe("splitAtLastBoundary", () => {
  it("puts complete sentences in settled and the fragment in pending", () => {
    const { settled, pending } = splitAtLastBoundary(
      "One complete sentence. A second one. And a frag",
    );
    expect(settled).toBe("One complete sentence. A second one. ");
    expect(pending).toBe("And a frag");
  });

  it("treats a buffer with no boundary as entirely pending", () => {
    const { settled, pending } = splitAtLastBoundary("Still writing the first");
    expect(settled).toBe("");
    expect(pending).toBe("Still writing the first");
  });

  it("leaves nothing pending when the buffer ends on a boundary", () => {
    const { settled, pending } = splitAtLastBoundary("All done here.");
    expect(settled).toBe("All done here.");
    expect(pending).toBe("");
  });

  it("terminates on adversarial input rather than looping", () => {
    // A degenerate buffer of nothing but terminators is the shape that would
    // spin a naive forward scan.
    const { settled } = splitAtLastBoundary(".".repeat(500));
    expect(settled.length).toBeLessThanOrEqual(500);
  });
});

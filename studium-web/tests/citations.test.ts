import { describe, expect, it } from "vitest";
import {
  citationLabel,
  parseCitations,
  partialMarkerLength,
  type CitationToken,
} from "@/lib/stream/citations";
import { rehypeCitations, CITATION_TAG } from "@/lib/stream/rehype-citations";
import type { Root } from "hast";

/**
 * §17 Tier 1: "Citation marker parser: given rendered text with markers,
 * produces correct component tree."
 */
describe("parseCitations", () => {
  it("splits a single marker out of surrounding prose", () => {
    const nodes = parseCitations("Reduction terminates [P3] under this rule.");

    expect(nodes).toHaveLength(3);
    expect(nodes[0]).toEqual({ type: "text", value: "Reduction terminates " });
    expect(nodes[1]).toMatchObject({ type: "citation", from: 3, to: 3, markers: ["P3"] });
    expect(nodes[2]).toEqual({ type: "text", value: " under this rule." });
  });

  it("expands a range into every marker it covers", () => {
    const [token] = parseCitations("[P3-P5]") as [CitationToken];

    expect(token.from).toBe(3);
    expect(token.to).toBe(5);
    expect(token.markers).toEqual(["P3", "P4", "P5"]);
    expect(citationLabel(token)).toBe("3-5");
  });

  it("labels a single marker with its bare number", () => {
    const [token] = parseCitations("[P7]") as [CitationToken];
    expect(citationLabel(token)).toBe("7");
  });

  it("handles several markers in one paragraph", () => {
    const nodes = parseCitations("First [P1] then [P2-P4] and [P9].");
    const citations = nodes.filter((n) => n.type === "citation");
    expect(citations).toHaveLength(3);
    expect(citations.map((c) => (c as CitationToken).markers.length)).toEqual([1, 3, 1]);
  });

  it("leaves a reversed range as literal text", () => {
    // Passage numbers follow retrieval's chunk_id ordering. A reversed range
    // means the generator produced something wrong, and quietly repairing it
    // would hide that from the review queue.
    const nodes = parseCitations("See [P5-P3].");
    expect(nodes.every((n) => n.type === "text")).toBe(true);
  });

  it("ignores things that only look like markers", () => {
    for (const text of ["[Q3]", "[P]", "[3]", "P3", "[[P3]"]) {
      const citations = parseCitations(text).filter((n) => n.type === "citation");
      if (text === "[[P3]") expect(citations).toHaveLength(1); // the inner one is real
      else expect(citations).toHaveLength(0);
    }
  });

  it("returns one text node for prose with no markers", () => {
    expect(parseCitations("Nothing cited here.")).toEqual([
      { type: "text", value: "Nothing cited here." },
    ]);
  });
});

describe("partialMarkerLength", () => {
  it.each([
    ["Reduction is [", 1],
    ["Reduction is [P", 2],
    ["Reduction is [P1", 3],
    ["Reduction is [P12-", 5],
    ["Reduction is [P12-P", 6],
    ["Reduction is [P12-P1", 7],
  ])("holds back %o as a possible marker", (text, expected) => {
    expect(partialMarkerLength(text)).toBe(expected);
  });

  it("holds back nothing once the marker closes", () => {
    expect(partialMarkerLength("Reduction is [P12]")).toBe(0);
  });

  it("holds back nothing in ordinary prose", () => {
    expect(partialMarkerLength("Reduction is complete")).toBe(0);
  });
});

describe("rehypeCitations", () => {
  function transform(tree: Root): Root {
    rehypeCitations()(tree);
    return tree;
  }

  it("rewrites markers inside a paragraph", () => {
    const tree: Root = {
      type: "root",
      children: [
        {
          type: "element",
          tagName: "p",
          properties: {},
          children: [{ type: "text", value: "Grounded [P1] claim." }],
        },
      ],
    };

    const paragraph = transform(tree).children[0];
    const children = (paragraph as { children: Array<{ type: string; tagName?: string }> }).children;
    expect(children.map((c) => c.tagName ?? c.type)).toEqual(["text", CITATION_TAG, "text"]);
  });

  it("leaves markers inside a code block alone", () => {
    // A lecture about citation formats will contain `[P1]` in a code fence, and
    // turning that into a live superscript would be wrong twice over.
    const tree: Root = {
      type: "root",
      children: [
        {
          type: "element",
          tagName: "pre",
          properties: {},
          children: [
            {
              type: "element",
              tagName: "code",
              properties: {},
              children: [{ type: "text", value: "cite [P1] here" }],
            },
          ],
        },
      ],
    };

    transform(tree);
    const pre = tree.children[0] as { children: Array<{ children: Array<{ type: string }> }> };
    expect(pre.children[0]?.children).toEqual([{ type: "text", value: "cite [P1] here" }]);
  });

  it("leaves markers inside KaTeX output alone", () => {
    const tree: Root = {
      type: "root",
      children: [
        {
          type: "element",
          tagName: "span",
          properties: { className: ["katex"] },
          children: [{ type: "text", value: "[P2]" }],
        },
      ],
    };

    transform(tree);
    const span = tree.children[0] as { children: Array<{ type: string }> };
    expect(span.children).toEqual([{ type: "text", value: "[P2]" }]);
  });
});

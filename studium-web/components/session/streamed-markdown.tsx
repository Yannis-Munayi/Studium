"use client";

import { memo, useMemo } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { partialMarkerLength } from "@/lib/stream/citations";
import { CITATION_TAG, rehypeCitations } from "@/lib/stream/rehype-citations";
import { splitAtLastBoundary } from "@/lib/stream/sentences";
import { cn } from "@/lib/cn";
import { Citation } from "./citation";

/**
 * Markdown for text that is still arriving (spec §8.2).
 *
 * Three problems the plain renderer does not have, all caused by the text being
 * incomplete at render time:
 *
 * 1. **Unclosed blocks.** remark closes an open fence or an open emphasis
 *    implicitly, so the layout does not break. What it cannot do is know the
 *    block is provisional, which §8.2 asks be shown -- that is the
 *    settled/pending split below.
 * 2. **Half-arrived citation markers.** `[P1` would render as literal text and
 *    become a superscript one token later, flickering once per citation per
 *    lecture. Held back until the `]` arrives (§10.1).
 * 3. **Half-arrived math.** §8.2 rules explicitly: literal until the closing
 *    delimiter, then it re-renders, and the flash is accepted. Nothing to do
 *    but let it happen -- the alternative shows nothing for longer.
 */

export interface StreamedMarkdownProps {
  text: string;
  /** False once the `end` chunk lands; disables the provisional treatment. */
  streaming: boolean;
  /** Resolves `[Pn]` markers. Null until the runtime supplies one (F3). */
  artifactId: string | null;
  className?: string;
}

export const StreamedMarkdown = memo(function StreamedMarkdown({
  text,
  streaming,
  artifactId,
  className,
}: StreamedMarkdownProps) {
  const { body, tail } = useMemo(() => split(text, streaming), [text, streaming]);

  return (
    <div className={cn("prose-reading", className)}>
      <MarkdownBlock text={body} artifactId={artifactId} />
      {tail ? (
        <div className="still-generating" data-testid="still-generating">
          <MarkdownBlock text={tail} artifactId={artifactId} />
        </div>
      ) : null}
    </div>
  );
});

/**
 * Divide the buffer into settled prose and the provisional tail.
 *
 * While streaming, the tail is everything after the last sentence boundary,
 * minus any suffix that could still be growing into a citation marker. Once the
 * stream ends there is no tail: all of it is settled.
 */
export function split(text: string, streaming: boolean): { body: string; tail: string } {
  if (!streaming) return { body: text, tail: "" };

  const partial = partialMarkerLength(text);
  const safe = partial > 0 ? text.slice(0, text.length - partial) : text;

  const { settled, pending } = splitAtLastBoundary(safe);
  return { body: settled, tail: pending };
}

function MarkdownBlock({ text, artifactId }: { text: string; artifactId: string | null }) {
  const components = useMemo(() => buildComponents(artifactId), [artifactId]);

  return (
    <Markdown
      remarkPlugins={[remarkGfm, remarkMath]}
      // Order matters: KaTeX first, so the citation pass sees math as elements
      // it knows to skip rather than as raw `$...$` text it might rewrite.
      rehypePlugins={[rehypeKatex, rehypeCitations]}
      components={components}
    >
      {text}
    </Markdown>
  );
}

/**
 * The component map, including the custom citation tag.
 *
 * `Components` is typed to known HTML tag names, and `studium-citation` is not
 * one. The cast is confined to this function and the props it receives are
 * validated before use, so an unexpected shape renders nothing rather than
 * throwing inside a stream.
 */
function buildComponents(artifactId: string | null): Components {
  const citation = (props: Record<string, unknown>) => {
    const from = Number(props["data-from"]);
    const to = Number(props["data-to"]);
    const raw = String(props["data-raw"] ?? "");
    if (!Number.isFinite(from) || !Number.isFinite(to)) return null;

    const markers: string[] = [];
    for (let n = from; n <= to; n += 1) markers.push(`P${n}`);
    return (
      <Citation token={{ type: "citation", raw, from, to, markers }} artifactId={artifactId} />
    );
  };

  return {
    [CITATION_TAG]: citation,

    // §13.1: headings inside streamed content start at h3. The surface owns h1
    // and h2; a segment emitting `# Title` must not introduce a second h1 or
    // skip a level.
    h1: ({ children }) => <h3>{children}</h3>,
    h2: ({ children }) => <h3>{children}</h3>,
    h3: ({ children }) => <h4>{children}</h4>,

    a: ({ children, href }) => (
      <a
        href={href}
        className="text-accent underline"
        // Model output is not a trusted source of link targets.
        rel="noopener noreferrer nofollow"
        target="_blank"
      >
        {children}
      </a>
    ),
  } as Components;
}

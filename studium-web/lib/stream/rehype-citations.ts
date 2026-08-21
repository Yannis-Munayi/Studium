/**
 * A rehype plugin that turns `[Pn]` text into citation elements (spec §10.1).
 *
 * Doing this in the AST rather than by post-processing rendered text is what
 * keeps it correct in the cases that matter: a marker inside a code fence or
 * inside a KaTeX span must stay literal, and only the tree knows which nodes
 * those are. A regex over the finished HTML cannot tell `[P3]` in prose from
 * `[P3]` in a code example about citation formats.
 */
import type { Element, Root, RootContent, Text } from "hast";
import { parseCitations } from "./citations";

/** Tag name the renderer maps to the `<Citation>` component. */
export const CITATION_TAG = "studium-citation";

/**
 * Node types whose text is literal and must not be rewritten.
 *
 * `code` and `pre` are the §8.2 code-block case. The KaTeX ones matter because
 * rehype-katex leaves annotation nodes carrying the original TeX, and a `[P1]`
 * inside a math annotation is not a citation.
 */
const LITERAL_PARENTS = new Set(["code", "pre", "math", "annotation", "semantics"]);

export function rehypeCitations() {
  return function transform(tree: Root): void {
    visit(tree, null);
  };
}

function visit(node: Root | Element, parentTag: string | null): void {
  if (parentTag && LITERAL_PARENTS.has(parentTag)) return;
  // rehype-katex output is marked with a `katex` class rather than a tag.
  if (isElement(node) && hasKatexClass(node)) return;

  const children: RootContent[] = [];
  let changed = false;

  for (const child of node.children) {
    if (child.type === "text") {
      const replacement = splitTextNode(child);
      if (replacement) {
        children.push(...replacement);
        changed = true;
        continue;
      }
      children.push(child);
      continue;
    }

    if (child.type === "element") visit(child, child.tagName);
    children.push(child);
  }

  if (changed) node.children = children;
}

function splitTextNode(node: Text): RootContent[] | null {
  const parts = parseCitations(node.value);
  if (parts.length === 1 && parts[0]?.type === "text") return null;

  return parts.map<RootContent>((part) =>
    part.type === "text"
      ? { type: "text", value: part.value }
      : {
          type: "element",
          tagName: CITATION_TAG,
          properties: {
            "data-raw": part.raw,
            "data-from": String(part.from),
            "data-to": String(part.to),
          },
          children: [],
        },
  );
}

function isElement(node: Root | Element): node is Element {
  return (node as Element).type === "element";
}

function hasKatexClass(node: Element): boolean {
  const className: unknown = node.properties?.["className"];
  if (Array.isArray(className)) return className.some((c) => String(c).startsWith("katex"));
  return typeof className === "string" && className.startsWith("katex");
}

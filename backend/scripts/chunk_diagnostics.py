"""Run the chunker against real PDF-extracted text and report what happens.

Answers retrieval §19's open question 1 -- whether the 400/600-token targets
survive contact with real sources -- and surfaces the extraction artefacts the
break-preference hierarchy will have to cope with.

**This is a diagnostic, not the ingestion pipeline.** Retrieval §2 puts
PDF-to-text extraction explicitly out of scope: subsystem 5 owns it, and the
extractor here is deliberately crude, existing only to produce input for
:mod:`studium.retrieval.chunking` so its behaviour on real prose can be
measured. Nothing it produces is written to the database. When subsystem 5
ships a real extractor, this script should point at that instead of at pypdf.

    python scripts/chunk_diagnostics.py material/2_lambda_calculus/gjm_lambda_book.pdf
    python scripts/chunk_diagnostics.py <pdf> --pages 30-60 --sample 3

``pypdf`` is an optional diagnostic dependency (the ``diagnostics`` extra), not
a retrieval runtime one -- importing it here rather than in the package keeps
that boundary visible.

**What the first run of this script found** (Michaelson, 241 pages, 19 August
2026). Two chunker defects, both since fixed and pinned by regression test in
``tests/retrieval/test_chunking.py::TestRealCorpusRegressions``:

* 7.6% of chunks exceeded the 600-token maximum. The 15% overlap is prepended
  *after* the body has been sized, so it escaped the size check entirely.
* 716 typographic ligatures reached the chunk text. Postgres tokenises
  "de<fi>nition" and "definition" differently, so those chunks were invisible
  to keyword search for words they plainly contained.

And one finding about the measurement itself, which is why §19's open question 1
is still open: three successive extraction heuristics over the same PDF, feeding
the same chunker, produced median chunk sizes of 440, 113 and 124 tokens. The
distribution is dominated by extraction quality, not by the chunker's
parameters, so the 400/600 targets cannot be tuned against anything until
subsystem 5's extractor exists. Recorded as SPEC_DEBT.md SD3.
"""

from __future__ import annotations

import argparse
import collections
import pathlib
import re
import statistics
import sys
import unicodedata

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from studium.retrieval.chunking import (  # noqa: E402
    MAX_TOKENS,
    MIN_BODY_TOKENS,
    TARGET_TOKENS,
    TARGET_WINDOW,
    Block,
    chunk_blocks,
)

# --- artefact detection ----------------------------------------------------
#
# Each pattern is something PDF text extraction produces that clean source text
# would not. They are counted rather than repaired: the point of this script is
# to report what subsystem 5's extractor will have to handle, and silently
# fixing them here would hide the requirement.

ARTEFACTS: dict[str, re.Pattern[str]] = {
    # "reduc-\ntion" -- a line break inside a word, from justified typesetting.
    "hyphenated line break": re.compile(r"[a-z]-\n[a-z]"),
    # Ligatures that survive extraction as single codepoints.
    "ligature": re.compile(r"[ﬀ-ﬆ]"),
    # A line ending mid-sentence with no punctuation, then a capital: the usual
    # signature of two-column text reflowed into one stream in the wrong order.
    "suspicious line join": re.compile(r"[a-z,]\n[A-Z][a-z]"),
    # Page furniture that lands in the text stream.
    "bare page number line": re.compile(r"\n\s*\d{1,3}\s*\n"),
    # Runs of spaces from column alignment.
    "column whitespace run": re.compile(r"\S {3,}\S"),
    # Replacement characters: the extractor could not map a glyph at all.
    "unmapped glyph": re.compile(r"�"),
    # Control characters other than newline/tab.
    "control character": re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]"),
}


def extract_pages(path: pathlib.Path, pages: slice) -> list[tuple[int, str]]:
    try:
        import pypdf
    except ImportError:
        sys.exit(
            "pypdf is not installed. It is a diagnostic-only dependency:\n"
            "    pip install -e '.[diagnostics]'"
        )

    reader = pypdf.PdfReader(str(path))
    total = len(reader.pages)
    indices = range(*pages.indices(total))
    out = []
    for i in indices:
        try:
            out.append((i + 1, reader.pages[i].extract_text() or ""))
        except Exception as exc:  # noqa: BLE001 -- a bad page is a finding
            print(f"  ! page {i + 1} failed to extract: {exc}")
    return out


#: "4.3. Recursion through definitions?" / "2. The lambda calculus". Requires a
#: capitalised word after the number and no terminal full stop, or every
#: numbered list item and every prose line opening with a digit ("4 times even
#: though...") is read as a section heading. The first version of this pattern
#: did exactly that and produced 994 headings out of 1,873 chunks.
_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s+[A-Z][^.!?]{2,70}$")
#: "- 53 -" page furniture.
_PAGE_FURNITURE = re.compile(r"^\s*-?\s*\d{1,3}\s*-?\s*$")
#: Strong code signals only. A permissive version of this (any line opening
#: with if/then/else/paren/dash) split prose into per-line fragments and drove
#: the median chunk to 113 tokens.
_CODEISH = re.compile(
    r"^\s*(def|rec|val|datatype|fun|type)\s+\S|^\s*λ|=>|^\s*[A-Z_]{3,}\s+[A-Z_]"
)


def _paragraphs(text: str) -> list[str]:
    """Recover paragraphs from single-newline-separated extracted text.

    pypdf emits one ``\\n`` per *typeset line*, with nothing distinguishing a
    wrapped line from a paragraph break -- this book contains zero blank lines
    across 241 pages. Feeding that to the chunker as one block per page is what
    the first run of this script did, and it measures the extractor rather than
    the chunker.

    The join rule: a line continues the previous one when the previous line does
    not end in sentence punctuation and the current line starts lowercase. That
    is the standard heuristic and it is wrong at the margins -- a paragraph
    genuinely starting with a lowercase identifier gets merged backwards. It is
    good enough to make the size measurement meaningful, and getting it properly
    right is subsystem 5's job with a better extractor.
    """
    paragraphs: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            paragraphs.append(" ".join(current).strip())
            current.clear()

    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip() or _PAGE_FURNITURE.match(line):
            flush()
            continue

        # Code and headings stand alone; joining them into prose would produce
        # a chunk that is half program text presented as exposition.
        if _HEADING.match(line.strip()) or _CODEISH.match(line):
            flush()
            paragraphs.append(line.strip())
            continue

        if current:
            previous = current[-1]
            continues = not previous.rstrip().endswith((".", "!", "?", ":", ";")) and (
                line[:1].islower() or line[:1] in ",)"
            )
            if not continues:
                flush()

        # Repair a hyphenated line break before the join, or "reduc- tion"
        # survives into the chunk text and into the tsvector.
        if current and current[-1].endswith("-") and line[:1].islower():
            current[-1] = current[-1][:-1] + line
            continue

        current.append(line)

    flush()
    return [p for p in paragraphs if p]


def blocks_from_pages(pages: list[tuple[int, str]]) -> list[Block]:
    """Split extracted text into paragraph blocks, tracking a crude section path."""
    blocks: list[Block] = []
    path: list[str] = []

    for page_number, text in pages:
        for para in _paragraphs(text):
            heading = _HEADING.match(para)
            if heading:
                depth = heading.group(1).count(".") + 1
                del path[depth - 1:]
                path.append(para)
                blocks.append(
                    Block(
                        text=para,
                        section_path=tuple(path),
                        kind="heading",
                        page=page_number,
                        starts_section=True,
                    )
                )
                continue

            blocks.append(
                Block(text=para, section_path=tuple(path), page=page_number)
            )
    return blocks


def find_artefacts(pages: list[tuple[int, str]]) -> dict[str, int]:
    counts: collections.Counter[str] = collections.Counter()
    for _, text in pages:
        for name, pattern in ARTEFACTS.items():
            counts[name] += len(pattern.findall(text))
    return dict(counts)


def non_ascii_profile(pages: list[tuple[int, str]]) -> list[tuple[str, int, str]]:
    counter: collections.Counter[str] = collections.Counter()
    for _, text in pages:
        counter.update(ch for ch in text if ord(ch) > 127)
    return [
        (ch, n, unicodedata.name(ch, "UNNAMED"))
        for ch, n in counter.most_common(12)
    ]


def report(path: pathlib.Path, pages: slice, sample: int) -> int:
    print(f"\n{'=' * 72}\n{path.name}\n{'=' * 72}")

    extracted = extract_pages(path, pages)
    if not extracted:
        print("no pages extracted")
        return 1

    chars = sum(len(t) for _, t in extracted)
    empty = sum(1 for _, t in extracted if not t.strip())
    print(f"\npages extracted : {len(extracted)}  ({empty} empty)")
    print(f"characters      : {chars:,}")

    print("\n--- extraction artefacts ---")
    artefacts = find_artefacts(extracted)
    if any(artefacts.values()):
        for name, count in sorted(artefacts.items(), key=lambda kv: -kv[1]):
            if count:
                per_page = count / len(extracted)
                print(f"  {name:24s} {count:6,}   ({per_page:.1f}/page)")
    else:
        print("  none detected")

    print("\n--- non-ASCII characters (top 12) ---")
    for ch, n, name in non_ascii_profile(extracted):
        print(f"  U+{ord(ch):04X} {n:6,}  {name}")

    blocks = blocks_from_pages(extracted)
    chunks = chunk_blocks(blocks)
    body = [c for c in chunks if c.chunk_type == "body"]

    print(f"\n--- chunking ---\nblocks in : {len(blocks)}\nchunks out: {len(chunks)}")

    kinds = collections.Counter(c.chunk_type for c in chunks)
    for kind, count in kinds.most_common():
        print(f"  {kind:16s} {count:5d}")

    if not body:
        print("\nno body chunks produced -- nothing to measure")
        return 1

    sizes = sorted(c.token_count for c in body)
    over_max = [s for s in sizes if s > MAX_TOKENS]
    under_min = [s for s in sizes if s < MIN_BODY_TOKENS]
    in_window = [s for s in sizes if TARGET_WINDOW[0] <= s <= TARGET_WINDOW[1]]

    def pct(n: int) -> str:
        return f"{100 * n / len(sizes):5.1f}%"

    print(f"\n--- body chunk size distribution (target {TARGET_TOKENS}, max {MAX_TOKENS}) ---")
    print(f"  count            {len(sizes)}")
    print(f"  mean             {statistics.mean(sizes):.0f}")
    print(f"  median           {statistics.median(sizes):.0f}")
    print(f"  min / max        {sizes[0]} / {sizes[-1]}")
    print(f"  p10 / p90        {sizes[len(sizes) // 10]} / {sizes[9 * len(sizes) // 10]}")
    print(f"  in 350-450 band  {len(in_window):5d}  {pct(len(in_window))}")
    print(f"  over max ({MAX_TOKENS})   {len(over_max):5d}  {pct(len(over_max))}")
    print(f"  under min ({MIN_BODY_TOKENS})    {len(under_min):5d}  {pct(len(under_min))}")

    print("\n--- boundary quality ---")
    clean = sum(1 for c in body if c.text.rstrip().endswith((".", "!", "?", ":", ";")))
    midword = sum(1 for c in body if re.search(r"[a-z]$", c.text.rstrip()))
    print(f"  ends at sentence punctuation  {clean:5d}  {pct(clean)}")
    print(f"  ends mid-word (lowercase)     {midword:5d}  {pct(midword)}")

    if sample:
        print(f"\n--- {sample} sample chunks ---")
        step = max(1, len(body) // (sample + 1))
        for chunk in body[step::step][:sample]:
            print(f"\n  [{chunk.chunk_index}] {chunk.token_count} tokens  "
                  f"p{chunk.page_start}  {chunk.section_path[-1:] or ['(no section)']}")
            print(f"  ...{chunk.text[:200].strip()!r}")
            print(f"  ENDS: {chunk.text[-80:].strip()!r}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=pathlib.Path)
    parser.add_argument(
        "--pages", default=":", help="page range, 1-based inclusive, e.g. 30-60"
    )
    parser.add_argument("--sample", type=int, default=3)
    args = parser.parse_args()

    if not args.pdf.exists():
        sys.exit(f"no such file: {args.pdf}")

    if args.pages == ":":
        pages = slice(None)
    else:
        start, _, end = args.pages.partition("-")
        pages = slice(int(start) - 1, int(end) if end else int(start))

    return report(args.pdf, pages, args.sample)


if __name__ == "__main__":
    raise SystemExit(main())

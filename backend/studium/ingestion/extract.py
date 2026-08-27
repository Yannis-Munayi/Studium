"""PDF text extraction (ingestion §7).

The highest-leverage decision in this subsystem. Everything downstream operates
on what the extractor produced: a bad extractor makes the chunker's
break-preference hierarchy fit noise, makes the embedder embed garbage, and
makes retrieval return nothing useful. The retrieval build measured three
extractors over one PDF and got chunk-size medians of 440, 113 and 124 tokens
-- a three-fold spread from the same bytes, before any downstream parameter was
touched.

That is also why the parameters downstream are not tuned against one
extractor's output. Retuning the chunker to fit pdfplumber's artefacts would
fit it to noise, and the fit would silently break the day the extractor changed.

**pdfplumber is the default** (§7.1): pure Python with no ML dependency, so
ingestion latency is bounded by CPU rather than model throughput and there is
no GPU, no model download, and no per-page inference cost. It handles the
textbook and paper material the corpus is built from adequately -- not
perfectly, which nothing does.

What it does not do, stated plainly because these are the failure modes a
reviewer will meet: scanned PDFs come back empty (OCR is v2, and §14 rejects
them cleanly rather than ingesting nothing); mathematics comes back as jumbled
character runs that the normalizer cannot repair; and heading detection is a
font-size heuristic that misses roughly 15% of section boundaries on real
documents.
"""

from __future__ import annotations

import asyncio
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

#: §6.3: below this, extraction is treated as having failed. A PDF that yields
#: 40 tokens is a scan, a cover sheet, or a broken file -- in every case there
#: is nothing to chunk, and letting it through produces a source that is
#: "ingested" and retrieves nothing.
MIN_DOCUMENT_TOKENS = 100

#: A page's text is a heading candidate when its characters run this much
#: larger than the document's body size. Multiplicative rather than absolute:
#: body text is 9pt in one book and 11pt in another, and an absolute threshold
#: tuned on one silently detects nothing on the other.
HEADING_SIZE_RATIO = 1.15
#: Headings are short. Without this a large-print epigraph becomes a section.
MAX_HEADING_CHARS = 120

WORD_SPLIT_TOLERANCE = 2

# --- why there is no paragraph detection here ------------------------------
#
# `extract_text` joins lines with single newlines and never emits a blank line,
# so a page arrives as one block and retrieval §7's preference-2 break point (a
# blank line) never fires. Chunks end up page-shaped rather than idea-shaped.
# That is a real defect and it is recorded in SPEC_DEBT (SD7) rather than fixed
# here, because both available fixes were tried against the real corpus and
# both made it worse:
#
#   * First-line indent as a paragraph marker turned the Michaelson book's 241
#     pages into 6,619 paragraphs and dropped the median chunk to 7 tokens.
#     Typeset academic material indents constantly -- centred equations,
#     displayed quotations, list items, hanging indents.
#   * A vertical gap wider than the page's modal line spacing (tried at 1.35x,
#     1.6x and 2.0x) took the same book from a 409-token median to 27, because
#     displayed lambda expressions carry extra leading and each became its own
#     paragraph.
#
# Left alone, the page-as-block behaviour yields a 409-token median against a
# 400-token target with quartiles at 334 and 480 -- the best distribution of
# anything measured. A real fix needs to distinguish a paragraph break from a
# display-math gap, which needs font and indent context the current pass does
# not carry.


class ExtractionError(RuntimeError):
    """Extraction failed in a way that is worth a review queue row."""


class ExtractorUnavailable(ExtractionError):
    """The requested extractor is not installed.

    Distinct from :class:`ExtractionError` because it is an operator problem,
    not a document problem: retrying will not help and the source is fine.
    """


@dataclass(frozen=True, slots=True)
class ExtractedTable:
    """One table as a grid of cell strings. ``None`` is an empty cell."""

    rows: tuple[tuple[str | None, ...], ...] = ()

    def as_text(self) -> str:
        """A tab-separated rendering, for the chunker to treat as one block.

        Tables reach the corpus as text or not at all -- ``source_chunks`` has
        no structured column for them. Tab-separated keeps the column
        boundaries legible to a reader and to a model, which pipe-and-dash
        Markdown does not do any better and at three times the token count.
        """
        return "\n".join(
            "\t".join((cell or "").strip() for cell in row) for row in self.rows
        )


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    page_number: int
    text: str
    #: Path from the top-level heading down, as of this page.
    section_hierarchy: tuple[str, ...] = ()
    tables: tuple[ExtractedTable, ...] = ()
    #: 0-1. The extractor's own confidence in this page. pdfplumber exposes
    #: none, so it reports 1.0 -- which asserts nothing about quality, only
    #: that the extractor had no opinion. Read it with ``extractor_version``.
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class ExtractedContent:
    pages: tuple[ExtractedPage, ...] = ()
    document_metadata: dict[str, str] = field(default_factory=dict)
    #: "pdfplumber/0.11.10". Written to ``sources.extractor_version``.
    extractor_version: str = ""

    @property
    def text(self) -> str:
        return "\n".join(page.text for page in self.pages)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@runtime_checkable
class Extractor(Protocol):
    """§7.2. What an extractor has to provide to be pluggable.

    ``extract`` is async because the pipeline is, not because extraction is
    I/O-bound. A pure-Python extractor is CPU work and blocks the event loop
    unless it hops to a thread -- which is what :class:`PdfPlumberExtractor`
    does, and what any implementation added here must also do.
    """

    name: str
    version: str

    async def extract(self, pdf_path: Path) -> ExtractedContent: ...


@dataclass(frozen=True, slots=True)
class PdfPlumberExtractor:
    """§7.1. The default extractor."""

    name: str = "pdfplumber"

    @property
    def version(self) -> str:
        return f"{self.name}/{_pdfplumber_version()}"

    async def extract(self, pdf_path: Path) -> ExtractedContent:
        # pdfplumber is synchronous and CPU-bound. Running it inline would
        # block the event loop the agent runtime shares (§4: ingestion workers
        # run in the same loop), so a 400-page book would stall every live
        # session for the length of the extraction.
        return await asyncio.to_thread(self._extract_sync, pdf_path)

    def _extract_sync(self, pdf_path: Path) -> ExtractedContent:
        pdfplumber = _import_pdfplumber()

        path = Path(pdf_path)
        if not path.exists():
            raise ExtractionError(f"no file at {path}")

        pages: list[ExtractedPage] = []
        metadata: dict[str, str] = {}
        hierarchy: list[str] = []

        with pdfplumber.open(str(path)) as pdf:
            metadata = _document_metadata(pdf)
            body_size = _body_font_size(pdf)

            for index, page in enumerate(pdf.pages, start=1):
                text = _page_text_with_paragraphs(page)
                headings = _headings_on_page(page, body_size)
                # The hierarchy is carried forward across pages: a page in the
                # middle of section 3.2 has no heading of its own and must
                # still be filed under it, or its chunks land at the document
                # root and section_path stops meaning anything.
                for heading in headings:
                    hierarchy = _apply_heading(hierarchy, heading)

                tables = tuple(
                    ExtractedTable(rows=tuple(tuple(row) for row in table))
                    for table in (page.extract_tables() or [])
                    if table
                )

                pages.append(
                    ExtractedPage(
                        page_number=index,
                        text=text,
                        section_hierarchy=tuple(hierarchy),
                        tables=tables,
                        # pdfplumber reports no per-page confidence. 1.0 is
                        # "no opinion", not "verified good" -- see the field.
                        confidence=1.0,
                    )
                )

        return ExtractedContent(
            pages=tuple(pages),
            document_metadata=metadata,
            extractor_version=self.version,
        )


def _import_pdfplumber() -> Any:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover -- environment-dependent
        raise ExtractorUnavailable(
            "pdfplumber is not installed; `pip install -e '.[ingestion]'`"
        ) from exc
    return pdfplumber


def _pdfplumber_version() -> str:
    try:
        return _import_pdfplumber().__version__
    except ExtractorUnavailable:
        return "unavailable"


def _plain_text(page: Any) -> str:
    """``extract_text`` at the tuned word-split tolerance.

    Every fallback path goes through here rather than calling
    ``page.extract_text()`` directly. Calling it bare would quietly reinstate
    pdfplumber's default tolerance of 3 on exactly the pages the geometry pass
    could not handle -- so a document would extract correctly except for its
    short pages, which is worse than a uniform failure because it looks fine.
    """
    return page.extract_text(x_tolerance=WORD_SPLIT_TOLERANCE) or ""


def _page_text_with_paragraphs(page: Any) -> str:
    """Page text at the tuned word-split tolerance.

    Named for what it was meant to do and deliberately does not: see the note
    on paragraph detection above. It stays as a named seam so a future
    implementation has one call site to replace, and so the measurement harness
    keeps comparing like with like across that change.
    """
    return _plain_text(page)


def _document_metadata(pdf: Any) -> dict[str, str]:
    """Title, author and producer from the PDF's own metadata dictionary.

    Advisory only. §11.4 refuses metadata as a *classification* input -- many
    PDFs carry no copyright field and those that do are often wrong -- so this
    populates the row for a reviewer to read, never a rights decision.
    """
    raw = getattr(pdf, "metadata", None) or {}
    out: dict[str, str] = {}
    for key in ("Title", "Author", "Subject", "Creator", "Producer"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            out[key.lower()] = value.strip()
    return out


def _body_font_size(pdf: Any) -> float:
    """The document's modal character size, sampled from its first pages.

    The whole document would be more accurate and is not worth the pass: font
    size is a document-level property, and reading 400 pages of character
    metadata to learn "the body is 10pt" costs more than the heading detection
    it feeds is worth.
    """
    sizes: list[float] = []
    for page in list(pdf.pages)[:5]:
        for char in page.chars or ():
            size = char.get("size")
            if isinstance(size, (int, float)):
                sizes.append(round(float(size), 1))
    if not sizes:
        return 0.0
    try:
        return float(statistics.mode(sizes))
    except statistics.StatisticsError:  # pragma: no cover -- ties are rare
        return float(statistics.median(sizes))


def _headings_on_page(page: Any, body_size: float) -> list[str]:
    """Lines set materially larger than the body (§7.1's font-size heuristic).

    Heuristic, and known to miss roughly 15% of section boundaries on real
    documents: a book that sets its headings in the body size and distinguishes
    them by weight alone is invisible to this. That is what
    ``unstructured.io`` is available for on sources where ``section_path``
    matters more than the marginal cost (§4).
    """
    if body_size <= 0:
        return []

    threshold = body_size * HEADING_SIZE_RATIO
    lines: dict[float, list[tuple[float, str]]] = {}

    for char in page.chars or ():
        size = char.get("size")
        top = char.get("top")
        text = char.get("text", "")
        if not isinstance(size, (int, float)) or not isinstance(top, (int, float)):
            continue
        if float(size) < threshold:
            continue
        # Grouped by rounded vertical position: characters on one visual line
        # share a `top` to within sub-point jitter.
        lines.setdefault(round(float(top), 0), []).append(
            (float(char.get("x0", 0.0)), text)
        )

    headings: list[str] = []
    for top in sorted(lines):
        chars = sorted(lines[top], key=lambda item: item[0])
        line = "".join(text for _, text in chars).strip()
        if line and len(line) <= MAX_HEADING_CHARS:
            headings.append(line)
    return headings


def _apply_heading(hierarchy: Sequence[str], heading: str) -> list[str]:
    """Fold a detected heading into the running section path.

    Numbered headings carry their own depth: "3.2.1" is three levels down, so
    the path is truncated to two and the new heading appended. Unnumbered
    headings have no depth information at all, so they replace the last
    element rather than nesting -- guessing a depth would build a path that
    looks precise and is invented.
    """
    depth = _heading_depth(heading)
    path = list(hierarchy)
    if depth is None:
        if path:
            path[-1] = heading
        else:
            path.append(heading)
        return path

    del path[depth - 1 :]
    path.append(heading)
    return path


def _heading_depth(heading: str) -> int | None:
    """Depth from a leading dotted number: "3.2.1 Types" is 3."""
    import re

    match = re.match(r"^\s*(\d+(?:\.\d+)*)\.?\s+\S", heading)
    if not match:
        return None
    return len(match.group(1).split("."))


# --- §7.2/§7.3 registry ----------------------------------------------------

#: The extractors this build can run. ``marker`` and ``unstructured`` are named
#: in §7.2 as pluggable alternatives and are deliberately *not* stubbed here: a
#: registry entry that raises on use is worse than a missing key, because it
#: reads as supported at the call site and fails at ingestion time. Adding one
#: means implementing the Protocol, nothing more.
_EXTRACTORS: dict[str, Extractor] = {
    "pdfplumber": PdfPlumberExtractor(),
}

DEFAULT_EXTRACTOR = "pdfplumber"


def available_extractors() -> tuple[str, ...]:
    return tuple(sorted(_EXTRACTORS))


def get_extractor(name: str | None = None) -> Extractor:
    """The named extractor, or the §7.3 default.

    The per-source override arrives on the extract job's payload rather than on
    a ``sources.metadata`` column, which the schema does not have -- see
    DIVERGENCES-INGESTION (I3). The job is the right home regardless: which
    extractor to run is a property of a particular extraction run, and a
    re-extraction under a different extractor is a new job, not an edit to the
    source.
    """
    chosen = name or DEFAULT_EXTRACTOR
    try:
        return _EXTRACTORS[chosen]
    except KeyError:
        raise ExtractorUnavailable(
            f"unknown extractor {chosen!r}; available: {', '.join(available_extractors())}"
        ) from None


def register_extractor(extractor: Extractor) -> None:
    """Add an extractor to the registry. Used by tests and by §7.2 alternatives."""
    _EXTRACTORS[extractor.name] = extractor

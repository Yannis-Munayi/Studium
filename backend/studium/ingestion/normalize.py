"""Text normalisation between extraction and chunking (ingestion §8).

The job here is narrow: repair the artefacts a PDF extractor produces that
would silently damage a downstream stage. Not clean up prose, not improve it --
repair what extraction broke. The U+FB01 ligature was the instance that made
the case; this module is the generalisation.

**Silently is the operative word.** Every artefact handled here degrades
something without raising: a ligature makes a chunk invisible to keyword search
for a word it plainly contains, a hyphenated line break splits one word into
two index terms, a running head repeated on 400 pages adds 400 copies of the
book's title to the corpus and pulls every vector slightly toward it. Nothing
fails. The corpus just gets quietly worse, and the symptom surfaces much later
as "retrieval is not finding the obvious passage".

**Determinism is a hard requirement**, for the same reason it is in retrieval
§7: chunk text feeds the Lecturer's cached prefix through ``grounding_version``,
so text that varies between runs invalidates a cache the runtime pays to keep
warm. Everything here is a pure function of its arguments -- no clock, no
randomness, no set iteration.

What this module deliberately does not do (§8.2): stemming, lemmatisation or
tokenisation (Postgres' tsvector owns that), sentence segmentation (chunking
owns it), any semantic transformation, and reference detection (``chunk_type``
owns it). Normalisation touches presentation, never meaning.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field

# One ligature table, not two. Retrieval's chunker already folds these on every
# block it is handed, and a second table here would be free to drift from it --
# at which point chunk text and the tsvector generated from it would disagree
# about the same source, and only for the words that happen to contain a
# ligature. Importing it makes the drift impossible rather than unlikely.
#
# It covers exactly U+FB00..U+FB06, which is what §8.1 asks for. Full NFKC
# would also fold superscripts, fractions and several mathematical operators,
# which in a lambda calculus corpus destroys content: the notation *is* the
# subject matter.
from studium.retrieval.chunking import LIGATURES, count_tokens

#: Stamped onto ``sources.normalizer_version``. Bump on any change that alters
#: output for input that previously normalised differently -- a new
#: transformation, or a changed one. §8.4: sources ingested under an older
#: version are not re-normalised automatically; a reviewer triggers it per
#: source when the change matters to that source.
NORMALIZER_VERSION = "studium.normalize/1.0"

# --- §8.3 warning thresholds ----------------------------------------------

#: Above this fraction of non-ASCII characters after normalisation, the
#: extractor probably missed an encoding conversion.
MAX_NON_ASCII_RATIO = 0.05
#: More long lines than this and line-break detection probably failed.
MAX_LONG_LINES = 20
LONG_LINE_CHARS = 500
#: A page that normalises below this, from raw text that was longer, suggests
#: the normalizer over-stripped -- almost always header/footer detection
#: eating real content.
MIN_PAGE_TOKENS = 50

#: Below this fraction of spaces, the extractor is running words together.
#:
#: Not in §8.3's list, and added because the corpus had the defect. The
#: Michaelson book extracted as
#: "Itispossible,however,forboundvariablesindifferentfunctions" under
#: pdfplumber's default word-split tolerance -- whole paragraphs as single
#: tokens. Every existing signal read healthy: the chunk-size median was 409
#: against a 400 target, coverage 0.99, section detection 1.00. Token counts
#: cannot see it, because a run-together paragraph still counts as many tokens.
#:
#: English prose runs about 0.16-0.18 spaces per character. 0.11 is low enough
#: that ordinary maths-heavy or code-heavy pages clear it (the Clojure material
#: sits at 0.14) while a document with no word breaks at all cannot.
MIN_SPACE_RATIO = 0.11

# --- character classes -----------------------------------------------------

_LIGATURE_RE = re.compile("[" + "".join(LIGATURES) + "]")

#: §8.1 step 2. Curly quotes to straight. The apostrophe is preserved *as an
#: apostrophe* -- U+2019 becomes ASCII "'", it is not deleted -- because
#: "Church's theorem" and "Churchs theorem" are different words to the search
#: index.
_QUOTES = {
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
    "′": "'",
    "″": '"',
}
_QUOTE_RE = re.compile("[" + "".join(_QUOTES) + "]")

#: An en dash between digits is a range ("pages 12-18", "1936-1937") and stays.
_EN_DASH_RANGE = re.compile(r"(?<=\d)–(?=\d)")
#: An en dash between letters is standing in for a hyphen in a compound
#: ("Church-Rosser") and becomes one, so the compound indexes as one term.
_EN_DASH_COMPOUND = re.compile(r"(?<=[^\W\d_])–(?=[^\W\d_])")

#: §8.1 step 5. A line ending "word-" whose next line starts lowercase was
#: hyphenated by the typesetter, not by the author. Joined without the hyphen.
_HYPHEN_BREAK = re.compile(r"([^\W\d_])-[ \t]*\n[ \t]*(?=[a-z])")

#: Horizontal whitespace only. Newlines are handled separately -- collapsing
#: them into this run would erase the paragraph structure the chunker breaks on.
_HORIZONTAL_WS = re.compile(r"[ \t  -   　]+")
_BLANK_LINES = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")
_TRAILING_WS = re.compile(r"[ \t]+\n")

#: §8.1 step 7. Zero-width characters and the soft hyphen. The soft hyphen is
#: here rather than with the dashes because it is invisible: it renders as
#: nothing and splits an index term in half with no visible cause.
_INVISIBLE = re.compile(r"[­​‌‍⁠﻿]")

#: Page furniture is short. A 300-character line repeated across pages is a
#: legal boilerplate paragraph, and stripping it would take real text with it.
#: §8.1: "errs on the side of keeping content if the pattern is ambiguous".
MAX_FURNITURE_CHARS = 80

#: Digit masking (see ``_furniture_signature``) applies only to lines at most
#: this long. Masking makes a signature less discriminating, which is what
#: catches "47"/"48" and "Chapter 3   47"/"Chapter 3   48" -- and also what
#: would collapse two *prose* lines differing only in a numeral. Confining it
#: to short lines is what keeps the first behaviour without the second: a line
#: long enough to be a sentence has to repeat exactly to count as furniture.
FURNITURE_MASK_CHARS = 40
#: §8.1: a line appearing in at least this fraction of pages is furniture.
FURNITURE_PAGE_RATIO = 0.5
#: Fewer pages than this and the ratio means nothing -- on a 3-page paper one
#: repeated section heading would clear 50%.
MIN_PAGES_FOR_FURNITURE = 4
#: How many lines at each edge of a page are candidates.
FURNITURE_EDGE_LINES = 2

#: A page must have more non-blank lines than the two edge windows cover, or
#: every line on it is an "edge" line and the heuristic is examining the whole
#: page as candidate furniture.
#:
#: This guard is load-bearing, not defensive. Digit masking (see ``_DIGITS``)
#: makes the signature *less* discriminating on purpose, and on a short page
#: that combination strips real content: eight pages reading "Real body text
#: for page 1 goes here." all mask to one signature, clear 50%, and the body of
#: every page is deleted as a running head. Requiring the page to be longer
#: than its own edges is what separates a running head from a page that is
#: nothing but edges.
MIN_PAGE_LINES_FOR_FURNITURE = 2 * FURNITURE_EDGE_LINES + 1

#: Digit runs are masked before comparing furniture candidates, so that "47"
#: and "48" -- or "Chapter 3    47" and "Chapter 3    48" -- compare equal.
#: Without this the §8.1 heuristic strips almost nothing on a real book, since
#: a running head virtually always carries the page number and therefore
#: matches no other page exactly. See DIVERGENCES-INGESTION (I2).
_DIGITS = re.compile(r"\d+")


@dataclass(frozen=True, slots=True)
class NormalizationWarning:
    """One §8.3 signal that extraction quality may be bad.

    A warning, never a failure: the pipeline continues and a reviewer decides
    whether to accept the source, re-extract it with a different extractor, or
    reject it. Carries ``detail`` in prose because it lands in a queue row a
    human reads, and ``page_number`` when it is about one page.
    """

    kind: str
    detail: str
    page_number: int | None = None

    def as_payload(self) -> dict[str, object]:
        return {"kind": self.kind, "detail": self.detail, "page": self.page_number}


@dataclass(frozen=True, slots=True)
class NormalizedPage:
    page_number: int
    text: str
    section_hierarchy: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    pages: tuple[NormalizedPage, ...]
    warnings: tuple[NormalizationWarning, ...] = ()
    #: The lines removed as page furniture, for the reviewer to sanity-check.
    #: Over-stripping is the failure mode that loses corpus content silently,
    #: so what was taken is reported rather than merely counted.
    stripped_furniture: tuple[str, ...] = ()
    version: str = NORMALIZER_VERSION

    @property
    def is_empty(self) -> bool:
        return not any(page.text.strip() for page in self.pages)


def normalize_page(text: str) -> str:
    """Apply the character-level transformations to one page (§8.1 steps 1-5, 7).

    Header/footer stripping (step 6) is not here: it is the one transformation
    that cannot be decided from a single page, since "repeated across pages" is
    the whole definition. :func:`normalize_document` applies it.

    Order matters in two places. Invisible characters are removed first, so a
    soft hyphen sitting inside "defi-\\nnition" does not defeat the line-break
    repair that runs later. Whitespace collapsing comes after the hyphen repair,
    because the repair needs the newline the collapse would otherwise have
    already folded into a space.
    """
    if not text:
        return ""

    # 7 (first): invisible characters, before anything pattern-matches around
    # them. Also drops the control characters that survive some extractors.
    #
    # Form feed and vertical tab are kept here and *converted to spaces* by
    # step 4, not deleted. They are separators -- a form feed is where a
    # column or a page ended -- so deleting one joins the last word before it
    # to the first word after, producing a token that exists in no dictionary
    # and matches no query. "the endBeginning" is a worse artefact than the
    # control character it replaced.
    text = _INVISIBLE.sub("", text)
    text = "".join(
        ch
        for ch in text
        if ch in "\t\n\r\v\f"
        or unicodedata.category(ch) not in {"Cc", "Cf", "Co", "Cs"}
    )
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 1: ligatures.
    text = _LIGATURE_RE.sub(lambda m: LIGATURES[m.group()], text)

    # 2: smart quotes.
    text = _QUOTE_RE.sub(lambda m: _QUOTES[m.group()], text)

    # 3: dashes. Em dash is semantic and untouched. The figure dash exists only
    # to align with digit widths and carries no meaning a hyphen does not.
    text = _EN_DASH_RANGE.sub("–", text)
    text = _EN_DASH_COMPOUND.sub("-", text)
    text = text.replace("‒", "-").replace("−", "-")

    # 5: hyphenated line-break repair, before whitespace collapsing eats the
    # newline it keys on. A hyphen before an uppercase letter or a digit is
    # left alone -- "Church-Rosser" broken across lines is still one term, but
    # so is "Curry-Howard", and joining a semantic hyphen produces a word that
    # exists nowhere.
    text = _HYPHEN_BREAK.sub(r"\1", text)

    # 4: whitespace. Horizontal runs collapse; the vertical structure survives,
    # because the chunker breaks paragraphs on blank lines and folding those
    # into spaces would hand it one continuous wall of text.
    text = _HORIZONTAL_WS.sub(" ", text)
    text = _TRAILING_WS.sub("\n", text)
    text = _BLANK_LINES.sub("\n\n", text)

    return text.strip()


def normalize_document(
    pages: Sequence[tuple[int, str]] | Sequence[NormalizedPage],
    *,
    section_hierarchies: Sequence[Sequence[str]] | None = None,
    strip_furniture: bool = True,
) -> NormalizationResult:
    """Normalise every page, then strip page furniture across them (§8.1).

    ``pages`` is ``(page_number, raw_text)`` pairs in document order.

    Furniture stripping runs last and across the whole document because it is
    defined by repetition: a line is a running head because it appears on most
    pages, which no single page can know. It is skipped for short documents,
    where the ratio is meaningless.
    """
    raw = [(number, text) for number, text in _as_pairs(pages)]
    hierarchies = _hierarchies_for(raw, section_hierarchies)

    normalized = [(number, normalize_page(text)) for number, text in raw]

    stripped: tuple[str, ...] = ()
    if strip_furniture:
        normalized, stripped = _strip_furniture(normalized)

    warnings = _warnings(raw, normalized, stripped)

    return NormalizationResult(
        pages=tuple(
            NormalizedPage(
                page_number=number,
                text=text,
                section_hierarchy=tuple(hierarchies.get(number, ())),
            )
            for number, text in normalized
        ),
        warnings=warnings,
        stripped_furniture=stripped,
    )


def _as_pairs(
    pages: Sequence[tuple[int, str]] | Sequence[NormalizedPage],
) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for page in pages:
        if isinstance(page, NormalizedPage):
            out.append((page.page_number, page.text))
        else:
            out.append((int(page[0]), page[1] or ""))
    return out


def _hierarchies_for(
    raw: Sequence[tuple[int, str]],
    hierarchies: Sequence[Sequence[str]] | None,
) -> dict[int, tuple[str, ...]]:
    if hierarchies is None:
        return {}
    return {
        number: tuple(path)
        for (number, _), path in zip(raw, hierarchies, strict=False)
    }


# --- §8.1 step 6: header/footer stripping ---------------------------------


def _furniture_signature(line: str) -> str:
    """A comparison key for one candidate furniture line.

    Two regimes, and the split is the whole point. "Chapter 3    47" and
    "Chapter 3    48" are the same running head, and an exact comparison says
    they are two lines that each appear once -- so a short line has its digit
    runs masked, which is what makes the §8.1 ratio test fire on a real book
    instead of only on a fixture whose pages happen to be identical.

    A *long* line is compared exactly. Masking it would fold together two
    sentences differing in one numeral, and page-edge prose that differs only
    in a number is common enough (numbered lists, tabular rows, equations
    carrying an index) that the masked form would delete real content on every
    page of the document. §8.1 says to err toward keeping content where the
    pattern is ambiguous; this is where the ambiguity actually lives.

    The two namespaces are kept disjoint by the prefix, so a masked signature
    can never collide with the exact text of some other line.
    """
    stripped = line.strip().casefold()
    if len(stripped) <= FURNITURE_MASK_CHARS:
        return "m:" + _DIGITS.sub("#", stripped)
    return "x:" + stripped


def _strip_furniture(
    pages: list[tuple[int, str]],
) -> tuple[list[tuple[int, str]], tuple[str, ...]]:
    """Remove lines that repeat at the same page edge across the document."""
    if len(pages) < MIN_PAGES_FOR_FURNITURE:
        return pages, ()

    # Counted per edge, not per document. A line is a header because it
    # repeats *at the top*; the same text appearing mid-page is body text and
    # must survive. Keyed (edge, signature) so a footer cannot be voted up by
    # occurrences of the same string in headers.
    counts: dict[tuple[str, str], int] = {}
    eligible = 0
    for _, text in pages:
        if not _has_room_for_furniture(text):
            continue
        eligible += 1
        for edge, line in _edge_lines(text):
            if len(line) > MAX_FURNITURE_CHARS:
                continue
            key = (edge, _furniture_signature(line))
            counts[key] = counts.get(key, 0) + 1

    if eligible < MIN_PAGES_FOR_FURNITURE:
        return pages, ()

    # Against the eligible pages, not every page. A document whose short pages
    # are excluded would otherwise raise a threshold its remaining pages cannot
    # reach, and stripping would silently stop working on exactly the documents
    # that mix full pages with short ones.
    threshold = max(2, int(eligible * FURNITURE_PAGE_RATIO))
    furniture = {key for key, count in counts.items() if count >= threshold}
    if not furniture:
        return pages, ()

    out: list[tuple[int, str]] = []
    removed: list[str] = []
    for number, text in pages:
        lines = text.split("\n")
        drop_indices: set[int] = set()
        for edge, line, index in _edge_lines_indexed(text):
            if (edge, _furniture_signature(line)) in furniture:
                drop_indices.add(index)

        kept_lines = [
            line for index, line in enumerate(lines) if index not in drop_indices
        ]

        # Never strip a page to nothing. If the candidates account for all of
        # the page's text then they were not furniture on this page, whatever
        # the document-wide count said -- and losing the page silently is a
        # worse outcome than leaving a running head in one chunk. §8.1: "errs
        # on the side of keeping content if the pattern is ambiguous."
        if drop_indices and not "".join(kept_lines).strip():
            out.append((number, text))
            continue

        removed.extend(lines[index].strip() for index in sorted(drop_indices))
        out.append((number, "\n".join(kept_lines).strip()))

    # Sorted and de-duplicated for a stable report: this is read by a human and
    # written into a queue payload, and set iteration order is not stable
    # across runs.
    return out, tuple(sorted({line for line in removed if line}))


def _has_room_for_furniture(text: str) -> bool:
    """Whether this page has more content than its own two edge windows."""
    non_blank = sum(1 for line in text.split("\n") if line.strip())
    return non_blank >= MIN_PAGE_LINES_FOR_FURNITURE


def _edge_lines(text: str) -> list[tuple[str, str]]:
    return [(edge, line) for edge, line, _ in _edge_lines_indexed(text)]


def _edge_lines_indexed(text: str) -> list[tuple[str, str, int]]:
    """The candidate furniture lines at both page edges, with their indices.

    Blank lines are skipped when choosing candidates but keep their index, so
    a page whose header is preceded by an empty line is still examined -- an
    off-by-one here silently disables stripping for a whole document.
    """
    lines = text.split("\n")
    non_blank = [(index, line) for index, line in enumerate(lines) if line.strip()]
    if not non_blank:
        return []

    out: list[tuple[str, str, int]] = []
    for index, line in non_blank[:FURNITURE_EDGE_LINES]:
        out.append(("head", line, index))
    for index, line in non_blank[-FURNITURE_EDGE_LINES:]:
        # A very short page can have one line that is both first and last.
        # Recording it twice would double its vote.
        if any(existing_index == index for _, _, existing_index in out):
            continue
        out.append(("foot", line, index))
    return out


# --- §8.3 warning surfaces -------------------------------------------------


def _warnings(
    raw: Sequence[tuple[int, str]],
    normalized: Sequence[tuple[int, str]],
    stripped: Sequence[str],
) -> tuple[NormalizationWarning, ...]:
    warnings: list[NormalizationWarning] = []
    joined = "\n".join(text for _, text in normalized)

    if joined:
        non_ascii = sum(1 for ch in joined if ord(ch) > 127)
        ratio = non_ascii / len(joined)
        if ratio > MAX_NON_ASCII_RATIO:
            warnings.append(
                NormalizationWarning(
                    kind="non_ascii",
                    detail=(
                        f"{ratio:.1%} of characters are non-ASCII after "
                        f"normalisation (threshold {MAX_NON_ASCII_RATIO:.0%}); "
                        f"the extractor may have missed an encoding conversion"
                    ),
                )
            )

    # Measured on the letters-and-spaces portion rather than on everything, so
    # a page of dense notation is not accused of running its words together
    # merely for containing few spaces between many symbols.
    prose = [ch for ch in joined if ch.isalpha() or ch == " "]
    if len(prose) > 200:
        space_ratio = sum(1 for ch in prose if ch == " ") / len(prose)
        if space_ratio < MIN_SPACE_RATIO:
            warnings.append(
                NormalizationWarning(
                    kind="words_run_together",
                    detail=(
                        f"only {space_ratio:.1%} of the prose is spaces "
                        f"(expected at least {MIN_SPACE_RATIO:.0%}); the "
                        f"extractor is probably not splitting words, which "
                        f"makes this source near-invisible to keyword search "
                        f"while every size metric still looks healthy"
                    ),
                )
            )

    long_lines = sum(
        1 for line in joined.split("\n") if len(line) > LONG_LINE_CHARS
    )
    if long_lines > MAX_LONG_LINES:
        warnings.append(
            NormalizationWarning(
                kind="long_lines",
                detail=(
                    f"{long_lines} lines exceed {LONG_LINE_CHARS} characters "
                    f"(threshold {MAX_LONG_LINES}); line-break detection may "
                    f"have failed"
                ),
            )
        )

    raw_by_page = dict(raw)
    for number, text in normalized:
        before = raw_by_page.get(number, "")
        if not before.strip():
            # A page that was empty before normalisation is an extraction
            # problem, not a normalisation one, and §6.3 has already flagged it.
            continue
        after_tokens = count_tokens(text)
        if after_tokens < MIN_PAGE_TOKENS and count_tokens(before) > after_tokens:
            warnings.append(
                NormalizationWarning(
                    kind="over_stripped",
                    detail=(
                        f"page normalised to {after_tokens} tokens from "
                        f"{count_tokens(before)}; the normalizer may have "
                        f"over-stripped"
                    ),
                    page_number=number,
                )
            )

    if stripped:
        warnings.append(
            NormalizationWarning(
                kind="furniture_stripped",
                detail=(
                    f"removed {len(stripped)} repeated line(s) as page "
                    f"furniture: {', '.join(repr(line) for line in stripped[:5])}"
                    + (" ..." if len(stripped) > 5 else "")
                ),
            )
        )

    return tuple(warnings)


@dataclass(frozen=True, slots=True)
class LigatureReport:
    """What :func:`ligature_coverage` found. Used by the Tier 1 completeness test."""

    mapped: tuple[str, ...] = field(default=())
    unmapped: tuple[str, ...] = field(default=())


def ligature_coverage() -> LigatureReport:
    """Every U+FB00..U+FB06 codepoint, split by whether the table maps it.

    §16 Tier 1 asserts ``unmapped`` is empty. The check is against the Unicode
    block rather than against the table's own keys, which would be vacuous --
    a table can only be complete relative to something outside itself.
    """
    block = [chr(cp) for cp in range(0xFB00, 0xFB07)]
    mapped = tuple(ch for ch in block if ch in LIGATURES)
    return LigatureReport(
        mapped=mapped,
        unmapped=tuple(ch for ch in block if ch not in LIGATURES),
    )

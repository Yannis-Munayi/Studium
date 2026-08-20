"""Chunking source text into ``source_chunks`` rows (retrieval §7).

The algorithm lives here; the ingestion pipeline that calls it lives in
subsystem 5. That split is deliberate: chunking quality drives retrieval
quality, so retrieval owns it, without owning the mechanics of getting text out
of a PDF.

The input is text *plus structure* -- a sequence of :class:`Block` values that
ingestion produces from a PDF outline or a Markdown parse. This module never
sees a binary. What it decides is where the boundaries fall, what kind each
chunk is, and what section path it inherits.

**Determinism is a hard requirement, not a nicety.** The same input must
produce byte-identical output on every run, because chunk text feeds the
Lecturer's cached prefix through ``grounding_version``: any instability here
invalidates a cache the agent runtime pays real money to keep warm (§3
"Determinism where possible"). Everything in this module is a pure function of
its arguments -- no clock, no randomness, no dict iteration order that is not
insertion order, no set iteration at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace

# --- §7 target parameters --------------------------------------------------

#: Tokens per chunk, aimed for rather than enforced.
TARGET_TOKENS = 400
#: A chunk that would exceed this splits.
MAX_TOKENS = 600
#: Below this a ``body`` chunk merges with a neighbour rather than standing
#: alone. An 80-token fragment embeds to a vector that means very little.
MIN_BODY_TOKENS = 80
#: The window the break-preference search walks, centred on the target.
TARGET_WINDOW = (350, 450)
#: Overlap between adjacent body chunks, as a fraction.
OVERLAP_RATIO = 0.15
#: Code and math stay whole up to here. Past it, even an atomic block has to
#: split or it cannot be embedded at all.
ATOMIC_HARD_LIMIT = 2000

#: §7: exempt from the minimum. A one-line caption or a three-line code block
#: is a legitimate chunk; a 40-token paragraph fragment is not.
MINIMUM_EXEMPT_TYPES = frozenset({"code", "math", "figure_caption", "exercise"})

#: Types that are never merged into a neighbour or split on prose boundaries.
ATOMIC_TYPES = frozenset({"code", "math"})

#: Rough characters-per-token for the fallback estimator. Measured against
#: English prose; see :func:`count_tokens` for why an estimate is acceptable.
CHARS_PER_TOKEN = 3.6

# --- structural detection --------------------------------------------------

#: "3.2.1 Function Types", "CHAPTER THREE", "Chapter 3: Types".
_HEADING_NUMERIC = re.compile(r"^\s*(?:\d+\.)*\d+\.?\s+\S")
_HEADING_WORD = re.compile(r"^\s*(chapter|section|part|appendix)\b", re.IGNORECASE)

#: "Figure 3.1:", "Table 2:", "Fig. 4 --".
_CAPTION = re.compile(
    r"^\s*(figure|fig\.?|table|listing|algorithm|exhibit)\s*\d+(\.\d+)*\s*[:.—-]",
    re.IGNORECASE,
)

#: Section names that make everything under them an exercise or a reference.
_EXERCISE_SECTIONS = ("exercise", "problem")
_REFERENCE_SECTIONS = ("reference", "bibliograph", "works cited", "further reading")

#: Abbreviations whose trailing period is not a sentence end. Without this the
#: sentence-boundary preference cuts "i.e." and "Fig." mid-phrase, which reads
#: as a truncation bug in the hover card long before anyone suspects chunking.
ABBREVIATIONS = frozenset(
    {
        "e.g.", "i.e.", "cf.", "vs.", "etc.", "al.", "eq.", "fig.", "ch.",
        "sec.", "no.", "pp.", "vol.", "ed.", "trans.", "approx.", "resp.",
        "mr.", "mrs.", "ms.", "dr.", "prof.", "st.", "def.", "thm.", "lem.",
    }
)

_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]?\s+(?=[\"'(\[]?[A-Z0-9])")

#: Typographic ligatures that survive PDF extraction as single codepoints.
#:
#: These are not cosmetic. Postgres tokenises "deﬁnition" to 'deﬁnit'
#: and "definition" to 'definit', and the two do not match -- so a chunk
#: carrying the ligature is invisible to the keyword half of hybrid search for
#: the word it plainly contains. The Michaelson book has 688 instances of
#: U+FB01 alone, which would have silently removed most of its prose from
#: keyword results for "definition", "first" and "find".
#:
#: Only these seven are mapped. Full NFKC normalisation would also fold
#: superscripts, fractions and several mathematical symbols, which in a lambda
#: calculus corpus is destructive: the notation is the content.
LIGATURES = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "st",
    "ﬆ": "st",
}

_LIGATURE_RE = re.compile("[" + "".join(LIGATURES) + "]")


def normalize_text(text: str) -> str:
    """Fold extraction artefacts that would corrupt the search index.

    Applied to every chunk's text before it is measured or stored, so the
    stored text, the generated tsvector, and what a learner sees in a hover
    card all agree. Deterministic: a pure character mapping, so §7's
    byte-identical guarantee is unaffected.
    """
    if not text:
        return text
    return _LIGATURE_RE.sub(lambda m: LIGATURES[m.group()], text)


def count_tokens(text: str) -> int:
    """Token count for ``text``, via Voyage's tokenizer when it is installed.

    §7 measures every size in Voyage-3 tokens. The SDK ships the tokenizer, but
    it is an optional dependency here (Tier 1 runs with no provider packages at
    all), so this falls back to a character-ratio estimate.

    The fallback changes *where* boundaries land, not whether the algorithm is
    correct or deterministic -- both paths are pure functions of the text. What
    it must not do is vary within a run, so the tokenizer is resolved once and
    cached: a corpus half-chunked under one estimator and half under another
    would produce two size distributions and make §19's open question 1
    unanswerable.
    """
    encoder = _tokenizer()
    if encoder is not None:
        return len(encoder.encode(text).ids)
    return max(1, int(len(text) / CHARS_PER_TOKEN)) if text else 0


_TOKENIZER: object | None = None
_TOKENIZER_RESOLVED = False


def _tokenizer() -> object | None:
    global _TOKENIZER, _TOKENIZER_RESOLVED
    if _TOKENIZER_RESOLVED:
        return _TOKENIZER
    _TOKENIZER_RESOLVED = True
    try:  # pragma: no cover -- exercised only where voyageai is installed
        import voyageai

        _TOKENIZER = voyageai.get_tokenizer("voyage-3")
    except Exception:  # noqa: BLE001 -- any failure means "use the estimate"
        _TOKENIZER = None
    return _TOKENIZER


@dataclass(frozen=True, slots=True)
class Block:
    """One structural unit from ingestion: a paragraph, a code fence, a heading.

    Ingestion (subsystem 5) produces these from a PDF outline plus text
    extraction, or from a Markdown parse. ``kind`` is what ingestion already
    knows from the document's own markup -- a fenced code block is code because
    it was fenced, not because it looked monospaced. Where ingestion does not
    know, it passes ``None`` and :func:`classify_block` decides from content.
    """

    text: str
    #: Structural breadcrumbs at this block's position.
    section_path: tuple[str, ...] = ()
    #: A ``chunk_kind`` value when ingestion knows it; ``None`` to infer.
    kind: str | None = None
    page: int | None = None
    #: True when the block starts a new section (preference-1 break point).
    starts_section: bool = False


@dataclass(slots=True)
class Chunk:
    """One ``source_chunks`` row, before it is written."""

    text: str
    chunk_index: int
    chunk_type: str
    section_path: list[str] = field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    token_count: int = 0

    def as_row(self, source_id: object) -> dict[str, object]:
        """The insert payload, for ingestion to hand to the data layer."""
        return {
            "source_id": source_id,
            "chunk_index": self.chunk_index,
            "text": self.text,
            "token_count": self.token_count,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "section_path": list(self.section_path),
            "chunk_type": self.chunk_type,
        }


def classify_block(block: Block) -> str:
    """Assign a ``chunk_kind`` from content and structural context (§7).

    Ingestion's own ``kind`` wins when it has one: it read the document's
    markup, and a heuristic over extracted text is a worse signal than the
    markup that produced it.

    Section context outranks content pattern for exercises and references,
    because an exercise looks like a paragraph -- only its position says
    otherwise.
    """
    if block.kind:
        return block.kind

    lowered_path = " ".join(block.section_path).lower()
    if any(marker in lowered_path for marker in _REFERENCE_SECTIONS):
        return "reference"
    if any(marker in lowered_path for marker in _EXERCISE_SECTIONS):
        return "exercise"

    stripped = block.text.strip()
    if not stripped:
        return "body"

    if _CAPTION.match(stripped):
        return "figure_caption"

    if _is_heading(stripped):
        return "heading"

    return "body"


def _is_heading(stripped: str) -> bool:
    """A single short line that looks like a title.

    All three conditions matter. Single-line excludes a paragraph that happens
    to open with a number; the length cap excludes a numbered list item that
    runs on; and the pattern excludes an ordinary short sentence.
    """
    if "\n" in stripped or len(stripped) > 120:
        return False
    if _HEADING_NUMERIC.match(stripped) or _HEADING_WORD.match(stripped):
        return True
    # ALL CAPS with at least one letter, e.g. "PRELIMINARIES".
    return stripped.isupper() and any(c.isalpha() for c in stripped)


def chunk_blocks(blocks: Sequence[Block]) -> list[Chunk]:
    """Chunk a source's structural blocks into ``source_chunks`` rows.

    The pipeline, in order:

    1. Classify each block (§7 "Chunk type assignment").
    2. Emit atomic blocks -- code, math, headings, captions -- as their own
       chunks, splitting only past the 2000-token hard limit.
    3. Accumulate runs of prose into target-sized chunks, breaking at the best
       available boundary and carrying overlap forward.
    4. Merge any undersized body chunk into a neighbour.

    Prose runs are broken by *any* non-body block, which is what stops a chunk
    from spanning a code fence and citing as prose evidence something that is
    half program text.
    """
    # Normalisation happens here, once, before anything measures or classifies:
    # doing it per-chunk instead would mean a ligature could still influence a
    # break decision or a size, and doing it in the caller would make it
    # optional. See LIGATURES for why it is not cosmetic.
    blocks = [
        block if block.text == (fixed := normalize_text(block.text))
        else replace(block, text=fixed)
        for block in blocks
    ]

    classified = [(block, classify_block(block)) for block in blocks]
    chunks: list[Chunk] = []
    pending: list[tuple[Block, str]] = []

    for block, kind in classified:
        if kind == "body":
            if block.text.strip():
                pending.append((block, kind))
            continue

        # A non-body block closes whatever prose was accumulating: a chunk
        # spanning a code fence would present program text as prose evidence.
        chunks.extend(_flush_prose(pending))
        pending = []
        chunks.extend(_atomic_chunks(block, kind))

    chunks.extend(_flush_prose(pending))
    chunks = _merge_undersized(chunks)

    for index, chunk in enumerate(chunks):
        chunk.chunk_index = index
        chunk.token_count = count_tokens(chunk.text)
    return chunks


def chunk_text(
    text: str,
    *,
    section_path: Sequence[str] = (),
    page: int | None = None,
) -> list[Chunk]:
    """Chunk a plain string with no structural metadata.

    The degenerate case ingestion hits on a source whose outline could not be
    extracted. Paragraph breaks are the only structure available, so
    preference 1 never fires and every break is a preference-2 or lower.
    """
    blocks = [
        Block(text=paragraph, section_path=tuple(section_path), page=page)
        for paragraph in _split_paragraphs(text)
    ]
    return chunk_blocks(blocks)


# --- prose accumulation ----------------------------------------------------


def _flush_prose(pending: Sequence[tuple[Block, str]]) -> list[Chunk]:
    """Turn accumulated body blocks into target-sized chunks."""
    if not pending:
        return []

    chunks: list[Chunk] = []
    run: list[Block] = []

    def emit(blocks: list[Block]) -> None:
        if blocks:
            chunks.extend(_chunks_from_run(blocks))

    for block, _ in pending:
        # §7 preference 1: a section boundary is the best break there is, and
        # it is the only one that also changes the section_path -- letting a
        # chunk straddle it would file half its text under the wrong section.
        if block.starts_section and run:
            emit(run)
            run = []
        run.append(block)

    emit(run)
    return chunks


def _chunks_from_run(blocks: list[Block]) -> list[Chunk]:
    """Break one same-section prose run into chunks, carrying overlap.

    Walks paragraph by paragraph, closing a chunk once adding the next
    paragraph would push past the target window. A single paragraph larger than
    the maximum is split internally by :func:`_split_oversized`.
    """
    section_path = list(blocks[0].section_path)
    chunks: list[Chunk] = []

    buffer: list[str] = []
    buffer_tokens = 0
    first_page = blocks[0].page
    last_page = blocks[0].page
    carry = ""

    def close() -> None:
        nonlocal buffer, buffer_tokens, carry, first_page
        if not buffer:
            return
        body = "\n\n".join(buffer).strip()
        if body:
            chunks.append(
                Chunk(
                    text=_with_overlap(carry, body),
                    chunk_index=0,
                    chunk_type="body",
                    section_path=list(section_path),
                    page_start=first_page,
                    page_end=last_page,
                )
            )
            carry = _overlap_suffix(body)
        buffer = []
        buffer_tokens = 0
        first_page = last_page

    for block in blocks:
        paragraph = block.text.strip()
        if not paragraph:
            continue
        last_page = block.page if block.page is not None else last_page

        tokens = count_tokens(paragraph)

        if tokens > MAX_TOKENS:
            # The paragraph alone exceeds the maximum: flush what is buffered,
            # then break the paragraph on sentence/word/character preferences.
            close()
            for piece in _split_oversized(paragraph):
                chunks.append(
                    Chunk(
                        text=_with_overlap(carry, piece),
                        chunk_index=0,
                        chunk_type="body",
                        section_path=list(section_path),
                        page_start=block.page,
                        page_end=block.page,
                    )
                )
                carry = _overlap_suffix(piece)
            first_page = block.page
            continue

        if buffer and buffer_tokens + tokens > TARGET_WINDOW[1]:
            close()
            first_page = block.page

        if not buffer:
            first_page = block.page
        buffer.append(paragraph)
        buffer_tokens += tokens

        if buffer_tokens >= TARGET_WINDOW[0]:
            close()

    close()
    return chunks


def _split_oversized(paragraph: str) -> list[str]:
    """Break a single over-long paragraph on §7 preferences 3, 4, then 5.

    Preferences 1 and 2 are unavailable by construction -- this is one
    paragraph inside one section, so there is no section boundary and no blank
    line left to break on.
    """
    pieces: list[str] = []
    remaining = paragraph

    while count_tokens(remaining) > MAX_TOKENS:
        cut = _best_cut(remaining)
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()

    if remaining.strip():
        pieces.append(remaining.strip())
    return [p for p in pieces if p]


def _best_cut(text: str) -> int:
    """Character offset of the best break within the target window.

    Preferences are tried in §7's order and the first that lands inside the
    window wins. The character fallback is not a defect: a single "word" longer
    than a whole chunk is a URL or a mangled identifier, and cutting it beats
    emitting a chunk that blows the embedding provider's own limit.
    """
    window_end = _offset_for_tokens(text, TARGET_WINDOW[1])
    window_start = _offset_for_tokens(text, TARGET_WINDOW[0])

    sentence = _last_sentence_break(text, window_start, window_end)
    if sentence is not None:
        return sentence

    space = text.rfind(" ", window_start, window_end)
    if space > 0:
        return space + 1

    return max(1, window_end)


def _last_sentence_break(text: str, start: int, end: int) -> int | None:
    """The latest sentence boundary in ``[start, end)``, skipping abbreviations."""
    best: int | None = None
    for match in _SENTENCE_END.finditer(text, 0, end):
        position = match.end()
        if position < start:
            continue
        if _ends_with_abbreviation(text[:match.start() + 1]):
            continue
        best = position
    return best


def _ends_with_abbreviation(text: str) -> bool:
    tail = text.rstrip()
    if not tail.endswith("."):
        return False
    last = tail.split()[-1].lower() if tail.split() else ""
    if last in ABBREVIATIONS:
        return True
    # A lone initial: "Church, A. proved" is not two sentences.
    return len(last) == 2 and last[0].isalpha()


def _offset_for_tokens(text: str, tokens: int) -> int:
    """Character offset approximating ``tokens`` tokens into ``text``.

    Converting a token budget to a character position needs a ratio in either
    direction; the exact tokenizer would require a binary search per cut for a
    boundary that is then adjusted to the nearest sentence anyway. The estimate
    decides where to *look*; the preference hierarchy decides where to cut.
    """
    return min(len(text), max(1, int(tokens * CHARS_PER_TOKEN)))


def _with_overlap(carry: str, body: str) -> str:
    """Prepend the previous chunk's tail, trimmed so the total stays under the max.

    The overlap is added *after* the body has been sized, so without this it
    escapes the size check entirely: a 583-token paragraph plus an 87-token
    carry produced a 670-token chunk against a 600 maximum. Found by running the
    chunker over the Michaelson book, where 7.6% of chunks exceeded the max --
    invisible against the synthetic fixtures, whose uniform paragraph lengths
    never left a body close enough to the ceiling for the carry to push it over.

    Trimming from the front rather than the back: the carry exists to restore
    the context immediately preceding the body, so the words nearest the body
    are the ones worth keeping.

    Over-long chunks are not a cosmetic problem. ``MAX_TOKENS`` exists because
    the embedding provider has its own input ceiling and because an oversized
    chunk dilutes the vector it produces -- the passage retrieves worse, and the
    hover card shows the learner more text than they asked for.
    """
    if not carry or count_tokens(body) >= MAX_TOKENS:
        return body

    # Measured on the joined string, not on the parts. Token counts are not
    # additive across a concatenation -- the separator and the estimator's
    # rounding both land in the gap -- and budgeting from
    # ``MAX_TOKENS - count_tokens(body)`` left chunks one token over the
    # ceiling on the real corpus.
    words = carry.split()
    while words:
        candidate = " ".join(words) + "\n\n" + body
        if count_tokens(candidate) <= MAX_TOKENS:
            return candidate
        words.pop(0)

    return body


def _overlap_suffix(text: str) -> str:
    """The 15% tail of ``text``, to prefix the next chunk (§7 "Overlap").

    One-directional by design: chunk N+1 carries a suffix of chunk N, and
    nothing carries a prefix backwards. Every content window is therefore
    visible in exactly one chunk plus one neighbour's overlap, which prevents
    boundary context loss without doubling what gets embedded.

    Cut at a word boundary so the overlap never opens mid-token -- a fragment
    like "duc­tion is the" degrades the embedding of the chunk carrying it.
    """
    if not text:
        return ""
    keep = int(len(text) * OVERLAP_RATIO)
    if keep < 20:
        return ""
    tail = text[-keep:]
    space = tail.find(" ")
    if space == -1:
        return ""
    return tail[space + 1:].strip() + "\n\n"


# --- atomic blocks ---------------------------------------------------------


def _atomic_chunks(block: Block, kind: str) -> list[Chunk]:
    """Emit a non-prose block, splitting only past the hard limit (§7).

    Code and math are atomic because splitting them produces chunks that cite
    as evidence for claims neither chunk actually supports -- half a derivation
    proves nothing, and half a function does not compile. Past 2000 tokens they
    have to split anyway; the break goes to the nearest natural boundary, which
    is a blank line in both languages (a function gap in code, an equation gap
    in a display block).
    """
    text = block.text.strip()
    if not text:
        return []

    path = list(block.section_path)

    if kind not in ATOMIC_TYPES or count_tokens(text) <= ATOMIC_HARD_LIMIT:
        return [
            Chunk(
                text=text,
                chunk_index=0,
                chunk_type=kind,
                section_path=path,
                page_start=block.page,
                page_end=block.page,
            )
        ]

    return [
        Chunk(
            text=piece,
            chunk_index=0,
            chunk_type=kind,
            section_path=path,
            page_start=block.page,
            page_end=block.page,
        )
        for piece in _split_atomic(text)
    ]


def _split_atomic(text: str) -> list[str]:
    """Split an oversized code or math block at natural boundaries.

    Blank lines separate functions in code and equations in a display block, so
    they are the same rule in both. When there are none -- a single 2000-token
    function -- it falls through to a line break, and then to the prose cutter,
    because a chunk that cannot be embedded at all is worse than one cut badly.

    The pieces that result should be linked as siblings so retrieval can return
    them together. That is ``chunk_relations``, deferred to v1.1 (§19); until it
    exists, :func:`studium.retrieval.search.expand_atomic_siblings` recovers the
    relationship from adjacency, which is exact for blocks split here because
    the pieces are written with consecutive ``chunk_index`` values.
    """
    pieces: list[str] = []
    remaining = text

    while count_tokens(remaining) > ATOMIC_HARD_LIMIT:
        limit = _offset_for_tokens(remaining, ATOMIC_HARD_LIMIT)
        cut = remaining.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = _best_cut(remaining)
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip("\n")

    if remaining.strip():
        pieces.append(remaining.strip())
    return [p for p in pieces if p]


# --- post-processing -------------------------------------------------------


def _merge_undersized(chunks: list[Chunk]) -> list[Chunk]:
    """Fold short body chunks into a neighbour (§7 "Minimum size").

    Only ``body`` is subject to the minimum. A short chunk merges backwards
    into the previous body chunk in the same section when there is one, and
    forwards otherwise -- backwards first because a trailing fragment is
    usually the tail of the idea before it, not the head of the one after.

    A short body chunk with no body neighbour in its section survives. Dropping
    it would silently lose text from the corpus, and a lone short paragraph
    under a heading is legitimate content, not noise.
    """
    if not chunks:
        return []

    merged: list[Chunk] = []
    for chunk in chunks:
        if (
            chunk.chunk_type == "body"
            and count_tokens(chunk.text) < MIN_BODY_TOKENS
            and merged
            and merged[-1].chunk_type == "body"
            and merged[-1].section_path == chunk.section_path
            and count_tokens(merged[-1].text) + count_tokens(chunk.text) <= MAX_TOKENS
        ):
            previous = merged[-1]
            previous.text = f"{previous.text}\n\n{chunk.text}"
            previous.page_end = chunk.page_end or previous.page_end
            continue
        merged.append(chunk)

    # Forward pass for a short leading chunk, which the backward pass cannot
    # reach: it has no predecessor by definition.
    if (
        len(merged) > 1
        and merged[0].chunk_type == "body"
        and count_tokens(merged[0].text) < MIN_BODY_TOKENS
        and merged[1].chunk_type == "body"
        and merged[0].section_path == merged[1].section_path
        and count_tokens(merged[0].text) + count_tokens(merged[1].text) <= MAX_TOKENS
    ):
        merged[1].text = f"{merged[0].text}\n\n{merged[1].text}"
        merged[1].page_start = merged[0].page_start or merged[1].page_start
        merged.pop(0)

    return merged


def _split_paragraphs(text: str) -> Iterator[str]:
    for paragraph in re.split(r"\n\s*\n", text):
        if paragraph.strip():
            yield paragraph.strip()


def blocks_from_markdown(text: str, *, section_path: Sequence[str] = ()) -> list[Block]:
    """Parse Markdown into blocks, tracking heading depth into ``section_path``.

    A convenience for fixtures and for ingestion's Markdown path. PDF sources
    go through subsystem 5's extractor instead, which has the outline this has
    to infer.
    """
    blocks: list[Block] = []
    path: list[str] = list(section_path)
    base_depth = len(path)
    in_fence = False
    fence_lines: list[str] = []

    for raw in _paragraphs_and_fences(text):
        if raw.startswith("```"):
            in_fence = not in_fence
            if not in_fence and fence_lines:
                blocks.append(
                    Block(
                        text="\n".join(fence_lines),
                        section_path=tuple(path),
                        kind="code",
                    )
                )
                fence_lines = []
            continue

        if in_fence:
            fence_lines.append(raw)
            continue

        stripped = raw.strip()
        if not stripped:
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            depth = len(heading.group(1))
            title = heading.group(2).strip()
            del path[base_depth + depth - 1:]
            path.append(title)
            blocks.append(
                Block(
                    text=stripped,
                    section_path=tuple(path),
                    kind="heading",
                    starts_section=True,
                )
            )
            continue

        blocks.append(Block(text=stripped, section_path=tuple(path)))

    return blocks


def _paragraphs_and_fences(text: str) -> Iterable[str]:
    """Yield fence delimiters and lines inside them individually, paragraphs whole."""
    buffer: list[str] = []
    in_fence = False

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            if buffer:
                yield "\n".join(buffer)
                buffer = []
            in_fence = not in_fence
            yield "```"
            continue
        if in_fence:
            yield line
            continue
        if not line.strip():
            if buffer:
                yield "\n".join(buffer)
                buffer = []
            continue
        buffer.append(line)

    if buffer:
        yield "\n".join(buffer)

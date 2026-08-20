"""Tier 1: the chunking algorithm (retrieval §7, §17).

No database, no API. Chunking is a pure function and this tier proves it stays
one -- §3 makes determinism load-bearing for prompt caching, so an instability
here costs money in the agent runtime rather than failing anything locally.
"""

from __future__ import annotations

import hashlib

import pytest

from studium.retrieval.chunking import (
    ATOMIC_HARD_LIMIT,
    MAX_TOKENS,
    MIN_BODY_TOKENS,
    Block,
    blocks_from_markdown,
    chunk_blocks,
    chunk_text,
    classify_block,
    count_tokens,
)

PROSE = (
    "Beta reduction is the computational rule of the lambda calculus. "
    "A redex is an application whose left subterm is an abstraction. "
)


def body(times: int, *, path: tuple[str, ...] = ("Chapter 3",), page: int = 1) -> Block:
    return Block(text=PROSE * times, section_path=path, page=page)


def digest(chunks) -> str:
    joined = "\x00".join(f"{c.chunk_index}|{c.chunk_type}|{c.text}" for c in chunks)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


class TestDeterminism:
    """§17: "returns byte-identical output across multiple invocations"."""

    def test_repeated_chunking_is_byte_identical(self):
        blocks = [body(20), body(30), body(12)]
        first = chunk_blocks(blocks)
        assert first, "the fixture must actually produce chunks"
        for _ in range(5):
            assert digest(chunk_blocks(blocks)) == digest(first)

    def test_determinism_holds_across_every_chunk_type(self):
        blocks = [
            Block("# Chapter 3: Types", kind="heading", starts_section=True),
            body(15),
            Block("def f(x):\n    return x", kind="code"),
            Block(r"\begin{equation} E = mc^2 \end{equation}", kind="math"),
            Block("Figure 3.1: A reduction."),
            Block(PROSE * 4, section_path=("Exercises",)),
            Block(PROSE * 4, section_path=("References",)),
        ]
        first = chunk_blocks(blocks)
        kinds = {c.chunk_type for c in first}
        assert len(kinds) >= 5, f"coverage guard: only saw {kinds}"
        assert digest(chunk_blocks(blocks)) == digest(first)

    def test_the_fixture_corpus_is_not_empty(self):
        """§17's coverage guard: a determinism test over zero cases is green
        and worthless."""
        assert chunk_blocks([body(10)])


class TestChunkTypeAssignment:
    """§7 "Chunk type assignment"."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("3.2.1 Function Types", "heading"),
            ("Chapter 3: Types", "heading"),
            ("PRELIMINARIES", "heading"),
            ("Figure 3.1: The reduction relation.", "figure_caption"),
            ("Table 2: Typing rules.", "figure_caption"),
            ("An ordinary sentence of running prose that goes on for a while.", "body"),
        ],
    )
    def test_content_patterns(self, text, expected):
        assert classify_block(Block(text=text)) == expected

    def test_section_context_decides_exercises_and_references(self):
        """§7 detects these structurally: an exercise reads like a paragraph."""
        assert classify_block(Block(PROSE, section_path=("Ch 3", "Exercises"))) == "exercise"
        assert classify_block(Block(PROSE, section_path=("Problems",))) == "exercise"
        assert classify_block(Block(PROSE, section_path=("Bibliography",))) == "reference"
        assert classify_block(Block(PROSE, section_path=("References",))) == "reference"

    def test_ingestion_supplied_kind_wins_over_inference(self):
        """Markup is a better signal than a heuristic over extracted text."""
        looks_like_a_heading = Block(text="3.2 Types", kind="code")
        assert classify_block(looks_like_a_heading) == "code"

    def test_a_long_numbered_paragraph_is_not_a_heading(self):
        listy = Block(text="1. " + PROSE * 3)
        assert classify_block(listy) == "body"


class TestSizeTargets:
    """§7 "Target parameters"."""

    def test_no_body_chunk_exceeds_the_maximum(self):
        chunks = chunk_blocks([body(200)])
        oversized = [c for c in chunks if c.chunk_type == "body" and c.token_count > MAX_TOKENS]
        assert not oversized, [c.token_count for c in oversized]

    def test_chunks_land_near_the_target(self):
        chunks = [c for c in chunk_blocks([body(120)]) if c.chunk_type == "body"]
        assert len(chunks) > 2
        # The last chunk is whatever remains, so it is exempt from the floor.
        for chunk in chunks[:-1]:
            assert 200 <= chunk.token_count <= MAX_TOKENS, chunk.token_count

    def test_a_short_body_chunk_merges_into_its_neighbour(self):
        """§7: below 80 tokens a body chunk merges rather than standing alone."""
        blocks = [body(20), Block("A short trailing remark.", section_path=("Chapter 3",))]
        chunks = [c for c in chunk_blocks(blocks) if c.chunk_type == "body"]
        assert all(
            c.token_count >= MIN_BODY_TOKENS or c is chunks[-1] for c in chunks
        )
        assert "short trailing remark" in chunks[-1].text

    def test_short_atomic_chunks_are_exempt_from_the_minimum(self):
        """§7: code, math, captions and exercises may be smaller."""
        chunks = chunk_blocks([Block("x = 1", kind="code")])
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "code"
        assert chunks[0].token_count < MIN_BODY_TOKENS


class TestAtomicBlocks:
    """§7 "Special handling for code and math"."""

    def test_a_code_block_over_the_target_is_still_one_chunk(self):
        code = "\n".join(f"    line_{i} = compute({i})" for i in range(200))
        chunks = chunk_blocks([Block(code, kind="code")])
        assert len(chunks) == 1, "a code block under 2000 tokens stays whole"
        assert chunks[0].token_count > MAX_TOKENS

    def test_a_code_block_past_the_hard_limit_splits_at_a_blank_line(self):
        """§17's adversarial case: splits at a function boundary."""
        function = "def f_{i}(x):\n" + "\n".join(f"    y{j} = x + {j}" for j in range(40))
        code = "\n\n".join(function.replace("{i}", str(i)) for i in range(12))
        assert count_tokens(code) > ATOMIC_HARD_LIMIT

        chunks = chunk_blocks([Block(code, kind="code")])
        assert len(chunks) > 1
        assert all(c.chunk_type == "code" for c in chunks)
        for chunk in chunks[:-1]:
            assert chunk.text.rstrip().endswith(("0", "1", "2", "3", "4", "5", "6", "7", "8", "9")), (
                "each piece should end at a complete line, not mid-token"
            )
        # Nothing is lost: every function survives somewhere.
        rejoined = "".join(c.text for c in chunks)
        assert rejoined.count("def f_") == 12

    def test_math_is_atomic_too(self):
        math = r"\begin{equation}" + " x + y = z \\\\" * 30 + r"\end{equation}"
        chunks = chunk_blocks([Block(math, kind="math")])
        assert len(chunks) == 1
        assert chunks[0].chunk_type == "math"

    def test_prose_never_spans_a_code_fence(self):
        blocks = [body(5), Block("def f(): pass", kind="code"), body(5)]
        chunks = chunk_blocks(blocks)
        for chunk in chunks:
            if chunk.chunk_type == "body":
                assert "def f()" not in chunk.text


class TestBreakPreferences:
    """§7 "Break-preference hierarchy"."""

    def test_a_section_boundary_closes_the_previous_chunk(self):
        """Preference 1. A chunk must not straddle a section, or half its text
        is filed under the wrong section_path."""
        blocks = [
            Block(PROSE * 3, section_path=("Ch 3", "3.1"), starts_section=True),
            Block(PROSE * 3, section_path=("Ch 3", "3.2"), starts_section=True),
        ]
        chunks = [c for c in chunk_blocks(blocks) if c.chunk_type == "body"]
        paths = [tuple(c.section_path) for c in chunks]
        assert ("Ch 3", "3.1") in paths
        assert ("Ch 3", "3.2") in paths
        for chunk in chunks:
            if tuple(chunk.section_path) == ("Ch 3", "3.1"):
                # Overlap may carry a tail forward, but never a whole section.
                assert chunk.text.count("redex") <= 4

    def test_sentence_boundaries_are_preferred_over_word_boundaries(self):
        chunks = [c for c in chunk_blocks([body(150)]) if c.chunk_type == "body"]
        ends_at_sentence = sum(1 for c in chunks if c.text.rstrip().endswith("."))
        assert ends_at_sentence >= len(chunks) - 1

    def test_an_abbreviation_is_not_a_sentence_end(self):
        text = ("The reduction, i.e. the rewriting step, proceeds. " * 60)
        chunks = [c for c in chunk_blocks([Block(text)]) if c.chunk_type == "body"]
        for chunk in chunks:
            assert not chunk.text.rstrip().endswith("i.e."), chunk.text[-40:]

    def test_text_with_no_boundaries_falls_back_to_character_cutting(self):
        """§17's adversarial case: no sentence ends, no whitespace."""
        runaway = "x" * 8000
        chunks = chunk_text(runaway)
        assert len(chunks) > 1
        assert all(c.token_count <= MAX_TOKENS * 1.2 for c in chunks)
        assert "".join(c.text for c in chunks).count("x") == 8000


class TestOverlap:
    """§7 "Overlap": 15%, one-directional.

    Every sentence in this fixture is unique. Repetitive filler makes both
    assertions below unfalsifiable -- any window of it appears in every chunk,
    so "the tail carried forward" and "the tail did not" are indistinguishable.
    """

    @staticmethod
    def unique_prose(sentences: int) -> Block:
        return Block(
            text=" ".join(
                f"Sentence {i} establishes that term {i} reduces to value {i} "
                f"under the rule numbered {i}."
                for i in range(sentences)
            ),
            section_path=("Chapter 3",),
        )

    def test_a_later_chunk_carries_a_tail_of_the_one_before(self):
        chunks = [
            c for c in chunk_blocks([self.unique_prose(200)]) if c.chunk_type == "body"
        ]
        assert len(chunks) >= 2

        overlaps = [
            previous.text.rstrip()[-40:] in current.text
            for previous, current in zip(chunks, chunks[1:], strict=False)
        ]
        assert any(overlaps), "no chunk carried its predecessor's tail forward"

    def test_overlap_does_not_run_backwards(self):
        """One-directional: chunk N must not contain chunk N+1's own ending."""
        chunks = [
            c for c in chunk_blocks([self.unique_prose(200)]) if c.chunk_type == "body"
        ]
        assert len(chunks) >= 2
        for previous, current in zip(chunks, chunks[1:], strict=False):
            ending = current.text.rstrip()[-40:]
            assert ending not in previous.text

    def test_overlap_starts_at_a_word_boundary(self):
        """A tail cut mid-word degrades the embedding of the chunk carrying it."""
        chunks = [
            c for c in chunk_blocks([self.unique_prose(200)]) if c.chunk_type == "body"
        ]
        for chunk in chunks[1:]:
            first_word = chunk.text.split()[0].strip(".,;:")
            assert first_word.isalnum() or first_word.isalpha(), chunk.text[:50]


class TestStructurePreservation:
    def test_section_path_is_inherited_from_the_block(self):
        chunks = chunk_blocks([body(20, path=("Ch 3", "3.2", "3.2.1"))])
        for chunk in chunks:
            assert chunk.section_path == ["Ch 3", "3.2", "3.2.1"]

    def test_markdown_headings_build_the_path(self):
        blocks = blocks_from_markdown("# A\n\ntext one\n\n## B\n\ntext two\n\n# C\n\ntext three")
        paths = [b.section_path for b in blocks if b.kind is None]
        assert paths == [("A",), ("A", "B"), ("C",)]

    def test_pages_span_the_blocks_a_chunk_covers(self):
        blocks = [body(6, page=10), body(6, page=11), body(6, page=12)]
        chunks = [c for c in chunk_blocks(blocks) if c.chunk_type == "body"]
        assert chunks[0].page_start == 10
        assert chunks[-1].page_end >= chunks[0].page_start

    def test_as_row_carries_every_column_the_schema_needs(self):
        chunk = chunk_blocks([body(10)])[0]
        row = chunk.as_row(source_id="src")
        assert set(row) == {
            "source_id", "chunk_index", "text", "token_count",
            "page_start", "page_end", "section_path", "chunk_type",
        }


class TestRealCorpusRegressions:
    """Defects found by running the chunker over the Michaelson book.

    Both were invisible against the synthetic fixtures. They are pinned here
    rather than left to the diagnostic script, which needs a PDF and is not
    part of any tier.
    """

    def test_overlap_cannot_push_a_chunk_past_the_maximum(self):
        """The overlap is prepended after the body is sized, so without a guard
        it escapes the size check: a 583-token paragraph plus an 87-token carry
        produced a 670-token chunk. 7.6% of Michaelson chunks exceeded the max.
        """
        para = "The redex contracts by substitution into the body of the abstraction. " * 30
        assert MAX_TOKENS * 0.9 < count_tokens(para) <= MAX_TOKENS, (
            "the fixture must sit just under the ceiling, or it proves nothing"
        )

        chunks = chunk_blocks([Block(text=para, section_path=("Ch",))] * 2)

        assert len(chunks) >= 2, "two paragraphs must produce a carry"
        oversized = [c.token_count for c in chunks if c.token_count > MAX_TOKENS]
        assert not oversized, f"chunks over the {MAX_TOKENS} maximum: {oversized}"

    def test_the_overlap_survives_when_there_is_room_for_it(self):
        """The guard trims; it must not silently disable overlap entirely."""
        blocks = [
            Block(text=" ".join(f"Sentence {i} about reduction." for i in range(120)),
                  section_path=("Ch",))
        ]
        chunks = [c for c in chunk_blocks(blocks) if c.chunk_type == "body"]
        assert len(chunks) >= 2
        assert any(
            previous.text.rstrip()[-30:] in current.text
            for previous, current in zip(chunks, chunks[1:], strict=False)
        ), "trimming removed the overlap altogether"

    @pytest.mark.parametrize(
        ("ligature", "expected"),
        [("deﬁnition", "definition"), ("ﬂag", "flag"), ("eﬀect", "effect")],
    )
    def test_ligatures_are_folded(self, ligature, expected):
        """Postgres tokenises 'deﬁnition' to 'deﬁnit' and 'definition' to
        'definit'; they do not match, so a ligature hides the chunk from the
        keyword half of hybrid search for a word it plainly contains."""
        [chunk] = chunk_blocks([Block(text=f"{ligature} " * 40)])
        assert expected in chunk.text
        assert ligature not in chunk.text

    def test_folding_leaves_mathematical_notation_alone(self):
        """Targeted rather than NFKC: in this corpus the notation is content."""
        source = "The term λx.M applies to N, giving M[x := N] with β-reduction."
        [chunk] = chunk_blocks([Block(text=source + " padding." * 60)])
        assert "λx.M" in chunk.text
        assert "β-reduction" in chunk.text

    def test_folding_does_not_break_determinism(self):
        blocks = [Block(text="deﬁnition of the ﬁrst eﬀective ﬂag. " * 30)]
        assert digest(chunk_blocks(blocks)) == digest(chunk_blocks(blocks))


class TestIndexing:
    def test_chunk_indexes_are_dense_and_ordered(self):
        chunks = chunk_blocks([body(30), Block("def f(): pass", kind="code"), body(30)])
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))

    def test_empty_input_produces_no_chunks(self):
        assert chunk_blocks([]) == []
        assert chunk_blocks([Block("   ")]) == []

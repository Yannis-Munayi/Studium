"""Tier 1: citation parsing and thin-grounding detection (§12, §13, §17)."""

from __future__ import annotations

import uuid

import pytest

from studium.retrieval.citations import (
    EXCERPT_CHARS,
    _excerpt,
    has_invalid_citation,
    parse_citation_markers,
)
from studium.retrieval.types import (
    THIN_GROUNDING_MIN_AVG_SCORE,
    THIN_GROUNDING_MIN_COUNT,
    Passage,
    detect_thin_grounding,
)


def passage(score: float | None = None, *, index: int = 0) -> Passage:
    return Passage(
        chunk_id=uuid.UUID(f"00000000-0000-7000-8000-00000000000{index}"),
        text="the reduction of a beta-redex proceeds by substitution",
        relevance_score=score,
    )


class TestMarkerParsing:
    """§17: parse_citation_markers("[P1] and [P3-P5]") returns [1, 3, 4, 5]."""

    def test_the_documented_example(self):
        assert parse_citation_markers("[P1] and [P3-P5]") == [1, 3, 4, 5]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("[P3]", [3]),
            ("[P3-P5]", [3, 4, 5]),
            ("[P3-5]", [3, 4, 5]),
            ("[P1][P4]", [1, 4]),
            ("no markers here", []),
            ("[P1] then [P1] again", [1]),
        ],
    )
    def test_supported_forms(self, text, expected):
        assert parse_citation_markers(text) == expected

    def test_comma_separated_form_yields_nothing_rather_than_half(self):
        """§12 does not support ``[P1, P4]``; the agent prompts instruct
        against it, so seeing one is a prompt-compliance problem.

        It parses to nothing rather than to ``[1]`` deliberately. Extracting
        the first number would attach one citation and drop the other
        silently, and the artifact would look correctly grounded. Extracting
        none leaves the marker unresolvable, which the frontend renders as a
        dead marker -- visible, and traceable back to the prompt that caused
        it."""
        assert parse_citation_markers("[P1, P4]") == []

    def test_results_are_ascending_and_deduplicated(self):
        assert parse_citation_markers("[P5] [P2] [P3-P4] [P2]") == [2, 3, 4, 5]


class TestAdversarialMarkers:
    """§17's adversarial cases. None of these may raise: a malformed marker in
    a stream the learner is reading is a review flag, not an exception."""

    @pytest.mark.parametrize(
        "text",
        [
            "[P1",
            "P1]",
            "[[P1]]",
            "[P]",
            "[P-]",
            "[P1-]",
            "[]",
            "[P0]",
            "[P999999999999999999]",
        ],
    )
    def test_malformed_markers_do_not_raise(self, text):
        parse_citation_markers(text)

    def test_a_reversed_range_reads_as_its_first_bound(self):
        """Expanding backwards would invent an intent; dropping it would lose a
        citation that names a real passage."""
        assert parse_citation_markers("[P5-P3]") == [5]

    def test_out_of_range_markers_are_dropped_when_a_maximum_is_given(self):
        assert parse_citation_markers("[P1] [P9]", maximum=6) == [1]

    def test_zero_and_negative_numbers_are_dropped(self):
        assert parse_citation_markers("[P0]") == []

    def test_unicode_around_markers_is_irrelevant(self):
        assert parse_citation_markers("La réduction (λx.M)N [P2] — voir aussi [P4].") == [2, 4]

    def test_a_marker_inside_a_code_block_still_parses(self):
        """Deliberate: the parser has no notion of context, and a false
        positive costs one spurious citation while a miss costs a real one."""
        assert parse_citation_markers("`arr[P1]`") == [1]


class TestInvalidCitationDetection:
    def test_a_number_beyond_the_supplied_count_is_invalid(self):
        assert has_invalid_citation("See [P9].", passage_count=6) is True
        assert has_invalid_citation("See [P6].", passage_count=6) is False

    def test_a_range_whose_end_overflows_is_invalid(self):
        assert has_invalid_citation("[P1-P5]", passage_count=2) is True

    def test_no_markers_is_not_invalid(self):
        assert has_invalid_citation("plain prose", passage_count=0) is False

    def test_any_marker_with_zero_passages_is_invalid(self):
        assert has_invalid_citation("[P1]", passage_count=0) is True


class TestThinGroundingThresholds:
    """§17: "fires at 2 passages, does not fire at 3, fires at avg_score 0.49,
    does not fire at 0.51"."""

    def test_fires_at_two_passages(self):
        thin, reason = detect_thin_grounding([passage(0.9, index=i) for i in range(2)])
        assert thin is True
        assert "2 passage" in reason

    def test_does_not_fire_at_three_passages(self):
        thin, _ = detect_thin_grounding([passage(0.9, index=i) for i in range(3)])
        assert thin is False

    def test_fires_at_an_average_of_0_49(self):
        thin, reason = detect_thin_grounding([passage(0.49, index=i) for i in range(4)])
        assert thin is True
        assert "0.49" in reason

    def test_does_not_fire_at_an_average_of_0_51(self):
        thin, _ = detect_thin_grounding([passage(0.51, index=i) for i in range(4)])
        assert thin is False

    def test_the_boundary_is_exclusive(self):
        """"below 0.5" -- exactly 0.5 is not thin."""
        thin, _ = detect_thin_grounding([passage(0.5, index=i) for i in range(4)])
        assert thin is False

    def test_zero_passages_is_thin(self):
        thin, reason = detect_thin_grounding([])
        assert thin is True
        assert reason

    def test_the_thresholds_are_the_documented_values(self):
        assert THIN_GROUNDING_MIN_COUNT == 3
        assert THIN_GROUNDING_MIN_AVG_SCORE == 0.5

    def test_unscored_passages_only_trip_the_count_condition(self):
        """A retriever that returns no scores -- the curated-pointer one --
        must not be judged as if every score were zero."""
        thin, _ = detect_thin_grounding([passage(None, index=i) for i in range(4)])
        assert thin is False

    def test_the_reason_names_the_numbers_a_reviewer_needs(self):
        _, reason = detect_thin_grounding([passage(0.2, index=i) for i in range(5)])
        assert "5 passages" in reason
        assert "0.20" in reason


class TestExcerpts:
    def test_a_short_chunk_is_its_own_excerpt(self):
        text = "A short passage."
        excerpt, start, end = _excerpt(text, None)
        assert excerpt == text
        assert (start, end) == (0, len(text))

    def test_a_long_chunk_is_truncated_at_a_word_boundary(self):
        text = "word " * 400
        excerpt, start, end = _excerpt(text, None)
        assert len(excerpt) <= EXCERPT_CHARS + 3
        assert excerpt.endswith("...")
        assert start == 0 and end <= EXCERPT_CHARS

    def test_a_recorded_quoted_span_wins(self):
        """It is the passage the agent actually leaned on."""
        text = "Preamble. The redex reduces by substitution. Afterword."
        span = "The redex reduces by substitution."
        excerpt, start, end = _excerpt(text, span)
        assert excerpt == span
        assert text[start:end] == span

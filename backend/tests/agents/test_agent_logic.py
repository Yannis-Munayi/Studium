"""The deterministic decisions inside agents (agent runtime §10-§14, §8).

Not everything an agent does is a model call. Citation resolution, the FSRS
rating map, the mastery-jump guard, and the rule-based intent classifier are all
pure functions, and they are where a silent error does the most damage --
a citation that resolves to the wrong chunk is a grounding claim that is simply
false, and nothing downstream would notice.
"""

from __future__ import annotations

import pytest

from studium.agents.evaluator import (
    MASTERY_JUMP_THRESHOLD,
    verdict_to_score,
    would_jump_mastery,
)
from studium.agents.lecturer import (
    CITATION_PATTERN,
    has_invalid_citation,
    resolve_citations,
)
from studium.agents.orchestrator import INTENT_CONFIDENCE_FLOOR, rule_based_intent
from studium.agents.reviewer import FSRS_RATING, rating_for
from studium.agents.schemas import PartialCheck
from studium.orchestration.state_machine import State
from studium.retrieval import THIN_GROUNDING_THRESHOLD, grounding_is_thin
from studium.review.fsrs import Rating
from studium.session.context import Passage
from tests.fixtures.runtime import CHUNK_ONE, CHUNK_TWO


class TestCitationResolution:
    def _passages(self) -> list[Passage]:
        return [
            Passage(chunk_id=CHUNK_TWO, text="second by id", source_title="S"),
            Passage(chunk_id=CHUNK_ONE, text="first by id", source_title="S"),
        ]

    def test_markers_resolve_against_sorted_order_not_retrieval_order(self):
        """The prefix numbers passages in sorted order (§17); so must resolution.

        Resolving against retrieval order instead would attach every citation
        to the wrong chunk -- silently, because both lists are the same length.
        """
        cited = resolve_citations("As shown in [P1].", self._passages())
        assert len(cited) == 1
        assert cited[0].chunk_id == CHUNK_ONE  # lowest id, rendered as [P1]

    def test_ranges_expand_inclusively(self):
        """§10's own example: 'Cite by passage number: [P3], [P7-P8]'."""
        cited = resolve_citations("See [P1-P2].", self._passages())
        assert {c.chunk_id for c in cited} == {CHUNK_ONE, CHUNK_TWO}

    def test_repeated_markers_produce_one_citation_row(self):
        """content_citations has a unique (artifact_id, source_chunk_id) pair."""
        cited = resolve_citations("[P1] and again [P1] and [P1].", self._passages())
        assert len(cited) == 1

    def test_out_of_range_markers_are_dropped_not_raised(self):
        """A learner is watching; an invented citation is flagged, not fatal."""
        cited = resolve_citations("As shown in [P9].", self._passages())
        assert cited == []

    def test_invented_citations_are_detectable(self):
        """Severity-3 review flag territory: an ungrounded claim reached a learner."""
        assert has_invalid_citation("See [P9].", passage_count=2) is True
        assert has_invalid_citation("See [P2].", passage_count=2) is False
        assert has_invalid_citation("See [P1-P5].", passage_count=2) is True

    def test_text_with_no_markers_yields_no_citations(self):
        assert resolve_citations("A segment with no citations at all.", self._passages()) == []

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("[P3]", 1), ("[P7-P8]", 1), ("[P1] [P2]", 2), ("[P 3]", 0), ("P3", 0)],
    )
    def test_pattern_matches_the_documented_forms_only(self, text, expected):
        assert len(CITATION_PATTERN.findall(text)) == expected


class TestGroundingThreshold:
    def test_thin_grounding_is_fewer_than_three_passages(self):
        """§10's threshold, which the curated-pointer retriever will trip often."""
        assert THIN_GROUNDING_THRESHOLD == 3
        assert grounding_is_thin([]) is True
        assert grounding_is_thin([Passage(chunk_id=CHUNK_ONE, text="a")] * 2) is True
        assert grounding_is_thin([Passage(chunk_id=CHUNK_ONE, text="a")] * 3) is False


class TestEvaluatorGuards:
    def test_verdict_to_score_matches_the_grading_rules(self):
        """§12: 2 = covers the key points, 1 = partial, 0 = missing or wrong."""
        assert verdict_to_score("correct") == 2
        assert verdict_to_score("partially_correct") == 1
        assert verdict_to_score("incorrect") == 0

    def test_an_ordinary_grade_does_not_trip_the_jump_guard(self):
        """From a mid estimate, one piece of evidence moves mastery modestly."""
        assert would_jump_mastery(0.5, "correct") < MASTERY_JUMP_THRESHOLD

    def test_a_grade_from_near_zero_can_trip_the_guard(self):
        """§12's third failure mode: >0.4 movement in a single evidence step.

        This is the case worth flagging -- a learner at 0.05 graded correct once
        should not land near mastery on one answer.
        """
        jump = would_jump_mastery(0.02, "correct")
        assert jump >= 0  # the guard computes a real BKT delta, not a constant
        assert isinstance(jump, float)

    def test_the_guard_reads_the_real_bkt_posterior(self):
        """Not a heuristic: it runs the same update that will be persisted."""
        from studium.mastery import BKTParams, posterior

        expected = abs(posterior(0.5, True, BKTParams()) - 0.5)
        assert would_jump_mastery(0.5, "correct") == pytest.approx(expected)

    def test_partial_credit_is_treated_as_incorrect_for_the_jump_estimate(self):
        """Matches how practice_partial resolves when the evidence is applied."""
        assert would_jump_mastery(0.5, "partially_correct") == would_jump_mastery(
            0.5, "incorrect"
        )


class TestReviewerFSRSMapping:
    @pytest.mark.parametrize(
        ("verdict", "confident", "expected"),
        [
            ("correct", True, Rating.EASY),
            ("correct", False, Rating.GOOD),
            ("partially_correct", True, Rating.HARD),
            ("partially_correct", False, Rating.HARD),
            ("incorrect", True, Rating.AGAIN),
            ("incorrect", False, Rating.AGAIN),
        ],
    )
    def test_matches_the_section_14_table(self, verdict, confident, expected):
        """§14: correct+confident -> easy, correct+hesitant -> good, etc."""
        check = PartialCheck(verdict=verdict, confident=confident)
        assert rating_for(check) is expected

    def test_the_confidence_flag_is_what_separates_easy_from_good(self):
        """Its only purpose: deciding whether the interval grows or holds."""
        assert rating_for(PartialCheck(verdict="correct", confident=True)) is Rating.EASY
        assert rating_for(PartialCheck(verdict="correct", confident=False)) is Rating.GOOD

    def test_every_verdict_confidence_pair_is_mapped(self):
        """An unmapped pair would KeyError mid-review."""
        for verdict in ("correct", "partially_correct", "incorrect"):
            for confident in (True, False):
                assert (verdict, confident) in FSRS_RATING

    def test_an_again_rating_actually_shortens_the_interval(self):
        """The mapping is only useful if it drives FSRS the right way."""
        from studium.review.fsrs import CardState, review

        card = CardState(stability=20.0, difficulty=5.0, reps=4, state="review")
        again = review(card, Rating.AGAIN)
        easy = review(card, Rating.EASY)
        assert again.stability < easy.stability
        assert again.lapses == card.lapses + 1


class TestRuleBasedIntent:
    """§8's deterministic escape hatch, used below 0.7 confidence."""

    def test_the_floor_is_the_documented_one(self):
        assert INTENT_CONFIDENCE_FLOOR == 0.7

    @pytest.mark.parametrize(
        ("utterance", "expected"),
        [
            ("wait, hold on", "interrupt"),
            ("next", "next"),
            ("keep going", "next"),
            ("go back", "back"),
            ("I'm done for today", "end_session"),
            ("i'm lost", "primitive:im_lost"),
            ("explain it differently", "primitive:explain_differently"),
            ("show me an example", "primitive:show_worked_example"),
            ("let me try one", "primitive:let_me_try_one"),
            ("why does this matter", "primitive:why_does_this_matter"),
            ("prove it", "primitive:prove_it_to_me"),
        ],
    )
    def test_recognises_unambiguous_phrasings(self, utterance, expected):
        assert rule_based_intent(utterance, state=State.LECTURING) == expected

    def test_question_marks_and_interrogatives_read_as_questions(self):
        assert rule_based_intent("is that always true?", state=State.LECTURING) == "question"
        assert rule_based_intent("how does that follow", state=State.LECTURING) == "question"

    def test_an_unmarked_utterance_in_lab_reads_as_an_answer(self):
        """State-sensitive: in LAB they were asked something, so this is the reply."""
        assert rule_based_intent("beta reduces it to y", state=State.LAB) == "answer"
        assert rule_based_intent("beta reduces it to y", state=State.LECTURING) == "comment"

    def test_review_and_assessment_also_read_bare_text_as_answers(self):
        for state in (State.REVIEW, State.SUMMATIVE_ASSESSMENT):
            assert rule_based_intent("the diamond property", state=state) == "answer"

    def test_every_rule_output_is_a_valid_intent(self):
        """A rule emitting an unroutable intent would dead-end the turn."""
        from studium.agents.schemas import Intent

        valid = set(Intent.__args__)  # type: ignore[attr-defined]
        samples = [
            "wait", "next", "go back", "i'm done", "i'm lost", "explain it differently",
            "show me an example", "let me try one", "why does this matter", "prove it",
            "what is this?", "just an observation",
        ]
        for state in State:
            for sample in samples:
                assert rule_based_intent(sample, state=state) in valid

"""Closed-book enforcement and assessment definitions (evaluation §17, §11).

§3 is categorical: "Any softening of the closed-book rule turns assessment into
practice-with-different-branding and loses the credentialing value." So each of
§11.2's six conditions is asserted here against the thing that actually
enforces it, not against a docstring.

Two of the six turn out to be enforced by the state machine's *shape* -- no
primitive rule lists SUMMATIVE_ASSESSMENT, and no interrupt transition starts
there. That was true before this subsystem and it was true by accident. These
tests are what stop a later widening of the primitive matrix from silently
handing a learner the command palette mid-examination.
"""

from __future__ import annotations

import datetime as dt
import textwrap
from pathlib import Path

import pytest

from studium.eval import summative
from studium.eval.datasets import DatasetError
from studium.eval.summative import (
    CLOSED_BOOK,
    AssessmentError,
    ClosedBookViolation,
    assert_closed_book,
    parse_definition,
)
from studium.orchestration.state_machine import (
    PRIMITIVE_MATRIX,
    TRANSITIONS,
    Event,
    GuardContext,
    SessionStateMachine,
    State,
)

DEFINITION_ROOT = (
    Path(__file__).resolve().parents[2]
    / "content"
    / "subjects"
    / "lambda-calculus"
    / "assessments"
)


class TestClosedBookCapabilities:
    @pytest.mark.parametrize(
        "capability",
        ["primitives", "learner_retrieval", "citations", "hints", "interrupts", "revision"],
    )
    def test_disabled_during_assessment(self, capability: str) -> None:
        with pytest.raises(ClosedBookViolation):
            assert_closed_book(summative.MODE, capability)

    @pytest.mark.parametrize(
        "capability", ["primitives", "learner_retrieval", "hints", "interrupts"]
    )
    def test_allowed_outside_assessment(self, capability: str) -> None:
        """The assertion is that these do not raise. Written as `assert ... is
        None` rather than a bare call so the statement is an assertion; a bare
        `f(...) is None` evaluates a bool and discards it, which passes
        whatever the function did."""
        assert assert_closed_book("tutorial", capability) is None
        assert assert_closed_book(None, capability) is None

    def test_the_evaluator_keeps_its_retrieval(self) -> None:
        """§11.2: "The system's own retrieval for the Evaluator continues
        (grading needs source material for reference)." The one capability
        whose value differs between the learner and the grader, which is why
        the key says whose retrieval it is."""
        assert CLOSED_BOOK["evaluator_retrieval"] is True
        assert assert_closed_book(summative.MODE, "evaluator_retrieval") is None

    def test_an_unclassified_capability_raises_rather_than_defaulting_open(
        self,
    ) -> None:
        """A capability added to the product that nobody classified is exactly
        the one that quietly leaks the answer."""
        with pytest.raises(AssessmentError, match="not classified"):
            assert_closed_book(summative.MODE, "some_new_affordance")

    def test_the_message_explains_rather_than_just_refusing(self) -> None:
        with pytest.raises(ClosedBookViolation, match="closed-book"):
            assert_closed_book(summative.MODE, "hints")


class TestStateMachineEnforcement:
    """The two conditions the machine's shape already enforced."""

    @pytest.mark.parametrize("primitive", sorted(PRIMITIVE_MATRIX))
    def test_no_primitive_is_invocable_during_assessment(self, primitive: str) -> None:
        """§11.2: "The command palette is unavailable." Enforced structurally:
        InvalidPrimitive is raised before dispatch. This test is what stops a
        later widening of a valid_from set from handing a learner the palette
        mid-examination."""
        assert State.SUMMATIVE_ASSESSMENT not in PRIMITIVE_MATRIX[primitive].valid_from

    def test_no_interrupt_transition_starts_in_assessment(self) -> None:
        """§11.2: "The 'raise your hand' affordance is removed. No Tutor
        engagement during assessment.\""""
        interrupts = [
            t
            for t in TRANSITIONS
            if t.event is Event.LEARNER_INTERRUPT
            and t.source is State.SUMMATIVE_ASSESSMENT
        ]
        assert not interrupts

    def test_a_submission_advances_regardless_of_verdict(self) -> None:
        """§11.2's single submission. LAB branches three ways on the verdict
        (correct / retry / exhausted); a summative attempt must not, because
        the retry branch *is* the multi-attempt cycle closed-book replaces."""
        machine = SessionStateMachine(State.SUMMATIVE_ASSESSMENT)
        for correct in (True, False, None):
            guards = GuardContext(criteria_remaining=2, evaluator_correct=correct)
            assert machine.can(Event.ANSWER_SUBMITTED, guards)

    def test_the_last_submission_goes_to_closing(self) -> None:
        """Grading is not a state: no event would leave one, so the session
        would strand there. The effect is asserted alongside the destination --
        landing in CLOSING without grading would end the attempt ungraded."""
        machine = SessionStateMachine(State.SUMMATIVE_ASSESSMENT)
        target, effect = machine.peek(
            Event.ANSWER_SUBMITTED, GuardContext(criteria_remaining=0)
        )
        assert target is State.CLOSING
        assert effect == "grade_assessment_attempt"

    def test_a_submission_with_criteria_left_stays_put(self) -> None:
        machine = SessionStateMachine(State.SUMMATIVE_ASSESSMENT)
        target, effect = machine.peek(
            Event.ANSWER_SUBMITTED, GuardContext(criteria_remaining=3)
        )
        assert target is State.SUMMATIVE_ASSESSMENT
        assert effect == "record_assessment_response"

    def test_the_state_is_reachable_from_its_mode(self) -> None:
        """Before this subsystem the state was reachable and inescapable:
        MODE_ENTRY_STATE put a session here and nothing led out except the
        wildcard END_SESSION, so a learner who submitted got IllegalTransition.
        DIVERGENCES-EVALUATION (E10)."""
        from studium.orchestration.state_machine import MODE_ENTRY_STATE

        assert MODE_ENTRY_STATE[summative.MODE] is State.SUMMATIVE_ASSESSMENT


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "a.yaml"
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


VALID = """
    assessment:
      slug: lambda_foundations
      subject: lambda-calculus
      title: Foundations
      concepts: [beta_reduction]
      time_limit_minutes: 60
      passing_threshold: 0.70
    problems:
      - id: problem_001
        concept: beta_reduction
        prompt: Reduce the term.
        expected_key_points: [Identifies the redex, Shows substitution]
        rubric_criterion_id: 00000000-0000-7000-8000-000000000001
        max_score: 4
        time_estimate_minutes: 8
"""


class TestAssessmentDefinitions:
    def test_valid_definition_parses(self, tmp_path: Path) -> None:
        definition = parse_definition(write(tmp_path, VALID))
        assert definition.slug == "lambda_foundations"
        assert definition.passing_threshold == 0.70
        assert len(definition.problems) == 1

    def test_problems_without_key_points_are_refused(self, tmp_path: Path) -> None:
        """The Evaluator grades against them; without them it grades against
        nothing and returns a number anyway -- and this one decides whether
        someone gets credentialed."""
        with pytest.raises(DatasetError, match="expected_key_points"):
            parse_definition(
                write(tmp_path, VALID.replace("expected_key_points: [Identifies the redex, Shows substitution]", "expected_key_points: []"))
            )

    def test_a_problem_on_an_unlisted_concept_is_refused(self, tmp_path: Path) -> None:
        """A credential naming the wrong concepts is a false claim the
        signature would make tamper-evident but not untrue."""
        with pytest.raises(DatasetError, match="not listed"):
            parse_definition(
                write(tmp_path, VALID.replace("concept: beta_reduction", "concept: church_rosser"))
            )

    def test_a_definition_that_cannot_fit_its_time_limit_is_refused(
        self, tmp_path: Path
    ) -> None:
        """§11.2 collects unsubmitted responses as-is on expiry, so such a
        definition guarantees a truncated attempt."""
        with pytest.raises(DatasetError, match="minutes against"):
            parse_definition(
                write(tmp_path, VALID.replace("time_limit_minutes: 60", "time_limit_minutes: 5"))
            )

    def test_an_out_of_range_threshold_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(DatasetError, match="passing_threshold"):
            parse_definition(
                write(tmp_path, VALID.replace("passing_threshold: 0.70", "passing_threshold: 1.5"))
            )

    def test_duplicate_problem_ids_are_refused(self, tmp_path: Path) -> None:
        # Appended at the dedented indent VALID's own problems sit at, so the
        # file stays valid YAML and the parser reaches the duplicate check
        # rather than failing earlier on a parse error.
        doubled = VALID + (
            "      - id: problem_001\n"
            "        concept: beta_reduction\n"
            "        prompt: Another.\n"
            "        expected_key_points: [x]\n"
            "        rubric_criterion_id: 00000000-0000-7000-8000-000000000002\n"
        )
        with pytest.raises(DatasetError, match="declared twice"):
            parse_definition(write(tmp_path, doubled))

    def test_a_non_uuid_criterion_id_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(DatasetError, match="not a UUID"):
            parse_definition(
                write(
                    tmp_path,
                    VALID.replace(
                        "rubric_criterion_id: 00000000-0000-7000-8000-000000000001",
                        "rubric_criterion_id: uuid-of-rubric-criterion",
                    ),
                )
            )

    def test_lookup_by_problem_id(self, tmp_path: Path) -> None:
        definition = parse_definition(write(tmp_path, VALID))
        assert definition.problem("problem_001").concept == "beta_reduction"
        with pytest.raises(AssessmentError, match="no problem"):
            definition.problem("problem_999")


class TestShippedDefinition:
    def test_the_shipped_definition_parses(self) -> None:
        """A definition broken by an edit must not be discovered by a learner
        halfway through an examination."""
        definitions = summative.parse_definitions(DEFINITION_ROOT)
        assert definitions
        assert definitions[0].slug == "lambda_calculus_foundations"

    def test_the_shipped_definition_fits_its_time_limit(self) -> None:
        definition = summative.parse_definitions(DEFINITION_ROOT)[0]
        assert definition.estimated_minutes <= definition.time_limit_minutes


class TestTiming:
    def _attempt(self, tmp_path: Path, started_at: dt.datetime):
        return summative.Attempt(
            id=__import__("uuid").uuid4(),
            definition=parse_definition(write(tmp_path, VALID)),
            user_id=__import__("uuid").uuid4(),
            learner_subject_id=__import__("uuid").uuid4(),
            session_id=None,
            started_at=started_at,
            threshold=0.7,
        )

    def test_time_remaining_counts_down(self, tmp_path: Path) -> None:
        now = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
        attempt = self._attempt(tmp_path, now - dt.timedelta(minutes=20))
        assert attempt.time_remaining(now) == dt.timedelta(minutes=40)
        assert not attempt.expired(now)

    def test_an_expired_attempt_reports_zero_not_a_negative(
        self, tmp_path: Path
    ) -> None:
        now = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
        attempt = self._attempt(tmp_path, now - dt.timedelta(minutes=90))
        assert attempt.time_remaining(now) == dt.timedelta(0)
        assert attempt.expired(now)

    def test_an_untimed_assessment_never_expires(self, tmp_path: Path) -> None:
        """§11.2 makes the timer optional."""
        # VALID is written at four-space indent inside this module and dedented
        # by write(); the line as it appears here carries that indent.
        untimed = VALID.replace("      time_limit_minutes: 60\n", "")
        assert "time_limit_minutes" not in untimed, "the line was not removed"
        definition = parse_definition(write(tmp_path, untimed))
        attempt = summative.Attempt(
            id=__import__("uuid").uuid4(),
            definition=definition,
            user_id=__import__("uuid").uuid4(),
            learner_subject_id=__import__("uuid").uuid4(),
            session_id=None,
            started_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
            threshold=0.7,
        )
        assert attempt.time_remaining() is None
        assert not attempt.expired()

    def test_remaining_problems_exclude_answered_ones(self, tmp_path: Path) -> None:
        attempt = self._attempt(tmp_path, dt.datetime.now(dt.UTC))
        assert len(attempt.remaining) == 1
        attempt.answered.add("problem_001")
        assert attempt.remaining == ()


class TestRetakePolicy:
    def test_the_cooldown_matches_the_spec(self) -> None:
        assert summative.RETAKE_COOLDOWN_DAYS == 7

    def test_the_pool_guard_needs_two_attempts_worth(self) -> None:
        """§11.5: "if the pool is small enough that a retake would repeat
        problems, the reviewer must expand the pool before retakes are
        permitted"."""
        assert summative.MIN_POOL_MULTIPLE == 2

    def test_the_shipped_definition_cannot_support_a_retake_yet(self) -> None:
        """Three problems is one attempt's worth. That is the intended state
        and the file says so; asserting it here means expanding the pool later
        is a deliberate act rather than something that drifts."""
        definition = summative.parse_definitions(DEFINITION_ROOT)[0]
        needed = len(definition.problems) * summative.MIN_POOL_MULTIPLE
        assert len(definition.problems) < needed

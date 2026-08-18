"""The Evaluator: grading (agent runtime §12).

Deliberately isolated from any agent whose tone might inflate grades. The
Evaluator never sees the Tutor's encouraging framing -- it sees the rubric and
the answer, and nothing else. That isolation is the "grading integrity"
principle inherited from the draft's ``assess.py``, and it is enforced
structurally: the prefix carries the rubric, the suffix carries the answer, and
neither is assembled from another agent's output.

Two things this agent does **not** do:

* **Decide whether the learner passed.** That is ``score >= threshold``,
  computed in ``studium.assessment.compute_pass``. Data layer §3's first
  invariant is that no model judges a pass.
* **Compute the score.** ``studium.assessment.score_attempt`` does the weighted
  mean. Asking a model to do arithmetic over its own grades is how a rubric
  with five criteria silently becomes a rubric with four.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from studium.acl import wrap_user_content
from studium.llm.prompts import build_prefix
from studium.mastery import BKTParams, posterior

from .base import Agent, AgentInput, AgentOutput, ToolEffect
from .schemas import GradeReport, PartialCheck

log = logging.getLogger(__name__)

#: §12 failure mode: "a grade that would move a mastery estimate by more than
#: 0.4 in a single evidence step (unusual, worth flagging)".
MASTERY_JUMP_THRESHOLD = 0.4

#: Which mastery event a verdict produces, per context. The lecture-check and
#: practice families are separate enum values in the data layer (§6.5), so the
#: kind of grading decides the kind of evidence.
_EVIDENCE_KIND = {
    ("grade_check", "correct"): "lecture_check_correct",
    ("grade_check", "partially_correct"): "lecture_check_incorrect",
    ("grade_check", "incorrect"): "lecture_check_incorrect",
    ("grade_practice", "correct"): "practice_correct",
    ("grade_practice", "partially_correct"): "practice_partial",
    ("grade_practice", "incorrect"): "practice_incorrect",
    ("check_partial", "correct"): "review_correct",
    ("check_partial", "partially_correct"): "review_incorrect",
    ("check_partial", "incorrect"): "review_incorrect",
}

SINGLE_CRITERION_KINDS = frozenset({"grade_check", "grade_practice", "check_partial"})


def verdict_to_score(verdict: str) -> int:
    return {"correct": 2, "partially_correct": 1, "incorrect": 0}[verdict]


def would_jump_mastery(
    current: float, verdict: str, *, params: BKTParams | None = None
) -> float:
    """How far this verdict moves the BKT posterior.

    Run before emitting evidence, so an anomalous grade is flagged at the point
    it is produced rather than discovered later in the mastery history.
    Partial credit is treated as incorrect for the jump estimate, matching how
    ``practice_partial`` is resolved by the caller.
    """
    p = params or BKTParams()
    updated = posterior(current, verdict == "correct", p)
    return abs(updated - current)


class Evaluator(Agent):
    identity = "evaluator"
    kinds = frozenset({"grade_check", "grade_practice", "grade_assessment", "check_partial"})

    async def handle(self, input: AgentInput) -> AgentOutput:
        self.check_kind(input)
        if input.kind == "grade_assessment":
            return await self._grade_assessment(input)
        return await self._grade_single(input)

    # --- single-criterion grading -----------------------------------------

    async def _grade_single(self, input: AgentInput) -> AgentOutput:
        """One criterion: a segment check, a lab problem, or a review card.

        Routed to Haiku (§18) -- one criterion, no weighting, and the whole call
        runs in under three seconds, which is what keeps a lab loop feeling
        responsive.
        """
        ctx = input.session_context
        answer = str(input.require("answer"))
        rubric = self._synthetic_rubric(input)

        prefix = build_prefix(
            "evaluator", ctx, model=self._model(input), rubric=rubric
        )
        suffix = (
            f"Learner answer:\n{wrap_user_content(answer)}\n\n"
            f"Task: Grade this answer against the single criterion above. "
            f"{self._attempt_note(input)}"
        )
        spec = self.call_spec(input, prefix=prefix, suffix=suffix)
        result = await self.client.parse(spec, PartialCheck)
        check: PartialCheck = result.structured  # type: ignore[assignment]

        effects = self._evidence_effects(input, check, result.turn_id)

        return AgentOutput(
            text=check.feedback,
            structured=check,
            trace=result.record,
            turn_id=result.turn_id,
            tool_effects=effects,
        )

    def _attempt_note(self, input: AgentInput) -> str:
        """§12's attempt cycle, surfaced to the grader.

        A second attempt is graded knowing the hint was released, which is the
        difference between "they needed a nudge" and "they did not know it".
        """
        attempt = int(input.payload.get("attempt", 1))
        if attempt <= 1:
            return "This is their first attempt; the hint has not been shown."
        return (
            "This is their second attempt and the hint has already been shown. "
            "Grade what is in front of you; do not give extra credit for the hint."
        )

    def _synthetic_rubric(self, input: AgentInput) -> list[dict[str, Any]]:
        """Wrap a check or practice problem as a one-row rubric.

        ``grade_check`` and ``grade_practice`` grade against a
        ``ComprehensionCheck`` or ``PracticeProblem``, not against
        ``rubric_criteria`` rows -- those exist only for summative assessment.
        Shaping them identically lets one prefix builder serve both, and keeps
        the rubric hash meaningful as a cache key either way.
        """
        payload = input.payload
        criterion_id = payload.get("criterion_id") or uuid.uuid5(
            uuid.NAMESPACE_URL, f"studium/check/{payload.get('question', '')}"
        )
        return [
            {
                "id": str(criterion_id),
                "slug": "check",
                "weight": 2,
                "prompt": payload.get("question", ""),
                "key_points": payload.get("expected_key_points", []),
            }
        ]

    def _evidence_effects(
        self, input: AgentInput, check: PartialCheck, turn_id: uuid.UUID | None
    ) -> list[ToolEffect]:
        ctx = input.session_context
        effects: list[ToolEffect] = []

        kind = _EVIDENCE_KIND.get((input.kind, check.verdict))
        if kind is None:
            return effects

        current = ctx.focus_mastery
        jump = would_jump_mastery(current, check.verdict)

        effects.append(
            ToolEffect(
                kind="record_mastery_evidence",
                payload={
                    "learner_subject_id": str(ctx.learner_subject_id),
                    "concept_id": str(ctx.focus_concept_id),
                    "session_id": str(ctx.session_id),
                    "kind": kind,
                    # practice_partial and assessment_scored are the GRADED_KINDS
                    # that need an explicit correct= value (data layer mastery.py).
                    "correct": check.verdict == "correct",
                    "evidence": {
                        "verdict": check.verdict,
                        "confident": check.confident,
                        "learner_response": str(input.payload.get("answer", ""))[:2000],
                        "expected_key_points": input.payload.get("expected_key_points", []),
                        "missed_points": check.missing_points,
                        "attempt": int(input.payload.get("attempt", 1)),
                    },
                },
            )
        )

        if jump > MASTERY_JUMP_THRESHOLD:
            effects.append(
                ToolEffect(
                    kind="flag_for_review",
                    payload={
                        "source": "evaluator_disagreement",
                        "severity": 3,
                        "session_turn_id": str(turn_id) if turn_id else None,
                        "reason": (
                            f"Single grade moves mastery by {jump:.2f} "
                            f"(from {current:.2f}), above the {MASTERY_JUMP_THRESHOLD} "
                            f"threshold in §12."
                        ),
                    },
                )
            )

        return effects

    # --- whole-rubric grading ---------------------------------------------

    async def _grade_assessment(self, input: AgentInput) -> AgentOutput:
        """A summative attempt: 5-8 criteria in one call, on Opus (§12)."""
        ctx = input.session_context
        rubric = list(input.require("rubric"))
        responses = input.require("responses")

        prefix = build_prefix(
            "evaluator", ctx, model=self._model(input), rubric=rubric
        )

        answers = "\n\n".join(
            f"[{r['criterion_slug']}] (criterion_id {r['criterion_id']})\n"
            f"{wrap_user_content(r['learner_response'])}"
            for r in responses
        )
        suffix = (
            f"Learner answers:\n{answers}\n\n"
            f"Task: Grade every criterion in the rubric. Return one "
            f"criterion_grade per criterion, using the criterion_id shown "
            f"above. Do not omit a criterion; if an answer is missing, grade "
            f"it 0."
        )

        spec = self.call_spec(input, prefix=prefix, suffix=suffix)
        result = await self.client.parse(spec, GradeReport)
        report: GradeReport = result.structured  # type: ignore[assignment]

        effects = self._assessment_effects(input, report, rubric, result.turn_id)

        return AgentOutput(
            text=report.overall_feedback,
            structured=report,
            trace=result.record,
            turn_id=result.turn_id,
            tool_effects=effects,
        )

    def _assessment_effects(
        self,
        input: AgentInput,
        report: GradeReport,
        rubric: list[dict[str, Any]],
        turn_id: uuid.UUID | None,
    ) -> list[ToolEffect]:
        """Flag structural defects in the grade before it is persisted (§12)."""
        effects: list[ToolEffect] = []
        expected = {str(c["id"]) for c in rubric}
        graded = {str(g.criterion_id) for g in report.criterion_grades}

        missing = expected - graded
        unknown = graded - expected

        if missing or unknown:
            effects.append(
                ToolEffect(
                    kind="flag_for_review",
                    payload={
                        "source": "evaluator_disagreement",
                        "severity": 3,
                        "session_turn_id": str(turn_id) if turn_id else None,
                        "reason": (
                            "Grade report does not match the rubric: "
                            f"{len(missing)} criterion/criteria ungraded, "
                            f"{len(unknown)} unrecognised criterion_id(s). "
                            "Ungraded criteria are excluded from the weighted "
                            "mean rather than counted as zero, so this silently "
                            "changes the score."
                        ),
                    },
                )
            )
        return effects

    def _model(self, input: AgentInput) -> str:
        from studium.llm.client import route

        return input.payload.get("model_override") or route(self.identity, input.kind)

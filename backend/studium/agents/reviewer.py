"""The Reviewer: spaced repetition (agent runtime §14).

Selects due cards, generates retrieval prompts that require production rather
than recognition, grades responses, and hands the FSRS update to deterministic
code.

Two things here are *not* model calls, and that is the point:

* **Card selection** is a database query (``studium.review.selection``). §14 is
  explicit: "The Reviewer LLM is not involved in selection."
* **The FSRS update** is ``studium.review.fsrs.review()``. The model produces a
  verdict; the mapping from verdict to rating and from rating to the next
  interval is arithmetic, and arithmetic does not go to a model.

**Attribution note.** §14 says a response is "graded by a ``check_partial`` call
to the Evaluator", while §18 routes ``Reviewer / grade_response`` to Haiku 4.5
as its own row. Implemented as the Reviewer making its own single-criterion
call, which is what the routing table describes -- so the trace is attributed
to ``reviewer``, not ``evaluator``. Grading integrity is unaffected: the prompt
is the Evaluator's rubric-grading prefix and carries no Reviewer framing. If
per-agent grading dashboards are meant to include review grades, this is where
that attribution decision lives. See DIVERGENCES (R9).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from studium.acl import wrap_user_content
from studium.llm.prompts import build_prefix, fmt_float
from studium.retrieval import DEFAULT_K, PassageRetriever
from studium.review.fsrs import CardState, Rating, review

from .base import Agent, AgentInput, AgentOutput, ToolEffect
from .schemas import PartialCheck, RetrievalCheck, RetrievalPrompt

log = logging.getLogger(__name__)

#: §14's verdict -> FSRS rating table.
#:
#: "correct and confident" and "correct with hesitation" are distinguished by
#: PartialCheck.confident, which exists for exactly this mapping.
FSRS_RATING = {
    ("correct", True): Rating.EASY,
    ("correct", False): Rating.GOOD,
    ("partially_correct", True): Rating.HARD,
    ("partially_correct", False): Rating.HARD,
    ("incorrect", True): Rating.AGAIN,
    ("incorrect", False): Rating.AGAIN,
}

#: §14: high-stability cards get harder prompts, low-stability more direct ones.
HIGH_STABILITY_DAYS = 30.0
FRESH_STABILITY_DAYS = 5.0


def rating_for(check: PartialCheck) -> Rating:
    return FSRS_RATING[(check.verdict, check.confident)]


class Reviewer(Agent):
    identity = "reviewer"
    kinds = frozenset({"generate_prompt", "grade_response", "retrieval_check"})

    def __init__(self, client: Any, retriever: PassageRetriever) -> None:
        super().__init__(client)
        self.retriever = retriever

    async def handle(self, input: AgentInput) -> AgentOutput:
        self.check_kind(input)
        handler = {
            "generate_prompt": self._generate_prompt,
            "grade_response": self._grade_response,
            "retrieval_check": self._retrieval_check,
        }[input.kind]
        return await handler(input)

    async def _prefix(self, input: AgentInput) -> Any:
        ctx = input.session_context
        passages = ctx.passages
        if not passages and ctx.focus_concept_id is not None:
            passages = await self.retriever.retrieve_passages(
                ctx.focus_concept_id, k=DEFAULT_K
            )
        return build_prefix(
            "reviewer",
            ctx.model_copy(update={"passages": passages}),
            model=self._model(input),
        )

    # --- prompt generation -------------------------------------------------

    async def _generate_prompt(self, input: AgentInput) -> AgentOutput:
        card = input.require("card")
        stability = float(card.get("stability", 1.0))
        difficulty = float(card.get("difficulty", 5.0))
        days = _days_since(card.get("last_reviewed_at"))

        calibration = (
            "This card is well-mastered; prefer an application or transfer prompt."
            if stability > HIGH_STABILITY_DAYS
            else "This card is freshly learned; prefer direct recall."
            if stability < FRESH_STABILITY_DAYS
            else "This card is mid-stability; a connection or worked-example prompt fits."
        )

        suffix = (
            f"Card state: stability={fmt_float(stability, places=2)}, "
            f"difficulty={fmt_float(difficulty, places=2)}, "
            f"days_since_last_review={days}\n"
            f"Prior prompt on this card (if any): "
            f"{card.get('last_prompt_text') or '(none)'}\n\n"
            f"{calibration}\n\n"
            f"Task: Generate one retrieval prompt for this card. Do not repeat "
            f"the prior prompt's angle."
        )

        spec = self.call_spec(input, prefix=await self._prefix(input), suffix=suffix)
        result = await self.client.parse(spec, RetrievalPrompt)
        prompt: RetrievalPrompt = result.structured  # type: ignore[assignment]

        return AgentOutput(
            text=prompt.prompt,
            structured=prompt,
            trace=result.record,
            turn_id=result.turn_id,
        )

    # --- grading and the FSRS update --------------------------------------

    async def _grade_response(self, input: AgentInput) -> AgentOutput:
        """Grade a review answer and schedule the card's next appearance."""
        ctx = input.session_context
        card = input.require("card")
        prompt = input.require("prompt")
        answer = str(input.require("answer"))

        rubric = [
            {
                "id": str(card.get("id", uuid.uuid4())),
                "slug": "review",
                "weight": 2,
                "prompt": prompt.get("prompt", ""),
                "key_points": prompt.get("expected_key_points", []),
            }
        ]
        prefix = build_prefix(
            "evaluator", ctx, model=self._model(input), rubric=rubric
        )
        suffix = (
            f"Learner answer:\n{wrap_user_content(answer)}\n\n"
            f"Task: Grade per the rubric. Set 'confident' true only when the "
            f"answer is both correct and stated without hedging -- it decides "
            f"whether this card's interval grows or merely holds."
        )

        spec = self.call_spec(input, prefix=prefix, suffix=suffix)
        result = await self.client.parse(spec, PartialCheck, thinking=False)
        check: PartialCheck = result.structured  # type: ignore[assignment]

        rating = rating_for(check)
        before = _card_state(card)
        after = review(before, rating)

        effects = [
            ToolEffect(
                kind="schedule_review",
                payload={
                    "card_id": str(card["id"]),
                    "session_id": str(ctx.session_id),
                    "rating": int(rating),
                    "response_text": answer[:2000],
                    "stability_before": before.stability,
                    "stability_after": after.stability,
                    "difficulty_before": before.difficulty,
                    "difficulty_after": after.difficulty,
                    "retrievability": after.retrievability,
                    "state": after.state,
                    "reps": after.reps,
                    "lapses": after.lapses,
                    "due_at": after.due_at.isoformat() if after.due_at else None,
                    "last_reviewed_at": (
                        after.last_reviewed_at.isoformat() if after.last_reviewed_at else None
                    ),
                },
            ),
            ToolEffect(
                kind="record_mastery_evidence",
                payload={
                    "learner_subject_id": str(ctx.learner_subject_id),
                    "concept_id": str(card["concept_id"]),
                    "session_id": str(ctx.session_id),
                    "kind": "review_correct" if check.verdict == "correct" else "review_incorrect",
                    "correct": check.verdict == "correct",
                    "evidence": {
                        "card_id": str(card["id"]),
                        "verdict": check.verdict,
                        "rating": int(rating),
                        "missed_points": check.missing_points,
                    },
                },
            ),
        ]

        return AgentOutput(
            text=check.feedback,
            structured=check,
            trace=result.record,
            turn_id=result.turn_id,
            tool_effects=effects,
        )

    # --- session-open retrieval check -------------------------------------

    async def _retrieval_check(self, input: AgentInput) -> AgentOutput:
        """§14. Distinct from ordinary review: tests whether yesterday stuck."""
        ctx = input.session_context
        prior = ctx.prior_session_summary or {}

        suffix = (
            f"Prior session summary:\n{prior.get('summary', '(none)')}\n"
            f"Key points from that session: {prior.get('key_points', [])}\n"
            f"Concepts touched: {prior.get('concepts_touched', [])}\n\n"
            f"Task: Generate 2-3 retrieval prompts drawn from the prior session's "
            f"key points, calibrated to test whether that material has stuck. "
            f"These open the session, so keep them short and answerable in a "
            f"sentence or two each."
        )

        spec = self.call_spec(input, prefix=await self._prefix(input), suffix=suffix)
        result = await self.client.parse(spec, RetrievalCheck)
        check: RetrievalCheck = result.structured  # type: ignore[assignment]

        return AgentOutput(
            text="\n".join(p.prompt for p in check.prompts),
            structured=check,
            trace=result.record,
            turn_id=result.turn_id,
        )

    def _model(self, input: AgentInput) -> str:
        from studium.llm.client import route

        return input.payload.get("model_override") or route(self.identity, input.kind)


def _card_state(card: dict[str, Any]) -> CardState:
    return CardState(
        stability=float(card.get("stability", 1.0)),
        difficulty=float(card.get("difficulty", 5.0)),
        retrievability=float(card.get("retrievability", 1.0)),
        reps=int(card.get("reps", 0)),
        lapses=int(card.get("lapses", 0)),
        state=str(card.get("state", "new")),
        last_reviewed_at=_parse_ts(card.get("last_reviewed_at")),
        due_at=_parse_ts(card.get("due_at")),
    )


def _parse_ts(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    parsed = dt.datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _days_since(value: Any) -> int:
    ts = _parse_ts(value)
    if ts is None:
        return 0
    return max(0, int((dt.datetime.now(dt.UTC) - ts).total_seconds() / 86400))

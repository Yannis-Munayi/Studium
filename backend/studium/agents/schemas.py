"""Structured outputs for every agent (agent runtime §3).

"Where a downstream consumer needs to make a decision on part of an agent's
output, that part is emitted via a structured output tool with a validated
schema, not extracted from prose after the fact."

Collected in one module rather than beside each agent so the §23 Tier 1
round-trip test can enumerate them: every model here must serialise to JSON and
back losslessly, which is what guarantees a schema change cannot quietly break
a JSONB column it is stored in.

Constraints are kept to enums and required fields. The API's structured-output
support does not enforce numeric bounds (``minimum``/``maximum``) or string
lengths, so a schema that declared them would validate client-side only and
read as a stronger guarantee than it is. Where a bound matters it is stated in
the prompt and re-checked in application code -- ``score`` below is a
``Literal[0, 1, 2]``, which *is* enforceable, because it is an enum.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

Stance = Literal["formal", "intuitive", "applied", "historical", "default"]
Mode = Literal["lecture", "tutorial", "lab", "review"]

#: §8's intent set. ``primitive:<name>`` is flattened into eight explicit
#: members: a free-form ``primitive:{name}`` string would let the model invent
#: a primitive that has no handler, which then fails at dispatch instead of at
#: validation.
Intent = Literal[
    "question",
    "answer",
    "comment",
    "interrupt",
    "next",
    "back",
    "end_session",
    "primitive:explain_differently",
    "primitive:prove_it_to_me",
    "primitive:where_does_this_fit",
    "primitive:vocabulary_check",
    "primitive:show_worked_example",
    "primitive:let_me_try_one",
    "primitive:why_does_this_matter",
    "primitive:im_lost",
]

PRIMITIVE_NAMES: tuple[str, ...] = (
    "explain_differently",
    "prove_it_to_me",
    "where_does_this_fit",
    "vocabulary_check",
    "show_worked_example",
    "let_me_try_one",
    "why_does_this_matter",
    "im_lost",
)


# --- Orchestrator ----------------------------------------------------------


class IntentClassification(BaseModel):
    """§8's intent classifier output."""

    intent: Intent
    confidence: float
    reasoning: str

    @property
    def primitive(self) -> str | None:
        return self.intent.split(":", 1)[1] if self.intent.startswith("primitive:") else None


# --- Reviewer --------------------------------------------------------------


class RetrievalPrompt(BaseModel):
    """§14. A prompt that requires production, not recognition."""

    prompt: str
    kind: Literal["definition", "worked_example", "connection", "application", "edge_case"]
    model_answer: str
    expected_key_points: list[str] = Field(default_factory=list)


class RetrievalCheck(BaseModel):
    """The 2-3 prompt check at session open, drawn from the prior summary."""

    prompts: list[RetrievalPrompt] = Field(default_factory=list)
    basis: str = ""


# --- Curator ---------------------------------------------------------------


class SessionOpening(BaseModel):
    """§9. How a session opens."""

    focus_concept_id: uuid.UUID
    #: 3-6 items, learner-visible.
    session_agenda: list[str] = Field(default_factory=list)
    prior_session_note: str | None = None
    retrieval_check_prompts: list[RetrievalPrompt] = Field(default_factory=list)
    initial_mode: Mode
    #: Internal, for the trace.
    justification: str = ""


class NextTopic(BaseModel):
    """§9. What to work on next."""

    concept_id: uuid.UUID
    mode: Literal["lecture", "tutorial", "lab"]
    stance: Stance
    #: One sentence, internal.
    reason: str = ""


class StanceChoice(BaseModel):
    """§9 ``select_stance``: which stance, and whether to reuse an artifact."""

    stance: Stance
    reuse_existing_artifact: bool = True
    reason: str = ""


class SessionSummary(BaseModel):
    """§16 closing. Written to ``session_summaries``."""

    summary: str
    key_points: list[str] = Field(default_factory=list)
    open_threads: list[str] = Field(default_factory=list)
    next_session_note: str = ""


class PracticeProblem(BaseModel):
    """The problem ``let_me_try_one`` puts in front of the learner (§15)."""

    prompt: str
    model_answer: str
    expected_key_points: list[str] = Field(default_factory=list)
    difficulty: Literal[1, 2, 3, 4, 5] = 3
    hint: str = ""


# --- Lecturer --------------------------------------------------------------


class ComprehensionCheck(BaseModel):
    """§10. Presented after every second segment."""

    question: str
    #: 1-3 items.
    expected_key_points: list[str] = Field(default_factory=list)
    hint: str = ""
    #: Revealed only after the learner attempts.
    model_answer: str = ""


# --- Tutor -----------------------------------------------------------------


class TutorDiagnostic(BaseModel):
    """The diagnose phase of ``explain_differently`` (§15).

    The Tutor asks what is not landing and picks the stance the Lecturer should
    regenerate in.
    """

    chosen_stance: Stance
    what_is_not_landing: str = ""
    reason: str = ""


class VocabularyVerdict(BaseModel):
    """``vocabulary_check`` (§15): terminology confusion, or substance?"""

    kind: Literal["terminology", "substance"]
    term: str = ""
    paraphrase: str = ""


# --- Evaluator -------------------------------------------------------------

Verdict = Literal["correct", "partially_correct", "incorrect"]


class CriterionGrade(BaseModel):
    """One criterion of a rubric, graded."""

    criterion_id: uuid.UUID
    score: Literal[0, 1, 2]
    feedback: str = ""
    missing_points: list[str] = Field(default_factory=list)


class GradeReport(BaseModel):
    """§12. Matches ``GradeReport`` in the existing draft's ``models.py``."""

    criterion_grades: list[CriterionGrade] = Field(default_factory=list)
    overall_feedback: str = ""


class PartialCheck(BaseModel):
    """§12 ``check_partial``: a fast single-criterion verdict.

    ``confident`` separates a crisp correct answer from a hesitant one, which
    is what §14's FSRS mapping needs to tell ``easy`` from ``good``.
    """

    verdict: Verdict
    confident: bool = False
    feedback: str = ""
    missing_points: list[str] = Field(default_factory=list)


# --- Confusion-Tracker -----------------------------------------------------


class TrackerDecision(BaseModel):
    """§13. Whether a turn warrants a journal entry."""

    action: Literal["none", "create_entry", "revise_entry", "flag_resolved"]
    entry_id: uuid.UUID | None = None
    summary: str | None = None
    hypothesis: str | None = None
    origin: Literal["tracker_inferred"] = "tracker_inferred"
    #: Internal.
    reasoning: str = ""

    def is_valid(self) -> bool:
        """Whether the required fields for this action are present.

        §13 states the requirements per action but the schema cannot express a
        conditional required-field rule, so it is checked here and the
        Confusion-Tracker drops an invalid decision rather than writing a
        journal entry with a null summary.
        """
        if self.action == "none":
            return True
        if self.action == "create_entry":
            return bool(self.summary and self.hypothesis)
        if self.action == "revise_entry":
            return bool(self.entry_id and self.hypothesis)
        if self.action == "flag_resolved":
            return bool(self.entry_id)
        return False


#: Every structured output, for the Tier 1 round-trip sweep.
ALL_SCHEMAS: tuple[type[BaseModel], ...] = (
    IntentClassification,
    RetrievalPrompt,
    RetrievalCheck,
    SessionOpening,
    NextTopic,
    StanceChoice,
    SessionSummary,
    PracticeProblem,
    ComprehensionCheck,
    TutorDiagnostic,
    VocabularyVerdict,
    CriterionGrade,
    GradeReport,
    PartialCheck,
    TrackerDecision,
)

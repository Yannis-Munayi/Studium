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
from typing import ClassVar, Literal

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


class PracticeProblemForClient(BaseModel):
    """What the bench may render (v1.0.1 §6.2).

    This model's *shape* is the guarantee. §6.3 is explicit that the filtering
    belongs in the response schema rather than in the caller's discretion --
    a caller who must remember to strip the answer key is a caller who will one
    day forget, and the failure is silent because the leaked field simply is
    not drawn. There is no field here to forget: the reference answer has no
    home on this type.
    """

    prompt: str
    setup: str | None = None
    expected_minutes: int | None = None
    difficulty: Literal[1, 2, 3, 4, 5] = 3
    #: Only the hints the learner has explicitly asked for. Starts empty and
    #: grows as they click through the ladder (§6.2).
    hint_ladder: list[str] = Field(default_factory=list)


class PracticeProblem(BaseModel):
    """The problem ``let_me_try_one`` selects, whole (v1.0.1 §6.1).

    Server-side only. The Evaluator grades next turn against ``model_answer``
    and ``expected_key_points``, so the Orchestrator holds all of it -- but
    none of it crosses the wire except through :meth:`for_client`.

    Named ``PracticeProblem`` rather than the patch's ``PracticeProblemFull``
    because it is also the Curator's structured-output type, and renaming it
    would rename the schema the model is asked to fill for no gain. The split
    §6.3 asks for is the pair of types, not the spelling of either.
    """

    prompt: str
    model_answer: str
    expected_key_points: list[str] = Field(default_factory=list)
    difficulty: Literal[1, 2, 3, 4, 5] = 3
    hint: str = ""
    setup: str | None = None
    expected_minutes: int | None = None
    #: Progressive hints. ``hint`` is the first rung, kept for the Curator's
    #: existing output shape; anything further the author supplied lands here.
    hint_ladder: list[str] = Field(default_factory=list)

    #: Fields §6.1 forbids on the wire *ever*. Asserted against
    #: ``PracticeProblemForClient``'s own fields by a Tier 1 test, so adding a
    #: server-only field without excluding it fails rather than leaks.
    #:
    #: Hints are deliberately not in this set. §6.1's third item is
    #: "``hint_ladder`` beyond the first hint the learner has requested" --
    #: which is a release schedule, not a secret. A requested hint is
    #: learner-facing; an unrequested one is merely not yet due. That
    #: distinction is :meth:`for_client`'s ``hints_shown``, not this frozenset.
    SERVER_ONLY: ClassVar[frozenset[str]] = frozenset(
        {"model_answer", "expected_key_points"}
    )

    def for_client(self, hints_shown: int = 0) -> PracticeProblemForClient:
        """The learner-facing view (§6.3).

        ``hints_shown`` defaults to zero: a problem arrives with no hints
        released, and each one is a deliberate request. Defaulting the other
        way would hand over the whole ladder with the question.
        """
        ladder = self.full_hint_ladder()[:hints_shown]
        return PracticeProblemForClient(
            prompt=self.prompt,
            setup=self.setup,
            expected_minutes=self.expected_minutes,
            difficulty=self.difficulty,
            hint_ladder=ladder,
        )

    def full_hint_ladder(self) -> list[str]:
        """Every rung, server-side. ``hint`` is the first if it is set."""
        first = [self.hint] if self.hint else []
        return [*first, *self.hint_ladder]


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


class MetaGrade(BaseModel):
    """Evaluation §7.2's meta-grading verdict.

    A new invocation kind on the Evaluator, filed by evaluation §19 as
    subsystem 2 v1.1 work. Unlike every other Evaluator output this grades an
    *agent's* output against a prose rubric rather than a learner's answer
    against ``rubric_criteria`` -- so it is a continuous 0-1 score, not a
    ``Verdict``.

    ``score`` is continuous because §7.2 says so ("returns a score 0-1 with a
    written verdict") and because the properties it grades are continuous by
    nature: "does this adopt a formal stance" has degrees in a way "is this
    answer correct" does not.
    """

    #: No ``ge``/``le``: the structured-output API does not carry numeric
    #: bounds, so declaring them would read as a guarantee the model never
    #: received. ``eval.grading.MetaGrader`` clamps to 0.0-1.0 on the way out,
    #: which is where the constraint can actually be enforced -- and
    #: ``evaluation_results.score`` has the real CHECK behind that.
    score: float
    verdict: str = ""
    #: What in the output drove the score. §13.1 step 6 puts a diff of changed
    #: entries in front of the reviewer, and a bare number gives them nothing
    #: to act on.
    evidence: list[str] = Field(default_factory=list)


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
    MetaGrade,
    TrackerDecision,
)

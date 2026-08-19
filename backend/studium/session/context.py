"""The read-side context handed to every agent (agent runtime §6).

One object carries everything an agent needs from the data layer, so no agent
queries the database itself. That is what makes agent context isolation
structural rather than a convention: an agent cannot read a table it was not
given, and what it was given is visible in one place.

§6 calls the context "deliberately generous" -- cheaper to assemble more than
an agent uses than to have one discover mid-turn that a field is missing.

**Rows are dicts, not typed row models.** §6 sketches ``LearningSessionRow``,
``UserRow`` and friends. Those would be a second declaration of a schema the
ORM already declares, and the two drift the first time a column is added --
silently, because a Pydantic model with a missing field validates fine. Rows
here are plain dicts produced by ``studium.asyncdb.row_to_dict``, so the ORM
stays the single source of truth. The fields the *runtime* owns and the data
layer does not -- ``passages``, ``concepts_seen``, ``warnings`` -- are typed.
See DIVERGENCES (R6).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

Row = dict[str, Any]


class BudgetWarning(BaseModel):
    """A soft cap crossed. Passed to the frontend, not raised (§19)."""

    scope: Literal["daily", "monthly"]
    limit_usd: float
    spent_usd: float

    def message(self) -> str:
        return (
            f"You've used ${self.spent_usd:.2f} of your ${self.limit_usd:.2f} "
            f"{self.scope} budget."
        )


class Passage(BaseModel):
    """One retrieved source passage.

    The contract subsystem 3 fills in (§25): retrieval returns ranked passages
    carrying at minimum a ``chunk_id`` and ``text``. Everything else is what
    citation resolution and the frontend's hover card need, and is optional so
    a thinner retrieval implementation still satisfies the contract.

    Retrieval spec §6 fixes the full shape. The fields below ``page_end`` were
    added by subsystem 3; they carry provenance ("there is no valid retrieval
    result that lacks provenance") and the audit trail for *why* a passage was
    returned. They stay optional because ``StaticRetriever`` and the fixture
    retrievers construct passages without them.
    """

    chunk_id: uuid.UUID
    text: str
    source_id: uuid.UUID | None = None
    source_title: str | None = None
    page_start: int | None = None
    page_end: int | None = None

    #: Retrieval §6. Ordered breadcrumbs into the source's structure.
    section_path: list[str] = Field(default_factory=list)
    source_authors: list[str] = Field(default_factory=list)
    #: A ``chunk_kind`` enum value (retrieval §5).
    chunk_type: str = "body"
    #: Normalised 0.0-1.0, from the reranker. Feeds §13's score threshold.
    relevance_score: float | None = None
    #: 'curated' | 'vector' | 'keyword' | 'expanded' (retrieval §10). What makes
    #: a retrieval decision auditable at read time: a reviewer seeing a claim
    #: grounded in an 'expanded' passage knows to check whether the expansion
    #: was appropriate.
    retrieval_reason: str = "curated"


class SessionContext(BaseModel):
    """Assembled once per turn by ``orchestration/handoff.py``."""

    model_config = {"arbitrary_types_allowed": True}

    session: Row
    learner: Row
    learner_subject: Row
    subject: Row

    focus_concept: Row | None = None
    #: Prerequisites plus immediate dependencies of the focus concept.
    focus_neighborhood: list[Row] = Field(default_factory=list)
    #: The whole subject graph, for the Curator's topological summary.
    subject_concepts: list[Row] = Field(default_factory=list)

    #: Last 12 turns of this session (§6).
    recent_turns: list[Row] = Field(default_factory=list)
    #: concept_id (as str) -> p_known_decayed. Keys are strings because JSONB
    #: round-trips and Pydantic both mangle UUID dict keys.
    mastery_snapshot: dict[str, float] = Field(default_factory=dict)
    open_journal_entries: list[Row] = Field(default_factory=list)

    prior_session_summary: Row | None = None
    retrieval_check_result: Row | None = None

    #: Source passages for the focus concept, from ``retrieve_passages``.
    passages: list[Passage] = Field(default_factory=list)
    #: Concept slugs the learner has met in earlier sessions. The Lecturer is
    #: told not to re-teach these.
    concepts_seen: list[str] = Field(default_factory=list)

    #: Hash of subject + concept + chunk timestamps (§17). Part of the Lecturer
    #: and Tutor cache keys, so stale grounding cannot be served from cache.
    grounding_version: str = ""

    #: Soft-cap warnings raised by the budget gate this turn.
    warnings: list[BudgetWarning] = Field(default_factory=list)

    #: Which learner-facing exchange the current turn belongs to. Increments
    #: once per learner input, not once per LLM call.
    exchange_index: int = 0

    # --- convenience accessors --------------------------------------------

    @property
    def user_id(self) -> uuid.UUID:
        return uuid.UUID(str(self.learner["id"]))

    @property
    def session_id(self) -> uuid.UUID:
        return uuid.UUID(str(self.session["id"]))

    @property
    def learner_subject_id(self) -> uuid.UUID:
        return uuid.UUID(str(self.learner_subject["id"]))

    @property
    def focus_concept_id(self) -> uuid.UUID | None:
        if not self.focus_concept:
            return None
        return uuid.UUID(str(self.focus_concept["id"]))

    @property
    def mode(self) -> str:
        return str(self.session["mode"])

    def mastery_of(self, concept_id: uuid.UUID | str | None) -> float:
        """Decayed mastery for a concept; 0.0 when there is no evidence yet."""
        if concept_id is None:
            return 0.0
        return self.mastery_snapshot.get(str(concept_id), 0.0)

    @property
    def focus_mastery(self) -> float:
        return self.mastery_of(self.focus_concept_id)

    def minutes_remaining(self) -> int:
        """Session budget left, floored at zero.

        Feeds the Curator's pacing decision. Derived from ``started_at`` and
        ``target_duration_minutes`` rather than tracked separately, so a
        reconstructed Orchestrator (§7 "Persistence") gets the same answer as
        one that has been running all along.
        """
        import datetime as dt

        started = self.session.get("started_at")
        target = int(self.session.get("target_duration_minutes") or 90)
        if not started:
            return target
        if isinstance(started, str):
            started = dt.datetime.fromisoformat(started)
        if started.tzinfo is None:
            started = started.replace(tzinfo=dt.UTC)
        elapsed = (dt.datetime.now(dt.UTC) - started).total_seconds() / 60
        return max(0, int(target - elapsed))

    def journal_entries_for_focus(self) -> list[Row]:
        focus = str(self.focus_concept_id) if self.focus_concept_id else None
        if focus is None:
            return []
        return [e for e in self.open_journal_entries if str(e.get("concept_id")) == focus]

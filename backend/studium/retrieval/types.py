"""The ``retrieve_passages`` contract and its tunable constants (retrieval §6).

The interface was fixed by agent runtime §10 and the interior is specified by
retrieval §6. This module holds the shapes both sides agree on, so neither the
agents nor the search internals import the other.

Two things here are contracts rather than defaults, and changing them changes
behaviour elsewhere:

* **Passage ordering.** ``RetrievalResult.passages`` is sorted by ``chunk_id``,
  not by relevance. That is what makes ``[P1]..[Pn]`` byte-stable across two
  identical retrieval calls, which is what lets the Lecturer's cached prefix
  hit (§3 "Citation numbering is a contract", agent runtime §17). Relevance
  survives on ``Passage.relevance_score`` for any caller that wants it.
* **The thin-grounding thresholds.** They are the trigger for a reviewer-facing
  flag, so moving them changes how much under-grounded content reaches a
  learner unremarked.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from pydantic import BaseModel, Field

from studium.session.context import Passage

#: §6: "Default 6, matching the Lecturer's request in subsystem 2 §10."
DEFAULT_K = 6

#: §6: "Maximum enforced at 20 to bound reranking cost."
MAX_K = 20

#: §6: ``k`` is a soft ceiling. A code or math block that spans several
#: ``source_chunks`` rows is returned whole -- half a derivation is evidence
#: for a claim neither half supports -- so the hard ceiling is ``k + 3``.
K_OVERFLOW_ALLOWANCE = 3

#: §13 condition 1. Fewer than this many passages is thin grounding.
THIN_GROUNDING_MIN_COUNT = 3

#: §13 condition 2. A full slate of marginal matches is worse than a small
#: slate of good ones, so the average is checked as well as the count.
THIN_GROUNDING_MIN_AVG_SCORE = 0.5

#: Retained under its agent-runtime name: §10 of that spec calls the count
#: condition "the thin-grounding threshold" and the Lecturer imports it.
THIN_GROUNDING_THRESHOLD = THIN_GROUNDING_MIN_COUNT

#: §11. Stance does not filter -- it re-weights. A ``formal`` retrieval still
#: returns a worked example that scores highest overall, because a good example
#: grounding a formal claim beats a mediocre canonical definition.
STANCE_ROLES: dict[str, tuple[str, ...]] = {
    "formal": ("canonical_definition", "primary_exposition"),
    "intuitive": ("worked_example", "primary_exposition"),
    "applied": ("worked_example", "exercise"),
    "historical": ("historical", "primary_exposition"),
    "default": ("primary_exposition", "canonical_definition"),
}

#: §10: curated pointers outrank everything a similarity function suggests.
CURATED_BOOST = 1.5

#: §11: applied after the curated boost, and only to chunks a curator has
#: classified -- a pure vector hit has no role, so there is no stance signal to
#: read off it.
STANCE_BOOST = 1.3

#: §9. Reciprocal Rank Fusion's damping constant, at its standard value.
RRF_K = 60

#: §9. Per-modality candidate depth, and the reranker's input ceiling.
SEARCH_LIMIT = 20

#: §5/§9: structurally present, useless as evidence. Headings exist to populate
#: ``section_path`` for their siblings; a bibliography entry grounds nothing.
NON_EVIDENCE_CHUNK_TYPES = ("heading", "reference")

#: Valid ``Passage.retrieval_reason`` values (§10 "Cross-concept leakage").
RETRIEVAL_REASONS = ("curated", "vector", "keyword", "expanded")


class RetrievalResult(BaseModel):
    """What one ``retrieve_passages`` call returns (§6).

    ``retrieve_passages`` never raises for lack of results: zero passages is a
    valid, flagged answer. Raising would make every caller wrap the call in a
    try block to handle a condition that is not exceptional -- a concept whose
    curation is thin is a normal state of an evolving corpus, and the correct
    response is to tell the caller so, not to fail the learner's turn.
    """

    #: Numbered ``[P1]..[Pn]`` in ``chunk_id``-sorted order.
    passages: list[Passage] = Field(default_factory=list)
    #: The effective query string, after §9's construction rules. Recorded so a
    #: reviewer working the queue can see what was actually searched.
    query_used: str = ""
    #: §13: fewer than three passages, or an average score below 0.5.
    thin_grounding: bool = False
    #: Populated only when retrieval could write the review row itself -- which
    #: needs a ``session_turn_id`` to attach it to. See DIVERGENCES-RETRIEVAL
    #: (S2): at Lecturer-grounding time no turn exists yet, so the flag travels
    #: back to the caller and its effect batch writes the row.
    review_queue_id: uuid.UUID | None = None
    #: Human-readable trigger, e.g. "returned 2 passages, avg score 0.42".
    #: Becomes the ``content_review_queue.reason``.
    thin_grounding_reason: str = ""
    #: True when a degradation path was taken (§16): reranking skipped, or one
    #: search modality unavailable. Noted to the trace by the caller.
    degraded: bool = False

    def numbered(self) -> list[tuple[int, Passage]]:
        """``[(1, p), (2, p), ...]`` in the citation numbering (§12)."""
        return list(enumerate(self.passages, start=1))

    @property
    def average_score(self) -> float:
        scored = [p.relevance_score for p in self.passages if p.relevance_score is not None]
        return sum(scored) / len(scored) if scored else 0.0


class PassageRetriever(Protocol):
    """What the Lecturer, Tutor, and Reviewer depend on (agent runtime §25)."""

    async def retrieve_passages(
        self,
        concept_id: uuid.UUID,
        *,
        stance: str = "default",
        k: int = DEFAULT_K,
        query_text: str | None = None,
        session_turn_id: uuid.UUID | None = None,
    ) -> RetrievalResult:
        """Return up to ``k`` passages grounding ``concept_id``."""
        ...


def clamp_k(k: int) -> int:
    """§6: ``k`` is bounded at 20 so one call cannot rerank an arbitrary pool."""
    return max(1, min(int(k), MAX_K))


def stance_roles(stance: str) -> tuple[str, ...]:
    """Preferred ``concept_sources.role`` values for a stance (§11).

    An unknown stance falls back to ``default`` rather than raising: stance
    reaches here from a Curator's structured output, and a model returning an
    unmapped value should cost a lecture its stance preference, not its
    grounding.
    """
    return STANCE_ROLES.get(stance, STANCE_ROLES["default"])


def sort_for_citation(passages: list[Passage]) -> list[Passage]:
    """The §3 numbering contract, in one place.

    Every path that produces a ``RetrievalResult`` ends here. Sorting in one
    function rather than at each call site is what stops a future modality from
    returning relevance order and silently breaking the cached prefix -- the
    kind of failure that shows up as a cache hit rate, not as a test failure.
    """
    return sorted(passages, key=lambda p: str(p.chunk_id))


def detect_thin_grounding(passages: list[Passage]) -> tuple[bool, str]:
    """§13's two conditions. Returns ``(is_thin, reason)``.

    The reason is assembled here rather than by the caller so every review-queue
    row phrases the same finding the same way -- a reviewer skimming the queue
    is looking for patterns, and three spellings of "not enough passages" hides
    one.
    """
    count = len(passages)
    if count < THIN_GROUNDING_MIN_COUNT:
        return True, f"Retrieval returned {count} passage(s); §13 expects at least {THIN_GROUNDING_MIN_COUNT}."

    scored = [p.relevance_score for p in passages if p.relevance_score is not None]
    if scored:
        average = sum(scored) / len(scored)
        if average < THIN_GROUNDING_MIN_AVG_SCORE:
            return True, (
                f"Retrieval returned {count} passages, avg score {average:.2f}; "
                f"§13 expects at least {THIN_GROUNDING_MIN_AVG_SCORE}."
            )

    return False, ""


def grounding_is_thin(passages: list[Passage]) -> bool:
    """Agent runtime §10's count-only test, kept for callers holding a list."""
    return len(passages) < THIN_GROUNDING_MIN_COUNT


__all__ = [
    "CURATED_BOOST",
    "DEFAULT_K",
    "K_OVERFLOW_ALLOWANCE",
    "MAX_K",
    "NON_EVIDENCE_CHUNK_TYPES",
    "RETRIEVAL_REASONS",
    "RRF_K",
    "SEARCH_LIMIT",
    "STANCE_BOOST",
    "STANCE_ROLES",
    "THIN_GROUNDING_MIN_AVG_SCORE",
    "THIN_GROUNDING_MIN_COUNT",
    "THIN_GROUNDING_THRESHOLD",
    "Passage",
    "PassageRetriever",
    "RetrievalResult",
    "clamp_k",
    "detect_thin_grounding",
    "grounding_is_thin",
    "sort_for_citation",
    "stance_roles",
]

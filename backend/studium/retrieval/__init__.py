"""The ``retrieve_passages`` boundary (agent runtime §25).

This spec treats retrieval as a black box. It commits only that the tool
exists, returns ranked passages carrying ``chunk_id`` and ``text``, and is
called by the Lecturer, the Tutor, and the Reviewer. Chunking, embedding,
hybrid ranking, reranking, and citation resolution are subsystem 3's interior.

What lives here is the *contract* -- :class:`PassageRetriever` -- plus a
deliberately unsophisticated default so the runtime is runnable and testable
before subsystem 3 exists.

**The default is curated-pointer retrieval, not search.** It reads the
``chunk_ids`` array that ``concept_sources`` already curates per concept and
returns those chunks in document order. No embeddings, no ranking, no stance
sensitivity -- the ``stance`` argument is accepted and ignored. That is a real
limitation and it is stated rather than hidden: a Lecturer running on this
retriever is grounded in whatever an author pointed at, which is correct but
not selective. Segments built on it will more often trip §10's "fewer than
three passages" thin-grounding flag, and that flag firing is the signal, not a
defect. Swapping in subsystem 3 means passing a different retriever to the
agents; nothing else changes.
"""

from __future__ import annotations

import logging
import uuid
from typing import Protocol

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.asyncdb import read_db
from studium.session.context import Passage

log = logging.getLogger(__name__)

#: §10: the Lecturer asks for six passages per segment.
DEFAULT_K = 6

#: §10: "If retrieval returns fewer than three passages, the Lecturer notes to
#: the trace that grounding is thin and the segment is flagged for review."
THIN_GROUNDING_THRESHOLD = 3


class PassageRetriever(Protocol):
    """What the Lecturer, Tutor, and Reviewer depend on."""

    async def retrieve_passages(
        self,
        concept_id: uuid.UUID,
        *,
        stance: str = "default",
        k: int = DEFAULT_K,
    ) -> list[Passage]:
        """Return up to ``k`` passages grounding ``concept_id``."""
        ...


class CuratedPointerRetriever:
    """Reads the chunk pointers ``concept_sources`` already curates.

    Ordered by ``(source_id, chunk_index)`` -- document order, which for a
    curated pointer set is the only ordering that carries meaning. Prefix
    assembly re-sorts by ``chunk_id`` for byte stability regardless (§17), so
    the ordering here only decides *which* passages survive the ``k`` cut, not
    how they are numbered.
    """

    #: Roles worth grounding exposition in, most authoritative first. An
    #: 'exercise' chunk is a poor thing to lecture from, so it is excluded.
    ROLE_PRIORITY = (
        "canonical_definition",
        "primary_exposition",
        "worked_example",
        "alternative_stance",
        "historical",
    )

    async def retrieve_passages(
        self,
        concept_id: uuid.UUID,
        *,
        stance: str = "default",
        k: int = DEFAULT_K,
    ) -> list[Passage]:
        rows = await read_db(lambda s: self._query(s, concept_id, k))
        return [
            Passage(
                chunk_id=r.chunk_id,
                text=r.text,
                source_id=r.source_id,
                source_title=r.source_title,
                page_start=r.page_start,
                page_end=r.page_end,
            )
            for r in rows
        ]

    def _query(self, session: Session, concept_id: uuid.UUID, k: int) -> list:
        # unnest() flattens concept_sources.chunk_ids, which is an array rather
        # than a join table (data layer §6.2: "a curated pointer set for
        # retrieval to prefer"). The inner join drops dangling pointers, which
        # the daily consistency check reports separately -- a broken reference
        # degrades the lecture's grounding rather than failing the turn.
        return list(
            session.execute(
                sql(
                    """
                    SELECT sc.id           AS chunk_id,
                           sc.text         AS text,
                           sc.source_id    AS source_id,
                           sc.page_start   AS page_start,
                           sc.page_end     AS page_end,
                           s.title         AS source_title
                      FROM concept_sources cs
                      CROSS JOIN LATERAL unnest(cs.chunk_ids) AS ref(chunk_id)
                      JOIN source_chunks sc ON sc.id = ref.chunk_id
                      JOIN sources s        ON s.id = sc.source_id
                     WHERE cs.concept_id = :concept_id
                       AND cs.role = ANY(CAST(:roles AS concept_source_role[]))
                       AND s.deleted_at IS NULL
                     ORDER BY array_position(
                                  CAST(:roles AS concept_source_role[]), cs.role
                              ),
                              sc.source_id,
                              sc.chunk_index
                     LIMIT :k
                    """
                ),
                {
                    "concept_id": concept_id,
                    "roles": list(self.ROLE_PRIORITY),
                    "k": k,
                },
            ).all()
        )


class StaticRetriever:
    """Fixture retriever for offline tests. Returns what it was handed."""

    def __init__(self, passages: dict[uuid.UUID, list[Passage]] | None = None) -> None:
        self._passages = passages or {}

    async def retrieve_passages(
        self,
        concept_id: uuid.UUID,
        *,
        stance: str = "default",
        k: int = DEFAULT_K,
    ) -> list[Passage]:
        return list(self._passages.get(concept_id, []))[:k]


def grounding_is_thin(passages: list[Passage]) -> bool:
    """§10's thin-grounding test."""
    return len(passages) < THIN_GROUNDING_THRESHOLD


__all__ = [
    "DEFAULT_K",
    "THIN_GROUNDING_THRESHOLD",
    "CuratedPointerRetriever",
    "Passage",
    "PassageRetriever",
    "StaticRetriever",
    "grounding_is_thin",
]

"""Retrieval and the knowledge substrate (subsystem 3).

Source material becomes citable evidence here. Every claim in a generated
lecture, every source-grounded turn in a tutorial, every review prompt passes
through ``retrieve_passages``.

The failure this subsystem exists to prevent is not "no results" -- it is
*silent* under-grounding: an agent generating confident prose that cites
passages the learner can never find. That is why thin grounding (§13) is a
flagged, reviewer-visible event rather than a log line, and why every returned
passage carries the provenance needed to check it.

    from studium.retrieval import HybridRetriever

    retriever = HybridRetriever()
    result = await retriever.retrieve_passages(concept_id, stance="formal")
    if result.thin_grounding:
        ...  # say less; the caller decides, §13 only detects

Module map:

* :mod:`~studium.retrieval.types` -- the contract, thresholds, and the
  ``chunk_id`` numbering rule everything else obeys.
* :mod:`~studium.retrieval.chunking` -- §7. Pure, deterministic, no database.
* :mod:`~studium.retrieval.providers` -- §4/§8/§16. Embeddings, reranking,
  backoff, circuit breakers.
* :mod:`~studium.retrieval.search` -- §9/§10. SQL, RRF fusion, the graph
  constraint.
* :mod:`~studium.retrieval.service` -- §6. The pipeline, end to end.
* :mod:`~studium.retrieval.citations` -- §12. Markers in, provenance out.
* :mod:`~studium.retrieval.cache` -- §14. Five-minute results, warm starts.
* :mod:`~studium.retrieval.embedding_worker` -- §8. The background embedder.

:class:`CuratedPointerRetriever` predates this subsystem: it is what the agent
runtime shipped against, reading only the ``concept_sources`` pointers an
author curated. It is kept because it is the honest zero-dependency retriever
-- no API key, no embeddings, correct but not selective -- and the Tier 2 tests
use it to prove that swapping retrievers is the only change an agent sees.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.asyncdb import read_db
from studium.session.context import Passage

from .cache import CacheKey, ResultCache
from .chunking import Block, Chunk, chunk_blocks, chunk_text, classify_block
from .citations import (
    CITATION_PATTERN,
    ResolvedCitation,
    has_invalid_citation,
    parse_citation_markers,
    resolve_artifact_citations,
)
from .embedding_worker import EmbeddingWorker
from .providers import (
    EmbeddingProvider,
    EmbeddingUnavailable,
    IdentityReranker,
    Reranker,
    RerankUnavailable,
    StubEmbeddings,
)
from .service import HybridRetriever, default_retriever, flag_thin_grounding
from .types import (
    CURATED_BOOST,
    DEFAULT_K,
    MAX_K,
    STANCE_BOOST,
    STANCE_ROLES,
    THIN_GROUNDING_MIN_AVG_SCORE,
    THIN_GROUNDING_MIN_COUNT,
    THIN_GROUNDING_THRESHOLD,
    PassageRetriever,
    RetrievalResult,
    detect_thin_grounding,
    grounding_is_thin,
    sort_for_citation,
    stance_roles,
)

log = logging.getLogger(__name__)


class CuratedPointerRetriever:
    """Reads only the chunk pointers ``concept_sources`` already curates.

    The retriever the agent runtime was built against, kept to the §6 contract.
    No embeddings, no ranking, no search -- ``query_text`` is accepted and
    ignored, and the stance only reorders by curated role. Segments built on it
    trip §13's thin-grounding flag more often than a real retriever would, and
    that flag firing is the signal rather than a defect.

    Useful in three places: a deployment with no Voyage key, the Tier 2 tests
    that check retriever substitutability, and any comparison that wants
    "what an expert pointed at" as the baseline §19's open question 2 measures
    expansion against.
    """

    #: Roles worth grounding exposition in, most authoritative first. An
    #: 'exercise' chunk is a poor thing to lecture from, so it is last.
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
        query_text: str | None = None,
        session_turn_id: uuid.UUID | None = None,
    ) -> RetrievalResult:
        rows = await read_db(lambda s: self._query(s, concept_id, stance, k))
        passages = sort_for_citation(
            [
                Passage(
                    chunk_id=r.chunk_id,
                    text=r.text,
                    source_id=r.source_id,
                    source_title=r.source_title,
                    source_authors=list(r.source_authors or []),
                    page_start=r.page_start,
                    page_end=r.page_end,
                    section_path=_str_list(r.section_path),
                    chunk_type=str(r.chunk_type),
                    retrieval_reason="curated",
                )
                for r in rows
            ]
        )
        thin, reason = detect_thin_grounding(passages)
        return RetrievalResult(
            passages=passages,
            query_used=f"(curated pointers for concept {concept_id})",
            thin_grounding=thin,
            thin_grounding_reason=reason,
        )

    def _roles(self, stance: str) -> list[str]:
        """Stance-preferred roles first, then the rest of the priority order.

        The stance still cannot change *which* chunks exist for a concept --
        that is what makes this retriever unselective -- but it can decide
        which survive the ``k`` cut, which is the part of §11 implementable
        without a score to boost.
        """
        preferred = stance_roles(stance)
        return [*preferred, *[r for r in self.ROLE_PRIORITY if r not in preferred]]

    def _query(
        self, session: Session, concept_id: uuid.UUID, stance: str, k: int
    ) -> list:
        return list(
            session.execute(
                sql(
                    """
                    SELECT sc.id           AS chunk_id,
                           sc.text         AS text,
                           sc.source_id    AS source_id,
                           sc.page_start   AS page_start,
                           sc.page_end     AS page_end,
                           sc.section_path AS section_path,
                           sc.chunk_type   AS chunk_type,
                           s.title         AS source_title,
                           s.authors       AS source_authors
                      FROM concept_sources cs
                      CROSS JOIN LATERAL unnest(cs.chunk_ids) AS ref(chunk_id)
                      JOIN source_chunks sc ON sc.id = ref.chunk_id
                      JOIN sources s        ON s.id = sc.source_id
                     WHERE cs.concept_id = :concept_id
                       AND cs.role = ANY(CAST(:roles AS concept_source_role[]))
                       AND s.deleted_at IS NULL
                       AND sc.chunk_type <> ALL(ARRAY['heading', 'reference']::chunk_kind[])
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
                    "roles": self._roles(stance),
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
        query_text: str | None = None,
        session_turn_id: uuid.UUID | None = None,
    ) -> RetrievalResult:
        passages = sort_for_citation(list(self._passages.get(concept_id, []))[:k])
        thin, reason = detect_thin_grounding(passages)
        return RetrievalResult(
            passages=passages,
            query_used=query_text or f"(static fixture for {concept_id})",
            thin_grounding=thin,
            thin_grounding_reason=reason,
        )


def _str_list(raw: object) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [s if isinstance(s, str) else str(s) for s in raw]
    return [str(raw)]


__all__ = [
    "CITATION_PATTERN",
    "CURATED_BOOST",
    "DEFAULT_K",
    "MAX_K",
    "STANCE_BOOST",
    "STANCE_ROLES",
    "THIN_GROUNDING_MIN_AVG_SCORE",
    "THIN_GROUNDING_MIN_COUNT",
    "THIN_GROUNDING_THRESHOLD",
    "Block",
    "CacheKey",
    "Chunk",
    "CuratedPointerRetriever",
    "EmbeddingProvider",
    "EmbeddingUnavailable",
    "EmbeddingWorker",
    "HybridRetriever",
    "IdentityReranker",
    "Passage",
    "PassageRetriever",
    "RerankUnavailable",
    "Reranker",
    "ResolvedCitation",
    "ResultCache",
    "RetrievalResult",
    "StaticRetriever",
    "StubEmbeddings",
    "chunk_blocks",
    "chunk_text",
    "classify_block",
    "default_retriever",
    "detect_thin_grounding",
    "flag_thin_grounding",
    "grounding_is_thin",
    "has_invalid_citation",
    "parse_citation_markers",
    "resolve_artifact_citations",
    "sort_for_citation",
    "stance_roles",
]

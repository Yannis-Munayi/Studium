"""``retrieve_passages``, end to end (retrieval §6).

The pipeline for one call:

1. Cache lookup on ``(concept_id, stance, query_hash)`` (§14). A hit returns
   immediately, skipping everything below.
2. Build the effective query from concept metadata plus any caller text (§9).
3. Compute the concept neighbourhood (§10), expanding a hop if curation is thin.
4. Phase 1: collect curated pointers. Phase 2: hybrid vector + keyword search,
   restricted to sources the neighbourhood links to (§9, §10).
5. Fuse by RRF, apply the curated and stance boosts (§9, §11).
6. Rerank the fused candidates (§4), degrading to fusion order if the reranker
   is unavailable (§16).
7. Pull in siblings of any split code or math block (§6's ``k + 3`` allowance).
8. Sort by ``chunk_id`` for the citation-numbering contract (§3), detect thin
   grounding (§13), and return.

Every step that can fail has a defined degradation (§16) and none of them
raise: ``retrieve_passages`` returns an empty, flagged result rather than an
exception, because a concept with no usable grounding is a normal state of an
evolving corpus and the caller's job is to say less, not to fail the turn.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.asyncdb import read_db, run_db
from studium.session.context import Passage

from . import search
from .cache import CacheKey, ResultCache
from .providers import (
    EmbeddingProvider,
    EmbeddingUnavailable,
    IdentityReranker,
    Reranker,
    RerankUnavailable,
    StubEmbeddings,
)
from .search import Candidate, SearchUnavailable
from .types import (
    DEFAULT_K,
    K_OVERFLOW_ALLOWANCE,
    SEARCH_LIMIT,
    THIN_GROUNDING_MIN_COUNT,
    RetrievalResult,
    clamp_k,
    detect_thin_grounding,
    sort_for_citation,
    stance_roles,
)

log = logging.getLogger(__name__)


@dataclass
class HybridRetriever:
    """The retrieval subsystem's ``PassageRetriever`` (§6).

    Providers default to the deterministic stubs so the object is constructible
    -- and the whole pipeline testable -- without an API key. Production wiring
    passes ``VoyageEmbeddings()`` and ``VoyageReranker()``; nothing else about
    the class changes, which is what §4's protocols are for.
    """

    embeddings: EmbeddingProvider = field(default_factory=StubEmbeddings)
    reranker: Reranker = field(default_factory=IdentityReranker)
    cache: ResultCache = field(default_factory=ResultCache)
    #: Set False to bypass the cache entirely -- what the Tier 2 tests do, so a
    #: result cached by one assertion cannot satisfy the next.
    use_cache: bool = True

    async def retrieve_passages(
        self,
        concept_id: uuid.UUID,
        *,
        stance: str = "default",
        k: int = DEFAULT_K,
        query_text: str | None = None,
        session_turn_id: uuid.UUID | None = None,
    ) -> RetrievalResult:
        """§6's contract. Never raises for lack of results."""
        k = clamp_k(k)
        key = CacheKey.build(concept_id, stance, query_text)

        if self.use_cache:
            cached = self.cache.get(key)
            if cached is not None:
                log.debug("retrieval cache hit for %s/%s", concept_id, stance)
                return cached

        try:
            plan = await read_db(
                lambda s: _gather(s, concept_id, stance, query_text)
            )
        except Exception:
            log.exception("retrieval planning failed for concept %s", concept_id)
            return _empty(
                query_used="",
                reason="Retrieval could not read the concept graph.",
                degraded=True,
            )

        if plan is None:
            return _empty(
                query_used="",
                reason=f"Concept {concept_id} has no subject; nothing to search.",
            )

        candidates, degraded = await self._candidates(plan, k)
        ranked, rerank_degraded = await self._rerank(plan.query, candidates, k)
        degraded = degraded or rerank_degraded

        if ranked:
            ranked = await self._with_siblings(ranked, k)

        passages = sort_for_citation([_to_passage(c) for c in ranked])
        thin, reason = detect_thin_grounding(passages)

        result = RetrievalResult(
            passages=passages,
            query_used=plan.query,
            thin_grounding=thin,
            thin_grounding_reason=_reason_with_context(reason, plan, concept_id),
            degraded=degraded,
        )

        if thin and session_turn_id is not None:
            result.review_queue_id = await flag_thin_grounding(
                session_turn_id=session_turn_id,
                reason=result.thin_grounding_reason,
            )

        if self.use_cache and not degraded:
            # A degraded result is not cached: caching one would hold a
            # keyword-only or unreranked answer for five minutes after the
            # provider recovered, turning a 30-second outage into a five-minute
            # quality drop for every learner on that concept.
            self.cache.put(key, result)

        return result

    # --- candidate assembly (§9, §10) --------------------------------------

    async def _candidates(self, plan: _Plan, k: int) -> tuple[list[Candidate], bool]:
        """Phases 1 and 2, fused. Returns candidates and whether it degraded."""
        degraded = False
        embedding: list[float] | None = None

        if plan.source_ids or plan.curated:
            try:
                vectors = await self.embeddings.embed(
                    [plan.query], input_type="query"
                )
                embedding = vectors[0] if vectors else None
            except EmbeddingUnavailable as exc:
                # §16: fall back to keyword-only. The keyword half still finds
                # chunks with no embedding at all, which is also what makes an
                # in-progress embedding backlog invisible rather than fatal.
                log.warning("query embedding unavailable, keyword-only: %s", exc)
                degraded = True

        try:
            pools = await read_db(
                lambda s: _search_pools(s, plan, embedding)
            )
        except Exception:
            log.exception("hybrid search failed entirely for %s", plan.concept_id)
            return list(plan.curated), True

        vector_pool, keyword_pool, search_degraded = pools
        degraded = degraded or search_degraded

        fused = search.fuse(
            [plan.curated, vector_pool, keyword_pool],
            stance_preferred_roles=stance_roles(plan.stance),
        )
        return fused[:SEARCH_LIMIT], degraded

    # --- reranking (§4, §16) ------------------------------------------------

    async def _rerank(
        self, query: str, candidates: list[Candidate], k: int
    ) -> tuple[list[Candidate], bool]:
        """Reorder the fused pool, or keep fusion order if the provider is down."""
        if not candidates:
            return [], False

        try:
            scores = await self.reranker.rerank(
                query, [c.text for c in candidates], top_k=k
            )
        except RerankUnavailable as exc:
            # §16: return the top-k of the fused result and note the skip. The
            # fused order is a real ranking, so this costs ordering quality
            # rather than grounding.
            log.warning("reranking skipped: %s", exc)
            top = candidates[:k]
            for index, candidate in enumerate(top):
                # Fusion scores are not on a 0-1 scale and §13's threshold
                # reads one, so a positional proxy is assigned instead of
                # leaking a raw RRF value into relevance_score -- an RRF score
                # of 0.03 would trip the score threshold on every degraded
                # call and flood the review queue during an outage.
                candidate.fused_score = max(0.0, 1.0 - index * 0.05)
            return top, True

        ordered: list[Candidate] = []
        for index, score in scores:
            if 0 <= index < len(candidates):
                candidate = candidates[index]
                candidate.fused_score = score
                ordered.append(candidate)
        return ordered[:k], False

    # --- §6's k + 3 allowance ----------------------------------------------

    async def _with_siblings(self, ranked: list[Candidate], k: int) -> list[Candidate]:
        allowance = max(0, k + K_OVERFLOW_ALLOWANCE - len(ranked))
        if allowance <= 0:
            return ranked
        try:
            siblings = await read_db(
                lambda s: search.expand_atomic_siblings(s, ranked, allowance=allowance)
            )
        except Exception:  # noqa: BLE001 -- an optional enrichment
            log.warning("sibling expansion failed; returning ranked passages as-is")
            return ranked
        for sibling in siblings:
            # Inherit the weakest score of the block it completes: a sibling is
            # returned for completeness, not because it scored, and giving it
            # a score it did not earn would inflate §13's average.
            sibling.fused_score = min((c.fused_score for c in ranked), default=0.0)
        return [*ranked, *siblings]


# --- planning ---------------------------------------------------------------


@dataclass(slots=True)
class _Plan:
    """Everything one retrieval needs from the database before searching."""

    concept_id: uuid.UUID
    subject_id: uuid.UUID
    stance: str
    query: str
    neighborhood: search.Neighborhood
    curated: list[Candidate]
    source_ids: list[uuid.UUID]
    #: True when §10's one-hop expansion had to run.
    expanded: bool = False


def _gather(
    session: Session,
    concept_id: uuid.UUID,
    stance: str,
    query_text: str | None,
) -> _Plan | None:
    """Neighbourhood, curated pointers, and the effective query (§9, §10)."""
    concept = session.execute(
        sql(
            """
            SELECT id, subject_id, title, short_description
              FROM concepts
             WHERE id = :cid
            """
        ),
        {"cid": concept_id},
    ).one_or_none()

    if concept is None:
        return None

    query = _effective_query(concept.title, concept.short_description, query_text)
    neighborhood = search.concept_neighborhood(session, concept_id, hops=1)
    curated = search.curated_candidates(session, neighborhood)
    expanded = False

    # §10: "If Phase 1 returns fewer than three chunks, the algorithm expands
    # the neighborhood one hop further and re-runs Phase 1." A concept with
    # sparse curation reaches for its neighbours' pointers before it falls back
    # to unconstrained similarity.
    if len(curated) < THIN_GROUNDING_MIN_COUNT:
        wider = search.concept_neighborhood(session, concept_id, hops=2)
        if len(wider) > len(neighborhood):
            widened = search.curated_candidates(session, wider)
            if len(widened) > len(curated):
                expanded = True
                # Pointers reached only through the second hop are 'expanded',
                # not 'curated': nobody chose them *for this concept*, and §10's
                # audit trail exists so a reviewer can tell the difference.
                known = {c.chunk_id for c in curated}
                for candidate in widened:
                    if candidate.chunk_id not in known:
                        candidate.reason = "expanded"
                curated = widened
            neighborhood = wider

    return _Plan(
        concept_id=concept_id,
        subject_id=concept.subject_id,
        stance=stance,
        query=query,
        neighborhood=neighborhood,
        curated=curated,
        source_ids=search.source_ids_for_neighborhood(session, neighborhood),
        expanded=expanded,
    )


def _effective_query(
    title: str, short_description: str | None, query_text: str | None
) -> str:
    """§9's query construction.

    Both modalities use the same string. That matters for fusion: RRF combines
    ranks, and ranks from two different questions are not comparable, so a
    keyword search on the raw learner text fused with a vector search on the
    concept-anchored text would produce a meaningless ordering.

    The concept title always leads. It is what anchors a vague learner question
    ("why does that work?") in the right region of the corpus.
    """
    if query_text and query_text.strip():
        return f"{title}: {query_text.strip()}"
    description = (short_description or "").strip()
    return f"{title}. {description}".strip()


def _search_pools(
    session: Session, plan: _Plan, embedding: list[float] | None
) -> tuple[list[Candidate], list[Candidate], bool]:
    """Run both modalities, each degrading independently (§16)."""
    degraded = embedding is None
    vector_pool: list[Candidate] = []
    keyword_pool: list[Candidate] = []

    if embedding is not None:
        try:
            vector_pool = search.vector_search(
                session,
                query_embedding=embedding,
                subject_id=plan.subject_id,
                source_ids=plan.source_ids,
            )
        except SearchUnavailable:
            degraded = True

    try:
        keyword_pool = search.keyword_search(
            session,
            query_text=plan.query,
            subject_id=plan.subject_id,
            source_ids=plan.source_ids,
        )
    except SearchUnavailable:
        degraded = True

    return vector_pool, keyword_pool, degraded


# --- §13 review-queue write -------------------------------------------------


async def flag_thin_grounding(
    *,
    session_turn_id: uuid.UUID,
    reason: str,
    severity: int = 2,
) -> uuid.UUID | None:
    """Write §13's ``content_review_queue`` row and return its id.

    Only callable with a turn to attach the row to. ``content_review_queue``
    CHECKs that at least one of ``artifact_id`` or ``session_turn_id`` is set,
    and at the moment the Lecturer grounds a segment neither exists yet -- the
    LLM call that creates the turn has not happened. §13 acknowledges this
    ("if the retrieval was invoked from an agent turn") without resolving it.

    So the flag travels back on the result and the caller's effect batch writes
    it, which also keeps the write inside the turn's single transaction (agent
    runtime §8). This function covers the other case: a caller that already has
    a turn -- the Tutor mid-exchange, or a pre-warming pass being audited --
    and wants the row written here. See DIVERGENCES-RETRIEVAL (S2).
    """
    from studium.models import ContentReviewQueueItem

    def write(session: Session) -> uuid.UUID:
        item = ContentReviewQueueItem(
            session_turn_id=session_turn_id,
            source="system_confidence",
            reason=reason,
            severity=severity,
        )
        session.add(item)
        session.flush()
        return item.id

    try:
        return await run_db(write)
    except Exception:  # noqa: BLE001 -- flagging must not fail the retrieval
        log.exception("could not write thin-grounding flag for turn %s", session_turn_id)
        return None


# --- helpers ----------------------------------------------------------------


def _to_passage(candidate: Candidate) -> Passage:
    return Passage(
        chunk_id=candidate.chunk_id,
        text=candidate.text,
        source_id=candidate.source_id,
        source_title=candidate.source_title,
        source_authors=candidate.source_authors,
        page_start=candidate.page_start,
        page_end=candidate.page_end,
        section_path=candidate.section_path,
        chunk_type=candidate.chunk_type,
        relevance_score=candidate.fused_score,
        retrieval_reason=candidate.reason,
    )


def _empty(*, query_used: str, reason: str, degraded: bool = False) -> RetrievalResult:
    return RetrievalResult(
        passages=[],
        query_used=query_used,
        thin_grounding=True,
        thin_grounding_reason=reason,
        degraded=degraded,
    )


def _reason_with_context(reason: str, plan: _Plan, concept_id: uuid.UUID) -> str:
    """Give the reviewer the query and concept alongside the numbers (§13).

    §13 says a reviewer working the queue "sees the query that was used, the
    concept, the number of passages returned, the average score". Assembling
    that here rather than at the queue's read side means the row is
    self-contained -- readable a month later without re-deriving what the
    retrieval was for.
    """
    if not reason:
        return ""
    suffix = f' Concept {concept_id}, query "{plan.query}".'
    if plan.expanded:
        suffix += " Curated pointers were thin enough to need a two-hop expansion."
    return reason + suffix


def default_retriever() -> HybridRetriever:
    """The production wiring: real Voyage providers when the SDK is present.

    Falls back to the deterministic stubs when it is not, so a developer
    without a Voyage key gets a working -- if semantically blunt -- retrieval
    rather than an import error. The fallback is logged at warning level
    because a *deployment* running on stub embeddings would be quietly serving
    nonsense rankings.
    """
    try:
        import voyageai  # noqa: F401
    except ImportError:
        log.warning(
            "voyageai is not installed; retrieval is running on stub embeddings. "
            "Rankings will be arbitrary. Install the 'retrieval' extra."
        )
        return HybridRetriever()

    from .providers import VoyageEmbeddings, VoyageReranker

    return HybridRetriever(embeddings=VoyageEmbeddings(), reranker=VoyageReranker())

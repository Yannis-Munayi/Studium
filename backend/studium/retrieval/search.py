"""Hybrid search, fusion, and the concept-graph constraint (retrieval §9, §10).

Vector and keyword search catch different failures. Vector finds a passage that
discusses the concept in vocabulary the query never used; keyword finds a
passage carrying a specific term or piece of notation that a similarity
function ranks low precisely because it is unusual. Neither is sufficient, and
averaging their scores requires inventing a number for the modality that missed
-- which is why §9 fuses by rank instead.

The concept graph narrows what either modality is allowed to see. Without it, a
query about "reduction" in a lambda calculus subject can return passages from a
Turing machines subject sitting in the same corpus: semantically close,
pedagogically wrong.

Everything here is synchronous and takes a ``Session``. The async boundary is
in :mod:`studium.retrieval.service`, which runs these through
``studium.asyncdb`` -- the same bridge the rest of the runtime uses (R5).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from .types import (
    CURATED_BOOST,
    NON_EVIDENCE_CHUNK_TYPES,
    RRF_K,
    SEARCH_LIMIT,
    STANCE_BOOST,
)

log = logging.getLogger(__name__)

#: §9: "double the return count is the standard rule".
EF_SEARCH = SEARCH_LIMIT * 2


class SearchUnavailable(RuntimeError):
    """One search modality failed at the database level (§16).

    Caught per modality so a vector index problem degrades to keyword-only
    rather than failing the retrieval. Only both failing produces an empty,
    thin-grounded result.
    """


@dataclass(slots=True)
class Candidate:
    """One chunk in the candidate pool, with everything ranking needs."""

    chunk_id: uuid.UUID
    text: str
    source_id: uuid.UUID
    source_title: str
    source_authors: list[str]
    page_start: int | None
    page_end: int | None
    section_path: list[str]
    chunk_type: str
    chunk_index: int
    #: 'curated' | 'vector' | 'keyword' | 'expanded' (§10).
    reason: str = "vector"
    #: Curated role, when this chunk came through a ``concept_sources`` row.
    #: ``None`` for a pure search hit, which is why §11 gives those no stance
    #: boost: there is no curator classification to match a stance against.
    role: str | None = None
    #: Per-modality ranks, 1-based. Absent means the modality did not return it.
    ranks: dict[str, int] = field(default_factory=dict)
    fused_score: float = 0.0


# --- §10 neighbourhood ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Neighborhood:
    """§10's concept pool, split by how each member got in.

    The split is what keeps §10's audit trail honest. ``core`` members are
    reachable from the focus concept along real edges, so a chunk curated onto
    one of them was chosen by an expert for something genuinely adjacent.
    ``fallback`` members are only "load-bearing in the same subject" (§10 item
    5) -- nobody connected them to this concept at all, and reporting their
    chunks as ``'curated'`` would tell a reviewer an expert picked them *for
    this concept*, which is exactly the claim §10's ``retrieval_reason`` exists
    to make checkable.
    """

    core: tuple[uuid.UUID, ...] = ()
    fallback: tuple[uuid.UUID, ...] = ()

    @property
    def all_ids(self) -> list[uuid.UUID]:
        seen: dict[uuid.UUID, None] = {}
        for cid in (*self.core, *self.fallback):
            seen.setdefault(cid, None)
        return list(seen)

    def __len__(self) -> int:
        return len(self.all_ids)


def concept_neighborhood(
    session: Session, concept_id: uuid.UUID, *, hops: int = 1
) -> Neighborhood:
    """The concept ids retrieval is allowed to draw curated pointers from (§10).

    §10 lists five members: the concept, its prerequisites, its dependants,
    generalisation/related neighbours either way, and load-bearing concepts in
    the same subject as a low-weight fallback. The first four are ``core``; the
    fifth is ``fallback``.

    **Edge direction follows the data layer, not §10's wording.** §10 item 2
    says "a prerequisite edge *from* this concept ... (things this concept
    depends on)". Under subsystem 1's direction those two clauses describe
    opposite sets: ``concept_edges`` stores the prerequisite as
    ``from_concept_id`` and the dependent as ``to_concept_id``, so the things a
    concept depends on are reached by matching ``to_concept_id``. The
    parenthetical states the intent and the preposition contradicts it; the
    intent is followed. See DIVERGENCES-RETRIEVAL (S1).

    ``hops`` is the §10 expansion: two hops when the one-hop curated set comes
    back thin.
    """
    rows = session.execute(
        sql(
            """
            WITH RECURSIVE seed AS (
                -- CAST rather than ::uuid: SQLAlchemy's text() bind-parameter
                -- parser reads the first colon of '::' as the start of another
                -- parameter name and emits a syntax error.
                SELECT CAST(:concept_id AS uuid) AS concept_id, 0 AS depth
            ),
            walk AS (
                SELECT concept_id, depth FROM seed
                UNION
                SELECT n.concept_id, w.depth + 1
                  FROM walk w
                  JOIN LATERAL (
                      -- Things this concept depends on: it is the dependent,
                      -- so it is the edge's *to* side.
                      SELECT e.from_concept_id AS concept_id
                        FROM concept_edges e
                       WHERE e.to_concept_id = w.concept_id
                         AND e.kind IN ('prerequisite', 'dependency')
                      UNION
                      -- Things this concept enables: it is the requirement.
                      SELECT e.to_concept_id
                        FROM concept_edges e
                       WHERE e.from_concept_id = w.concept_id
                         AND e.kind IN ('prerequisite', 'dependency')
                      UNION
                      -- Undirected kinds, both ways (§10 item 4).
                      SELECT e.to_concept_id
                        FROM concept_edges e
                       WHERE e.from_concept_id = w.concept_id
                         AND e.kind IN ('generalization', 'related', 'application')
                      UNION
                      SELECT e.from_concept_id
                        FROM concept_edges e
                       WHERE e.to_concept_id = w.concept_id
                         AND e.kind IN ('generalization', 'related', 'application')
                  ) n ON TRUE
                 WHERE w.depth < :hops
            )
            SELECT DISTINCT walk.concept_id, TRUE AS is_core
              FROM walk
            UNION
            -- §10 item 5: load-bearing concepts in the same subject, as a
            -- fallback pool. Reachable by no edge from the focus concept, so
            -- they are marked and their chunks are reported as 'expanded'.
            SELECT c.id, FALSE
              FROM concepts c
             WHERE c.is_load_bearing
               AND c.subject_id = (
                   SELECT subject_id FROM concepts WHERE id = :concept_id
               )
            """
        ),
        {"concept_id": concept_id, "hops": hops},
    ).all()

    core = {r.concept_id for r in rows if r.is_core}
    fallback = {r.concept_id for r in rows if not r.is_core} - core
    return Neighborhood(
        core=tuple(sorted(core, key=str)),
        fallback=tuple(sorted(fallback, key=str)),
    )


def subject_of(session: Session, concept_id: uuid.UUID) -> uuid.UUID | None:
    """The subject a concept belongs to. Every query is scoped to one (§2)."""
    return session.execute(
        sql("SELECT subject_id FROM concepts WHERE id = :cid"),
        {"cid": concept_id},
    ).scalar()


def curated_candidates(
    session: Session, neighborhood: Neighborhood | Sequence[uuid.UUID]
) -> list[Candidate]:
    """§10 phase 1: the chunks a domain expert pointed at.

    ``concept_sources.chunk_ids`` is an array rather than a join table (data
    layer §6.2), so it is flattened with ``unnest``. The inner join drops
    dangling pointers: a chunk id that no longer resolves degrades this
    concept's grounding rather than failing the query, and the daily
    consistency check reports it separately.

    A chunk reached only through §10's load-bearing fallback comes back as
    ``'expanded'``, not ``'curated'`` -- see :class:`Neighborhood`. A plain
    sequence is accepted and treated as all-core, which is what the
    curated-pointer retriever wants: it queries one concept directly.
    """
    if isinstance(neighborhood, Neighborhood):
        ids = neighborhood.all_ids
        core = set(neighborhood.core)
    else:
        ids = list(neighborhood)
        core = set(ids)

    if not ids:
        return []

    rows = session.execute(
        sql(
            """
            SELECT DISTINCT ON (sc.id)
                   sc.id            AS chunk_id,
                   sc.text          AS text,
                   sc.source_id     AS source_id,
                   sc.page_start    AS page_start,
                   sc.page_end      AS page_end,
                   sc.section_path  AS section_path,
                   sc.chunk_type    AS chunk_type,
                   sc.chunk_index   AS chunk_index,
                   s.title          AS source_title,
                   s.authors        AS source_authors,
                   cs.role          AS role,
                   cs.concept_id    AS via_concept_id
              FROM concept_sources cs
              CROSS JOIN LATERAL unnest(cs.chunk_ids) AS ref(chunk_id)
              JOIN source_chunks sc ON sc.id = ref.chunk_id
              JOIN sources s        ON s.id = sc.source_id
             WHERE cs.concept_id = ANY(:neighborhood)
               AND s.deleted_at IS NULL
               AND s.status <> 'retired'
               AND sc.chunk_type <> ALL(:excluded)
             -- DISTINCT ON keeps one row per chunk; ordering core concepts
             -- first means a chunk curated onto both a core and a fallback
             -- concept is credited to the core one, which is the stronger and
             -- truer claim about it.
             ORDER BY sc.id, (cs.concept_id = ANY(:core)) DESC, cs.role
            """
        ),
        {
            "neighborhood": ids,
            "core": list(core),
            "excluded": list(NON_EVIDENCE_CHUNK_TYPES),
        },
    ).all()

    return [
        _candidate(
            r,
            reason="curated" if r.via_concept_id in core else "expanded",
            role=r.role,
        )
        for r in rows
    ]


def source_ids_for_neighborhood(
    session: Session, neighborhood: Neighborhood | Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    """Sources linked to any concept in the neighbourhood (§10 phase 2).

    This is what keeps expansion honest: vector search runs over the sources a
    curator has already associated with this part of the graph, not over the
    whole subject. A source nobody linked to anything nearby cannot surface a
    passage just because it repeats a word from the query.
    """
    ids = (
        neighborhood.all_ids
        if isinstance(neighborhood, Neighborhood)
        else list(neighborhood)
    )
    if not ids:
        return []
    rows = session.execute(
        sql(
            """
            SELECT DISTINCT cs.source_id
              FROM concept_sources cs
             WHERE cs.concept_id = ANY(:neighborhood)
            """
        ),
        {"neighborhood": ids},
    ).all()
    return [r.source_id for r in rows]


# --- §9 modalities ----------------------------------------------------------


def vector_search(
    session: Session,
    *,
    query_embedding: Sequence[float],
    subject_id: uuid.UUID,
    source_ids: Sequence[uuid.UUID] | None = None,
    limit: int = SEARCH_LIMIT,
) -> list[Candidate]:
    """Top-``limit`` chunks by cosine similarity via pgvector HNSW (§9)."""
    try:
        # SET LOCAL scopes to the transaction, so it cannot leak into another
        # query on a pooled connection.
        session.execute(sql(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}"))
        rows = session.execute(
            sql(
                """
                SELECT sc.id           AS chunk_id,
                       sc.text         AS text,
                       sc.source_id    AS source_id,
                       sc.page_start   AS page_start,
                       sc.page_end     AS page_end,
                       sc.section_path AS section_path,
                       sc.chunk_type   AS chunk_type,
                       sc.chunk_index  AS chunk_index,
                       s.title         AS source_title,
                       s.authors       AS source_authors,
                       (1 - (e.embedding <=> CAST(:query_embedding AS vector)))::float
                                       AS vector_score
                  FROM source_chunks sc
                  JOIN source_chunk_embeddings e ON e.chunk_id = sc.id
                  JOIN sources s ON s.id = sc.source_id
                 WHERE s.subject_id = :subject_id
                   AND s.deleted_at IS NULL
                   AND s.status <> 'retired'
                   AND sc.chunk_type <> ALL(:excluded)
                   AND (:no_source_filter OR sc.source_id = ANY(:source_ids))
                 -- The CAST is a no-op on the binary path (measured: 2.46ms
                 -- with it, 2.50ms without) and is what lets the text
                 -- fallback in _as_vector still work.
                 ORDER BY e.embedding <=> CAST(:query_embedding AS vector)
                 LIMIT :limit
                """
            ),
            {
                "query_embedding": _as_vector(query_embedding),
                "subject_id": subject_id,
                "excluded": list(NON_EVIDENCE_CHUNK_TYPES),
                "no_source_filter": not source_ids,
                "source_ids": list(source_ids or []),
                "limit": limit,
            },
        ).all()
    except Exception as exc:  # noqa: BLE001 -- narrowed for the §16 fallback
        log.warning("vector search failed; falling back to keyword-only: %s", exc)
        raise SearchUnavailable(str(exc)) from exc

    return [
        _candidate(r, reason="vector", rank=("vector", index))
        for index, r in enumerate(rows, start=1)
    ]


def keyword_search(
    session: Session,
    *,
    query_text: str,
    subject_id: uuid.UUID,
    source_ids: Sequence[uuid.UUID] | None = None,
    limit: int = SEARCH_LIMIT,
) -> list[Candidate]:
    """BM25-style ``ts_rank_cd`` over the generated tsvector column (§9).

    ``plainto_tsquery`` rather than ``to_tsquery``: the query is free-form text
    from an agent or a learner, and ``to_tsquery`` would need every special
    character escaped or it raises. A retrieval that fails because a learner
    typed a question mark is not an acceptable failure mode.
    """
    if not query_text.strip():
        return []
    try:
        rows = session.execute(
            sql(
                """
                SELECT sc.id           AS chunk_id,
                       sc.text         AS text,
                       sc.source_id    AS source_id,
                       sc.page_start   AS page_start,
                       sc.page_end     AS page_end,
                       sc.section_path AS section_path,
                       sc.chunk_type   AS chunk_type,
                       sc.chunk_index  AS chunk_index,
                       s.title         AS source_title,
                       s.authors       AS source_authors,
                       ts_rank_cd(
                           sc.tsvector_text,
                           plainto_tsquery('english', :query_text)
                       )::float        AS keyword_score
                  FROM source_chunks sc
                  JOIN sources s ON s.id = sc.source_id
                 WHERE s.subject_id = :subject_id
                   AND s.deleted_at IS NULL
                   AND s.status <> 'retired'
                   AND sc.chunk_type <> ALL(:excluded)
                   AND sc.tsvector_text @@ plainto_tsquery('english', :query_text)
                   AND (:no_source_filter OR sc.source_id = ANY(:source_ids))
                 ORDER BY keyword_score DESC, sc.id
                 LIMIT :limit
                """
            ),
            {
                "query_text": query_text,
                "subject_id": subject_id,
                "excluded": list(NON_EVIDENCE_CHUNK_TYPES),
                "no_source_filter": not source_ids,
                "source_ids": list(source_ids or []),
                "limit": limit,
            },
        ).all()
    except Exception as exc:  # noqa: BLE001 -- narrowed for the §16 fallback
        log.warning("keyword search failed; falling back to vector-only: %s", exc)
        raise SearchUnavailable(str(exc)) from exc

    return [
        _candidate(r, reason="keyword", rank=("keyword", index))
        for index, r in enumerate(rows, start=1)
    ]


# --- §9 fusion --------------------------------------------------------------


def fuse(
    pools: Sequence[Sequence[Candidate]],
    *,
    stance_preferred_roles: Sequence[str] = (),
) -> list[Candidate]:
    """Reciprocal Rank Fusion over per-modality pools, then §10/§11 boosts.

    RRF rather than score averaging because the modalities' scores are not
    comparable and a chunk missing from one pool has no score there to average.
    Rank is available for both, and "absent" is simply "no term to add".

    Boosts are multiplicative and ordered: curated first (§10), stance second
    (§11, "applied after the curated boost"). Both are applied here rather than
    at their own call sites so the order is visible in one place -- multiplying
    in the other order gives the same product, but a future additive term would
    not, and this is where that would be noticed.

    Ties break on ``chunk_id``. Fusion scores collide often -- two chunks at
    the same rank in one modality and absent from the other score identically
    -- and an arbitrary tiebreak would make the candidate order, and therefore
    the reranker's input, vary between two identical calls.
    """
    merged: dict[uuid.UUID, Candidate] = {}

    for pool in pools:
        for candidate in pool:
            existing = merged.get(candidate.chunk_id)
            if existing is None:
                merged[candidate.chunk_id] = candidate
                continue
            existing.ranks.update(candidate.ranks)
            # A chunk found by both a curated pointer and a search modality is
            # curated: that is the stronger claim about it, and §10's audit
            # trail should say an expert chose it, not that a cosine did.
            if candidate.reason == "curated" or existing.reason == "curated":
                existing.reason = "curated"
                existing.role = existing.role or candidate.role

    preferred = set(stance_preferred_roles)
    for candidate in merged.values():
        score = sum(1.0 / (RRF_K + rank) for rank in candidate.ranks.values())
        if candidate.reason == "curated":
            # A curated chunk that no modality returned still has to score, or
            # phase 1 would contribute nothing whenever search missed it --
            # which is exactly the sparse-corpus case curation exists for.
            if not candidate.ranks:
                score = 1.0 / (RRF_K + SEARCH_LIMIT)
            score *= CURATED_BOOST
        if candidate.role is not None and candidate.role in preferred:
            score *= STANCE_BOOST
        candidate.fused_score = score

    return sorted(
        merged.values(),
        key=lambda c: (-c.fused_score, str(c.chunk_id)),
    )


def expand_atomic_siblings(
    session: Session, selected: Sequence[Candidate], *, allowance: int
) -> list[Candidate]:
    """Pull in the sibling chunks of a split code or math block (§6).

    §7 splits a code or math block past 2000 tokens, and §6 allows the result
    set to exceed ``k`` by up to three so such a block comes back whole:
    returning half a derivation is evidence for a claim neither half supports.

    ``chunk_relations`` -- the explicit parent/child link -- is deferred to
    v1.1 (§19), so siblinghood is recovered from adjacency: consecutive
    ``chunk_index`` values in the same source with the same atomic
    ``chunk_type``. That is exact for blocks the chunker split, because it
    writes the pieces consecutively and nothing else can produce a run of
    adjacent same-type atomic chunks.
    """
    atomic = [c for c in selected if c.chunk_type in ("code", "math")]
    if not atomic or allowance <= 0:
        return []

    have = {c.chunk_id for c in selected}
    rows = session.execute(
        sql(
            """
            SELECT sc.id           AS chunk_id,
                   sc.text         AS text,
                   sc.source_id    AS source_id,
                   sc.page_start   AS page_start,
                   sc.page_end     AS page_end,
                   sc.section_path AS section_path,
                   sc.chunk_type   AS chunk_type,
                   sc.chunk_index  AS chunk_index,
                   s.title         AS source_title,
                   s.authors       AS source_authors
              FROM source_chunks sc
              JOIN sources s ON s.id = sc.source_id
              JOIN unnest(
                       CAST(:source_ids AS uuid[]),
                       CAST(:indexes AS int[]),
                       CAST(:kinds AS text[])
                   ) AS seed(source_id, chunk_index, chunk_type) ON TRUE
             WHERE sc.source_id = seed.source_id
               AND sc.chunk_type = CAST(seed.chunk_type AS chunk_kind)
               AND sc.chunk_index BETWEEN seed.chunk_index - 1 AND seed.chunk_index + 1
               AND s.deleted_at IS NULL
               AND s.status <> 'retired'
             ORDER BY sc.source_id, sc.chunk_index
             LIMIT :limit
            """
        ),
        {
            "source_ids": [str(c.source_id) for c in atomic],
            "indexes": [c.chunk_index for c in atomic],
            "kinds": [c.chunk_type for c in atomic],
            "limit": allowance + len(atomic) * 2,
        },
    ).all()

    siblings = [
        _candidate(r, reason="expanded")
        for r in rows
        if r.chunk_id not in have
    ]
    return siblings[:allowance]


# --- row mapping ------------------------------------------------------------


def _candidate(
    row: object,
    *,
    reason: str,
    role: str | None = None,
    rank: tuple[str, int] | None = None,
) -> Candidate:
    return Candidate(
        chunk_id=row.chunk_id,
        text=row.text,
        source_id=row.source_id,
        source_title=row.source_title or "",
        source_authors=list(row.source_authors or []),
        page_start=row.page_start,
        page_end=row.page_end,
        section_path=_section_path(row.section_path),
        chunk_type=str(row.chunk_type),
        chunk_index=int(row.chunk_index),
        reason=reason,
        role=role,
        ranks={rank[0]: rank[1]} if rank else {},
    )


def _section_path(raw: object) -> list[str]:
    """Coerce the JSONB ``section_path`` into the ``list[str]`` §6 promises.

    The column is JSONB carrying "hierarchical annotations without a rigid
    shape" (data layer §6.3), so an author could put an object in it. The
    hover card renders strings, so anything else is stringified here rather
    than at the boundary where it would reach the frontend as a dict.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [s if isinstance(s, str) else str(s) for s in raw]
    return [str(raw)]


def _as_vector(values: Sequence[float]) -> object:
    """Wrap an embedding so psycopg sends it as a binary ``vector``.

    ``studium.db`` registers pgvector's adapter on every connection, and this
    wrapper is what selects it: a bare list would be sent as
    ``double precision[]``, which has no ``<=>`` operator.

    The alternative -- a text literal cast in SQL -- is what this replaced, and
    it cost 42 ms per search in parameter parsing alone at MVP corpus size (see
    ``db._register_vector_type``). Falls back to that text form when pgvector's
    Python package is too old to expose ``Vector``, so the query still runs.
    """
    try:
        from pgvector import Vector

        return Vector(list(values))
    except ImportError:  # pragma: no cover -- older pgvector
        return _vector_literal(values)


def _vector_literal(values: Sequence[float]) -> str:
    """pgvector's text input form. The slow path; see :func:`_as_vector`."""
    return "[" + ",".join(f"{v:.7g}" for v in values) + "]"

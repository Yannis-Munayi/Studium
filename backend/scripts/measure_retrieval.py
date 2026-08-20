"""Seed a corpus at a named scale and measure §9's latency claims.

Retrieval §9 makes three claims this script exists to check:

===================  ==========================================================
MVP                  ~10-20 sources, ~5,000-15,000 chunks; both queries under
                     50ms warm
Classroom            ~150 sources, ~150,000 chunks; both under 150ms warm
University           ~2,000 sources, ~2,000,000 chunks; vector stays sub-100ms
                     via HNSW, keyword "may exceed 200ms"
===================  ==========================================================

    python scripts/measure_retrieval.py --tier mvp
    python scripts/measure_retrieval.py --tier classroom --reset

Companion to ``seed_volume.py``, which sizes *learner activity*. The two are
separate because §9's corpus tiers and §15's user tiers are different axes: a
classroom of 30 learners can share an MVP-sized corpus, and one learner can sit
on a university-sized one.

**Embeddings are synthetic and deterministic**, from
``StubEmbeddings`` -- the same hash-based vectors the offline tests use.
Latency is what is being measured, and pgvector's cost depends on dimensionality
and row count, not on whether the numbers mean anything. Nothing here says
anything about *ranking quality*; that needs the real provider and a labelled
set, which is Tier 3 and subsystem 6 respectively.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from studium.db import SessionLocal  # noqa: E402
from studium.retrieval.providers import StubEmbeddings  # noqa: E402
from studium.retrieval.search import (  # noqa: E402
    EF_SEARCH,
    _as_vector,
    keyword_search,
    vector_search,
)


@dataclass(frozen=True)
class Tier:
    """A §9 corpus size. Chunk counts are the midpoint of the spec's range."""

    subjects: int
    sources: int
    chunks: int
    #: §9's stated ceiling per modality, milliseconds. None where §9 declines
    #: to make a claim.
    vector_budget_ms: float | None
    keyword_budget_ms: float | None


TIERS = {
    "mvp": Tier(subjects=1, sources=15, chunks=10_000, vector_budget_ms=50, keyword_budget_ms=50),
    "classroom": Tier(subjects=10, sources=150, chunks=150_000, vector_budget_ms=150, keyword_budget_ms=150),
    # §9 expects keyword to exceed 200ms here and calls partitioning a v2
    # concern, so only the vector claim is a budget.
    "university": Tier(subjects=100, sources=2_000, chunks=2_000_000, vector_budget_ms=100, keyword_budget_ms=None),
}

#: Queries to time. Deliberately varied: a common word exercises the worst case
#: for keyword search (many matches to rank), a rare one the best.
QUERIES = [
    "beta reduction substitution redex",
    "Church-Rosser confluence theorem",
    "normal form termination",
    "alpha equivalence variable capture",
    "fixed point combinator recursion",
]

CHUNK_TEMPLATE = (
    "Section {i}. Beta reduction is the computational rule of the lambda "
    "calculus: the redex (lambda x. M) N contracts to M with every free "
    "occurrence of x replaced by N. Substitution must avoid capturing the free "
    "variables of N, which is why alpha-equivalence comes first. A term with no "
    "remaining redex is in normal form, and the Church-Rosser theorem "
    "guarantees such a form is unique up to alpha-equivalence when it exists. "
    "Topic marker {topic}."
)

TOPICS = ("confluence", "termination", "combinator", "encoding", "evaluation")


def reset(session: Session) -> None:
    """Drop the synthetic corpus. Leaves real fixture data alone."""
    session.execute(
        text(
            """
            DELETE FROM source_chunk_embeddings
             WHERE chunk_id IN (
                 SELECT sc.id FROM source_chunks sc
                 JOIN sources s ON s.id = sc.source_id
                 WHERE s.storage_path LIKE 'synthetic://%'
             )
            """
        )
    )
    session.execute(text("DELETE FROM sources WHERE storage_path LIKE 'synthetic://%'"))
    session.commit()


def seed(session: Session, tier: Tier, *, batch: int = 20_000) -> dict[str, int]:
    """Create subjects, sources, chunks and embeddings at ``tier``'s size.

    Set-based: a university-tier run is two million chunks, and inserting those
    through the ORM would take longer than the measurement it supports.

    Embeddings are the one part that cannot be done in SQL -- the vectors come
    from Python -- so they go in batches, with the HNSW index built *after* the
    rows land. Building it first would mean paying insertion cost into a live
    index for every one of those rows, which is both slower and not how a real
    ingestion would do it.
    """
    subject_ids = []
    for s in range(tier.subjects):
        sid = uuid.uuid4()
        session.execute(
            text(
                """
                INSERT INTO subjects (id, slug, title, status)
                VALUES (:id, :slug, :title, 'active')
                """
            ),
            {"id": sid, "slug": f"synthetic-subject-{s}-{sid.hex[:6]}", "title": f"Synthetic Subject {s}"},
        )
        subject_ids.append(sid)

    per_subject = max(1, tier.sources // tier.subjects)
    source_ids: list[uuid.UUID] = []
    for index in range(tier.sources):
        src = uuid.uuid4()
        session.execute(
            text(
                """
                INSERT INTO sources
                    (id, subject_id, title, authors, license, storage_path,
                     content_sha256, status)
                VALUES (:id, :sid, :title, ARRAY['Synthetic, A.'], 'public_domain',
                        :path, :sha, 'active')
                """
            ),
            {
                "id": src,
                "sid": subject_ids[index // per_subject % len(subject_ids)],
                "title": f"Synthetic Source {index}",
                "path": f"synthetic://source/{index}",
                "sha": f"{index:064x}",
            },
        )
        source_ids.append(src)
    session.commit()

    per_source = max(1, tier.chunks // tier.sources)
    print(f"  seeding {tier.chunks:,} chunks across {tier.sources} sources...")

    for n, src in enumerate(source_ids):
        session.execute(
            text(
                """
                INSERT INTO source_chunks
                    (source_id, chunk_index, text, token_count,
                     page_start, page_end, section_path, chunk_type)
                SELECT :src,
                       g,
                       replace(replace(:tmpl, '{i}', g::text), '{topic}',
                               (ARRAY[:t0,:t1,:t2,:t3,:t4])[1 + (g % 5)]),
                       95,
                       1 + g / 3, 1 + g / 3,
                       '["Ch 1", "1.1"]'::jsonb,
                       'body'
                  FROM generate_series(0, :n - 1) AS g
                """
            ),
            {
                "src": src,
                "tmpl": CHUNK_TEMPLATE,
                "n": per_source,
                **{f"t{i}": t for i, t in enumerate(TOPICS)},
            },
        )
        if n % 25 == 0:
            session.commit()
    session.commit()

    total = session.execute(
        text("SELECT count(*) FROM source_chunks sc JOIN sources s ON s.id = sc.source_id "
             "WHERE s.storage_path LIKE 'synthetic://%'")
    ).scalar_one()
    print(f"  {total:,} chunks written; embedding...")

    stub = StubEmbeddings()
    embedded = 0
    while True:
        rows = session.execute(
            text(
                """
                SELECT sc.id, sc.text
                  FROM source_chunks sc
                  JOIN sources s ON s.id = sc.source_id
                 WHERE s.storage_path LIKE 'synthetic://%'
                   AND NOT EXISTS (
                       SELECT 1 FROM source_chunk_embeddings e WHERE e.chunk_id = sc.id
                   )
                 LIMIT :batch
                """
            ),
            {"batch": batch},
        ).all()
        if not rows:
            break

        # Binary, via executemany. The text-literal form this replaced spent
        # minutes per batch building ~300MB of strings while Postgres sat
        # waiting on the client -- the same cost SD4 found dominating query
        # latency, except here it is paid 150,000 times.
        session.execute(
            text(
                """
                INSERT INTO source_chunk_embeddings (chunk_id, embedding, model_version)
                VALUES (:cid, CAST(:vec AS vector), 'stub-1')
                ON CONFLICT (chunk_id) DO NOTHING
                """
            ),
            [{"cid": r.id, "vec": _as_vector(stub.vector(r.text))} for r in rows],
        )
        session.commit()
        embedded += len(rows)
        print(f"    embedded {embedded:,}/{total:,}")

    print("  analyzing...")
    session.execute(text("ANALYZE source_chunks"))
    session.execute(text("ANALYZE source_chunk_embeddings"))
    session.commit()

    return {"chunks": int(total), "embedded": embedded, "sources": len(source_ids)}


def measure(
    session: Session, subject_id: uuid.UUID, *, runs: int = 5
) -> dict[str, dict[str, float]]:
    """Time both modalities. First run per query is discarded as cold.

    §9's claims are all stated "with warm caches", so a cold first touch would
    measure something the spec is not claiming. Reporting median and p95 rather
    than a mean: latency distributions have a tail, and the tail is what a
    learner notices.
    """
    stub = StubEmbeddings()
    results: dict[str, list[float]] = {"vector": [], "keyword": []}

    for query in QUERIES:
        embedding = stub.vector(query)
        for attempt in range(runs + 1):
            t0 = time.perf_counter()
            vector_search(session, query_embedding=embedding, subject_id=subject_id)
            elapsed = (time.perf_counter() - t0) * 1000
            if attempt:
                results["vector"].append(elapsed)

            t0 = time.perf_counter()
            keyword_search(session, query_text=query, subject_id=subject_id)
            elapsed = (time.perf_counter() - t0) * 1000
            if attempt:
                results["keyword"].append(elapsed)

    return {
        name: {
            "median": statistics.median(samples),
            "p95": sorted(samples)[int(len(samples) * 0.95) - 1],
            "min": min(samples),
            "max": max(samples),
            "n": len(samples),
        }
        for name, samples in results.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=sorted(TIERS), default="mvp")
    parser.add_argument("--reset", action="store_true", help="drop the synthetic corpus first")
    parser.add_argument("--measure-only", action="store_true")
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()

    tier = TIERS[args.tier]
    session = SessionLocal()

    try:
        if args.reset:
            print("resetting synthetic corpus...")
            reset(session)

        if not args.measure_only:
            print(f"\nseeding tier '{args.tier}': {tier.chunks:,} chunks")
            started = time.monotonic()
            stats = seed(session, tier)
            print(f"  done in {time.monotonic() - started:.0f}s: {stats}")

        subject_id = session.execute(
            text(
                """
                SELECT s.subject_id
                  FROM sources s
                 WHERE s.storage_path LIKE 'synthetic://%'
                 GROUP BY s.subject_id
                 ORDER BY count(*) DESC
                 LIMIT 1
                """
            )
        ).scalar()
        if subject_id is None:
            print("no synthetic corpus present; seed one first")
            return 1

        total = session.execute(
            text(
                """
                SELECT count(*) FROM source_chunks sc
                  JOIN sources s ON s.id = sc.source_id
                 WHERE s.subject_id = :sid
                """
            ),
            {"sid": subject_id},
        ).scalar_one()

        print(f"\nmeasuring against subject {subject_id} ({total:,} chunks, ef_search={EF_SEARCH})")
        stats = measure(session, subject_id, runs=args.runs)

        print(f"\n{'modality':10} {'median':>9} {'p95':>9} {'min':>9} {'max':>9}   §9 budget")
        print("-" * 68)
        exit_code = 0
        for name, s in stats.items():
            budget = tier.vector_budget_ms if name == "vector" else tier.keyword_budget_ms
            if budget is None:
                verdict = "(no claim)"
            elif s["p95"] <= budget:
                verdict = f"<= {budget:.0f}ms  PASS"
            else:
                verdict = f"<= {budget:.0f}ms  MISS"
                exit_code = 1
            print(
                f"{name:10} {s['median']:8.1f}ms {s['p95']:8.1f}ms "
                f"{s['min']:8.1f}ms {s['max']:8.1f}ms   {verdict}"
            )

        print(
            "\nNote: synthetic embeddings and one warm connection. This measures "
            "index and query cost at scale, not ranking quality."
        )
        return exit_code
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())

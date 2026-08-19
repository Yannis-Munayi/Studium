"""Tier 2: retrieval against real Postgres with pgvector (retrieval §17).

These are the checks that cannot be made offline because they are about what
Postgres does: whether HNSW returns what it should, whether the generated
tsvector column actually indexes, whether the concept-graph filter narrows,
whether the curated boost survives a round trip through SQL.

The providers stay stubbed. Tier 3 is where real Voyage calls happen; mixing
them in here would make a database test fail on an API outage.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sql

from studium.retrieval import CuratedPointerRetriever, HybridRetriever
from studium.retrieval.cache import load_bearing_concepts
from studium.retrieval.citations import resolve_artifact_citations
from studium.retrieval.embedding_worker import EmbeddingWorker
from studium.retrieval.providers import StubEmbeddings
from studium.retrieval.search import (
    concept_neighborhood,
    curated_candidates,
    keyword_search,
    source_ids_for_neighborhood,
    vector_search,
)
from tests.fixtures import lambda_calculus

pytestmark = pytest.mark.postgres


@pytest.fixture
def corpus(db):
    """The shared fixture plus §17's three extra sources."""
    fixture = lambda_calculus.build(db, email=f"retrieval-{uuid.uuid4().hex[:8]}@example.com")
    extra = lambda_calculus.extend_corpus(db, fixture)
    db.flush()
    return {"fixture": fixture, "extra": extra, "db": db}


def embed_everything(db, fixture) -> None:
    """Give every chunk a stub embedding, so vector search has an index to use."""
    stub = StubEmbeddings()
    rows = db.execute(sql("SELECT id, text FROM source_chunks")).all()
    for row in rows:
        vector = stub.vector(row.text)
        db.execute(
            sql(
                """
                INSERT INTO source_chunk_embeddings (chunk_id, embedding, model_version)
                VALUES (:cid, CAST(:vec AS vector), 'stub-1')
                ON CONFLICT (chunk_id) DO NOTHING
                """
            ),
            {"cid": row.id, "vec": "[" + ",".join(f"{v:.7g}" for v in vector) + "]"},
        )
    db.flush()


class TestSchemaAdditions:
    """§5's two columns, as the database actually built them."""

    def test_chunk_type_defaults_to_body(self, corpus):
        db = corpus["db"]
        kinds = db.execute(
            sql("SELECT DISTINCT chunk_type FROM source_chunks ORDER BY chunk_type")
        ).scalars().all()
        assert "body" in kinds
        assert {"heading", "code", "math", "exercise"} <= set(kinds)

    def test_the_tsvector_column_is_generated_not_written(self, corpus):
        """It must track ``text`` without anyone maintaining it."""
        db = corpus["db"]
        chunk_id = corpus["fixture"].chunks[0].id

        db.execute(
            sql("UPDATE source_chunks SET text = :t WHERE id = :cid"),
            {"t": "an entirely different sentence about combinators", "cid": chunk_id},
        )
        db.flush()

        matched = db.execute(
            sql(
                """
                SELECT tsvector_text @@ plainto_tsquery('english', 'combinators')
                  FROM source_chunks WHERE id = :cid
                """
            ),
            {"cid": chunk_id},
        ).scalar()
        assert matched is True

    def test_the_tsvector_column_cannot_be_written_directly(self, corpus):
        """A generated column that accepted writes could drift from its source."""
        from sqlalchemy.exc import DatabaseError

        db = corpus["db"]
        with pytest.raises(DatabaseError):
            db.execute(sql("UPDATE source_chunks SET tsvector_text = NULL"))
            db.flush()


class TestConceptGraph:
    """§10's neighbourhood."""

    def test_the_neighbourhood_includes_prerequisites_and_dependants(self, corpus):
        fixture = corpus["fixture"]
        beta = fixture.concept_id("beta-reduction")

        neighborhood = concept_neighborhood(corpus["db"], beta)

        assert beta in neighborhood.core
        # Things beta-reduction depends on.
        assert fixture.concept_id("syntax") in neighborhood.core
        assert fixture.concept_id("alpha-equivalence") in neighborhood.core
        # Things it enables.
        assert fixture.concept_id("church-rosser") in neighborhood.core

    def test_a_second_hop_widens_the_neighbourhood(self, corpus):
        fixture = corpus["fixture"]
        syntax = fixture.concept_id("syntax")

        one = set(concept_neighborhood(corpus["db"], syntax, hops=1).core)
        two = set(concept_neighborhood(corpus["db"], syntax, hops=2).core)
        assert one <= two
        assert len(two) > len(one)

    def test_an_unconnected_concept_has_only_the_fallback_pool(self, corpus):
        """§10 item 5. The load-bearing concepts of the subject are reachable
        from anything, so they must not be reported as core."""
        db, fixture = corpus["db"], corpus["fixture"]
        orphan = _insert_orphan_concept(db, fixture)

        neighborhood = concept_neighborhood(db, orphan)

        assert neighborhood.core == (orphan,)
        assert neighborhood.fallback
        assert fixture.concept_id("beta-reduction") in neighborhood.fallback

    def test_fallback_chunks_are_reported_as_expanded_not_curated(self, corpus):
        """The audit-trail claim §10 makes checkable: 'curated' must mean an
        expert chose this chunk *for this concept*."""
        db, fixture = corpus["db"], corpus["fixture"]
        orphan = _insert_orphan_concept(db, fixture)

        candidates = curated_candidates(db, concept_neighborhood(db, orphan))

        assert candidates, "the fallback pool does supply chunks"
        assert all(c.reason == "expanded" for c in candidates)

    def test_curated_pointers_come_back_for_the_neighbourhood(self, corpus):
        fixture = corpus["fixture"]
        neighborhood = concept_neighborhood(corpus["db"], fixture.concept_id("beta-reduction"))

        candidates = curated_candidates(corpus["db"], neighborhood)

        assert candidates
        assert all(c.reason == "curated" for c in candidates)
        assert all(c.role for c in candidates), "the curated role must survive"

    def test_headings_and_references_are_never_curated_candidates(self, corpus):
        """§9: structurally present, useless as evidence."""
        fixture = corpus["fixture"]
        neighborhood = concept_neighborhood(corpus["db"], fixture.concept_id("beta-reduction"))
        candidates = curated_candidates(corpus["db"], neighborhood)
        assert all(c.chunk_type not in ("heading", "reference") for c in candidates)

    def test_source_ids_narrow_to_the_neighbourhood(self, corpus):
        fixture = corpus["fixture"]
        neighborhood = concept_neighborhood(corpus["db"], fixture.concept_id("beta-reduction"))
        source_ids = source_ids_for_neighborhood(corpus["db"], neighborhood)
        assert source_ids

    def test_a_retired_source_contributes_nothing(self, corpus):
        """§12: retirement is the workflow for a source that should stop
        grounding new content."""
        db, fixture = corpus["db"], corpus["fixture"]
        neighborhood = concept_neighborhood(db, fixture.concept_id("beta-reduction"))

        before = len(curated_candidates(db, neighborhood))
        db.execute(sql("UPDATE sources SET status = 'retired'"))
        db.flush()

        assert curated_candidates(db, neighborhood) == []
        assert before > 0


class TestVectorSearch:
    """§17: "Insert chunks with known-distance embeddings, verify vector search
    returns them in expected order"."""

    def test_hnsw_returns_the_nearest_chunk_first(self, corpus):
        db, fixture = corpus["db"], corpus["fixture"]
        embed_everything(db, fixture)

        target = db.execute(
            sql("SELECT id, text FROM source_chunks ORDER BY id LIMIT 1")
        ).one()
        query_vector = StubEmbeddings().vector(target.text)

        results = vector_search(
            db, query_embedding=query_vector, subject_id=fixture.subject.id
        )

        assert results
        assert results[0].chunk_id == target.id, "an exact match must rank first"

    def test_vector_search_excludes_non_evidence_chunk_types(self, corpus):
        db, fixture = corpus["db"], corpus["fixture"]
        embed_everything(db, fixture)

        results = vector_search(
            db,
            query_embedding=StubEmbeddings().vector("beta reduction"),
            subject_id=fixture.subject.id,
        )
        assert results
        assert all(r.chunk_type not in ("heading", "reference") for r in results)

    def test_a_chunk_with_no_embedding_is_simply_invisible(self, corpus):
        """§8: not an error -- a corpus mid-embedding is still usable."""
        db, fixture = corpus["db"], corpus["fixture"]
        results = vector_search(
            db,
            query_embedding=StubEmbeddings().vector("anything"),
            subject_id=fixture.subject.id,
        )
        assert results == []


class TestKeywordSearch:
    """§17: "Insert chunks with known keyword content, verify tsvector query
    returns matches"."""

    def test_it_finds_a_distinctive_term(self, corpus):
        db, fixture = corpus["db"], corpus["fixture"]
        results = keyword_search(
            db, query_text="Church-Rosser theorem", subject_id=fixture.subject.id
        )
        assert results
        assert any("Church-Rosser" in r.text for r in results)

    def test_it_survives_free_form_punctuation(self, corpus):
        """plainto_tsquery rather than to_tsquery: a learner's question mark
        must not raise."""
        db, fixture = corpus["db"], corpus["fixture"]
        results = keyword_search(
            db,
            query_text="what *is* a redex? (and why!) -- explain :)",
            subject_id=fixture.subject.id,
        )
        assert isinstance(results, list)

    def test_an_empty_query_returns_nothing_rather_than_everything(self, corpus):
        db, fixture = corpus["db"], corpus["fixture"]
        assert keyword_search(db, query_text="   ", subject_id=fixture.subject.id) == []

    def test_it_excludes_non_evidence_chunk_types(self, corpus):
        db, fixture = corpus["db"], corpus["fixture"]
        results = keyword_search(
            db, query_text="Barendregt lambda calculus syntax semantics",
            subject_id=fixture.subject.id,
        )
        assert all(r.chunk_type not in ("heading", "reference") for r in results)


class TestEndToEndRetrieval:
    async def test_a_full_retrieval_returns_grounded_passages(self, corpus, monkeypatch):
        db, fixture = corpus["db"], corpus["fixture"]
        embed_everything(db, fixture)
        _use_test_session(monkeypatch, db)

        result = await HybridRetriever(use_cache=False).retrieve_passages(
            fixture.concept_id("beta-reduction"), stance="formal"
        )

        assert result.passages
        assert result.query_used
        ids = [str(p.chunk_id) for p in result.passages]
        assert ids == sorted(ids), "§3's numbering contract"
        for passage in result.passages:
            assert passage.source_title
            assert passage.source_id is not None

    async def test_a_curated_chunk_outranks_an_uncurated_one(self, corpus, monkeypatch):
        """§17: "A concept_sources row pointing at a specific chunk causes that
        chunk to rank higher"."""
        db, fixture = corpus["db"], corpus["fixture"]
        embed_everything(db, fixture)
        _use_test_session(monkeypatch, db)

        result = await HybridRetriever(use_cache=False).retrieve_passages(
            fixture.concept_id("beta-reduction")
        )

        by_score = sorted(
            result.passages, key=lambda p: p.relevance_score or 0, reverse=True
        )
        assert by_score
        assert by_score[0].retrieval_reason == "curated"

    async def test_an_uncurated_concept_is_grounded_only_by_expansion(
        self, corpus, monkeypatch
    ):
        """§10 item 5 means a concept with no curation of its own still gets
        the subject's load-bearing material -- which is better than nothing,
        and is why this does *not* trip thin grounding.

        What must hold is that none of it claims to be curated for this
        concept. A reviewer seeing an all-'expanded' result knows the concept's
        own ``concept_sources`` rows are missing, which is §13's feedback loop
        arriving by a different route than the count threshold."""
        db, fixture = corpus["db"], corpus["fixture"]
        _use_test_session(monkeypatch, db)

        orphan = _insert_orphan_concept(db, fixture)

        result = await HybridRetriever(use_cache=False).retrieve_passages(orphan)

        assert result.passages
        assert all(p.retrieval_reason != "curated" for p in result.passages)

    async def test_a_subject_with_nothing_curated_is_flagged_thin(
        self, corpus, monkeypatch
    ):
        """§16: "No chunks in the concept neighborhood" -- the genuinely empty
        case, once the fallback pool has nothing in it either."""
        db, fixture = corpus["db"], corpus["fixture"]
        _use_test_session(monkeypatch, db)

        orphan = _insert_orphan_concept(db, fixture)
        db.execute(sql("DELETE FROM concept_sources"))
        db.flush()

        result = await HybridRetriever(use_cache=False).retrieve_passages(orphan)
        assert result.passages == []
        assert result.thin_grounding is True
        assert result.thin_grounding_reason

    async def test_the_curated_pointer_retriever_satisfies_the_same_contract(
        self, corpus, monkeypatch
    ):
        """Swapping retrievers is the only change an agent sees (§25)."""
        db, fixture = corpus["db"], corpus["fixture"]
        _use_test_session(monkeypatch, db)

        result = await CuratedPointerRetriever().retrieve_passages(
            fixture.concept_id("beta-reduction")
        )

        assert result.passages
        ids = [str(p.chunk_id) for p in result.passages]
        assert ids == sorted(ids)
        assert all(p.retrieval_reason == "curated" for p in result.passages)


class TestEmbeddingWorker:
    """§8."""

    async def test_it_embeds_unembedded_chunks(self, corpus, monkeypatch):
        db = corpus["db"]
        _use_test_session(monkeypatch, db, write=True)

        report = await EmbeddingWorker().run_once()
        assert report.embedded > 0
        assert report.cost_usd > 0

        remaining = db.execute(
            sql(
                """
                SELECT COUNT(*) FROM source_chunks sc
                 WHERE NOT EXISTS (
                     SELECT 1 FROM source_chunk_embeddings e WHERE e.chunk_id = sc.id
                 )
                """
            )
        ).scalar()
        assert remaining < 80

    async def test_it_is_idempotent(self, corpus, monkeypatch):
        db, fixture = corpus["db"], corpus["fixture"]
        embed_everything(db, fixture)
        _use_test_session(monkeypatch, db, write=True)

        report = await EmbeddingWorker().run_once()
        assert report.embedded == 0, "nothing left to do"

    async def test_load_bearing_material_is_embedded_first(self, corpus, monkeypatch):
        """§8 "Batch scheduling"."""
        db = corpus["db"]
        _use_test_session(monkeypatch, db, write=True)

        report = await EmbeddingWorker(batch_size=4).run_once()
        assert report.embedded == 4

        embedded = db.execute(
            sql(
                """
                SELECT EXISTS (
                    SELECT 1
                      FROM source_chunk_embeddings e
                      JOIN concept_sources cs ON e.chunk_id = ANY(cs.chunk_ids)
                      JOIN concepts c ON c.id = cs.concept_id
                     WHERE c.is_load_bearing
                )
                """
            )
        ).scalar()
        assert embedded is True


class TestCitationResolution:
    """§17: "Given an artifact with three content_citations rows, the endpoint
    returns the passages in chunk_id-sorted order with correct metadata"."""

    def _artifact_with_citations(self, db, fixture, chunk_ids):
        artifact_id = uuid.uuid4()
        db.execute(
            sql(
                """
                INSERT INTO content_artifacts
                    (id, concept_id, kind, stance, body, generated_by, status)
                VALUES (:id, :cid, 'lecture_segment', 'formal', 'A segment [P1][P2][P3].',
                        'lecturer', 'draft')
                """
            ),
            {"id": artifact_id, "cid": fixture.concept_id("beta-reduction")},
        )
        for chunk_id in chunk_ids:
            db.execute(
                sql(
                    """
                    INSERT INTO content_citations (artifact_id, source_chunk_id)
                    VALUES (:aid, :cid)
                    """
                ),
                {"aid": artifact_id, "cid": chunk_id},
            )
        db.flush()
        return artifact_id

    def test_citations_resolve_in_chunk_id_order(self, corpus):
        db, fixture = corpus["db"], corpus["fixture"]
        # Deliberately inserted out of order.
        chunk_ids = [c.id for c in fixture.chunks[:3]]
        artifact_id = self._artifact_with_citations(db, fixture, reversed(chunk_ids))

        resolved = resolve_artifact_citations(db, artifact_id)

        assert [c.marker for c in resolved] == ["P1", "P2", "P3"]
        assert [str(c.chunk_id) for c in resolved] == sorted(
            str(c) for c in chunk_ids
        )

    def test_every_citation_carries_the_provenance_a_hover_card_needs(self, corpus):
        db, fixture = corpus["db"], corpus["fixture"]
        artifact_id = self._artifact_with_citations(
            db, fixture, [c.id for c in fixture.chunks[:3]]
        )

        for citation in resolve_artifact_citations(db, artifact_id):
            assert citation.source_title
            assert citation.source_authors
            assert citation.page_start is not None
            assert citation.section_path
            assert citation.excerpt

    def test_a_retired_source_resolves_as_inactive_not_broken(self, corpus):
        """§12: the marker renders inactive, and the row is retained."""
        db, fixture = corpus["db"], corpus["fixture"]
        artifact_id = self._artifact_with_citations(
            db, fixture, [c.id for c in fixture.chunks[:3]]
        )
        db.execute(sql("UPDATE sources SET status = 'retired'"))
        db.flush()

        resolved = resolve_artifact_citations(db, artifact_id)
        assert len(resolved) == 3, "retention is preserved"
        assert all(c.source_deleted for c in resolved)
        assert all(c.excerpt == "" for c in resolved)

    def test_a_cited_chunk_cannot_be_hard_deleted(self, corpus):
        """§12: ON DELETE RESTRICT is what makes retirement the only workflow."""
        from sqlalchemy.exc import IntegrityError

        db, fixture = corpus["db"], corpus["fixture"]
        chunk_ids = [c.id for c in fixture.chunks[:3]]
        self._artifact_with_citations(db, fixture, chunk_ids)

        with pytest.raises(IntegrityError):
            db.execute(
                sql("DELETE FROM source_chunks WHERE id = :cid"), {"cid": chunk_ids[0]}
            )
            db.flush()


class TestCacheWarming:
    def test_load_bearing_concepts_are_discoverable(self, corpus):
        """§14 pre-warms exactly this set."""
        db, fixture = corpus["db"], corpus["fixture"]
        db.execute(
            sql("UPDATE subjects SET status = 'active' WHERE id = :sid"),
            {"sid": fixture.subject.id},
        )
        db.flush()

        concepts = load_bearing_concepts(db)
        assert fixture.concept_id("beta-reduction") in concepts


def _insert_orphan_concept(db, fixture) -> uuid.UUID:
    """A concept with no edges and no ``concept_sources`` rows of its own."""
    orphan = uuid.uuid4()
    db.execute(
        sql(
            """
            INSERT INTO concepts (id, subject_id, slug, title, depth, position)
            VALUES (:id, :sid, 'orphan-concept', 'An orphan', 1, 99)
            """
        ),
        {"id": orphan, "sid": fixture.subject.id},
    )
    db.flush()
    return orphan


def _use_test_session(monkeypatch, db, *, write: bool = False) -> None:
    """Point ``asyncdb`` at the test's transaction.

    The retrieval service reaches the database through ``asyncdb.read_db`` /
    ``run_db``, which open their own sessions from ``SessionLocal`` -- outside
    this test's transaction, where the fixture data does not exist and would
    not be rolled back. Redirecting both onto the test session keeps every
    write inside the transaction the harness discards.

    ``commit`` is neutered rather than allowed through: a commit here would end
    the harness's transaction and leak the fixture into the database.
    """
    import studium.retrieval.service as service_module

    async def read_db(fn):
        return fn(db)

    async def run_db(fn):
        result = fn(db)
        db.flush()
        return result

    monkeypatch.setattr(service_module, "read_db", read_db)
    if write:
        monkeypatch.setattr(service_module, "run_db", run_db)

    import studium.retrieval.embedding_worker as worker_module

    monkeypatch.setattr(worker_module, "run_db", run_db)

    import studium.retrieval as retrieval_module

    monkeypatch.setattr(retrieval_module, "read_db", read_db)

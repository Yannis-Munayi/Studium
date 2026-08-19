"""Tier 1: the ``retrieve_passages`` pipeline and its degradations (§6, §16).

The database bridge is stubbed rather than mocked at the SQL level: what these
tests are about is the orchestration -- which phases run, what happens when a
provider is down, whether the contract holds on every path -- and that is
decided above the queries. The queries themselves are Tier 2's subject.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from studium.retrieval import service
from studium.retrieval.providers import EmbeddingUnavailable, RerankUnavailable
from studium.retrieval.search import Candidate
from studium.retrieval.service import HybridRetriever, _effective_query
from studium.retrieval.types import MAX_K, RetrievalResult

CONCEPT = uuid.UUID("00000000-0000-7000-8000-0000000000aa")
SUBJECT = uuid.UUID("00000000-0000-7000-8000-0000000000bb")


def candidate(index: int, *, reason: str = "curated", chunk_type: str = "body") -> Candidate:
    return Candidate(
        chunk_id=uuid.UUID(f"00000000-0000-7000-8000-0000000{index:05d}"),
        text=f"Passage {index} about beta reduction and substitution.",
        source_id=SUBJECT,
        source_title="An Introduction to Functional Programming",
        source_authors=["Michaelson, Greg"],
        page_start=40 + index,
        page_end=40 + index,
        section_path=["Chapter 3", "3.2 Beta Reduction"],
        chunk_type=chunk_type,
        chunk_index=index,
        reason=reason,
    )


@pytest.fixture
def plan_with(monkeypatch):
    """Stub the database-touching helpers, and let ``read_db`` run the closure.

    The alternative -- faking ``read_db`` and guessing which call site each
    closure came from -- cannot distinguish them: they are all lambdas defined
    in the same method. Stubbing the functions the closures call keeps the
    service's own control flow, including its try/except boundaries, under
    test rather than replaced.
    """

    def install(
        *,
        curated: list[Candidate] | None = None,
        vector: list[Candidate] | None = None,
        keyword: list[Candidate] | None = None,
        siblings: list[Candidate] | None = None,
        plan_fails: bool = False,
        search_fails: bool = False,
        concept_missing: bool = False,
    ):
        plan = service._Plan(
            concept_id=CONCEPT,
            subject_id=SUBJECT,
            stance="default",
            query="Beta Reduction. Substituting an argument into a body.",
            neighborhood=[CONCEPT],
            curated=list(curated or []),
            source_ids=[SUBJECT],
        )

        async def fake_read_db(fn):
            # A None session: every callee below is stubbed, so nothing
            # dereferences it. A stub that got missed raises AttributeError
            # here rather than silently returning empty.
            return fn(None)

        def fake_gather(session, concept_id, stance, query_text):
            if plan_fails:
                raise RuntimeError("planning blew up")
            if concept_missing:
                return None
            return service._Plan(
                concept_id=plan.concept_id,
                subject_id=plan.subject_id,
                stance=stance,
                query=service._effective_query(
                    "Beta Reduction",
                    "Substituting an argument into a body.",
                    query_text,
                ),
                neighborhood=list(plan.neighborhood),
                # Fresh copies per call: fusion mutates fused_score in place,
                # and the caching tests call twice against the same fixture.
                curated=[replace(c, ranks=dict(c.ranks)) for c in plan.curated],
                source_ids=list(plan.source_ids),
            )

        def fake_pools(session, plan_arg, embedding):
            if search_fails:
                raise RuntimeError("search blew up")
            return (list(vector or []), list(keyword or []), embedding is None)

        def fake_siblings(session, selected, *, allowance):
            return list(siblings or [])[:allowance]

        monkeypatch.setattr(service, "read_db", fake_read_db)
        monkeypatch.setattr(service, "_gather", fake_gather)
        monkeypatch.setattr(service, "_search_pools", fake_pools)
        monkeypatch.setattr(service.search, "expand_atomic_siblings", fake_siblings)

    return install


class TestContract:
    """§6: the shape every call returns, on every path."""

    async def test_it_returns_a_retrieval_result(self, plan_with):
        plan_with(curated=[candidate(i) for i in range(4)])
        got = await HybridRetriever(use_cache=False).retrieve_passages(CONCEPT)
        assert isinstance(got, RetrievalResult)
        assert len(got.passages) == 4
        assert got.query_used

    async def test_passages_come_back_in_chunk_id_order(self, plan_with):
        """§3's numbering contract, through the whole pipeline."""
        plan_with(curated=[candidate(i) for i in (5, 1, 3, 2)])
        got = await HybridRetriever(use_cache=False).retrieve_passages(CONCEPT)
        ids = [str(p.chunk_id) for p in got.passages]
        assert ids == sorted(ids)

    async def test_it_never_raises_when_there_is_nothing_to_return(self, plan_with):
        plan_with(curated=[], vector=[], keyword=[])
        got = await HybridRetriever(use_cache=False).retrieve_passages(CONCEPT)
        assert got.passages == []
        assert got.thin_grounding is True
        assert got.thin_grounding_reason

    async def test_a_missing_concept_returns_a_flagged_empty_result(self, plan_with):
        plan_with(concept_missing=True)
        got = await HybridRetriever(use_cache=False).retrieve_passages(CONCEPT)
        assert got.passages == []
        assert got.thin_grounding is True

    async def test_a_planning_failure_degrades_rather_than_raising(self, plan_with):
        plan_with(plan_fails=True)
        got = await HybridRetriever(use_cache=False).retrieve_passages(CONCEPT)
        assert got.passages == []
        assert got.thin_grounding is True
        assert got.degraded is True

    async def test_k_is_respected_and_clamped(self, plan_with):
        plan_with(curated=[candidate(i) for i in range(30)])
        retriever = HybridRetriever(use_cache=False)

        assert len(await _passages(retriever, k=3)) == 3
        assert len(await _passages(retriever, k=500)) <= MAX_K

    async def test_every_passage_carries_provenance(self, plan_with):
        """§3: "There is no valid retrieval result that lacks provenance"."""
        plan_with(curated=[candidate(i) for i in range(3)])
        got = await HybridRetriever(use_cache=False).retrieve_passages(CONCEPT)
        for passage in got.passages:
            assert passage.source_id is not None
            assert passage.source_title
            assert passage.section_path
            assert passage.retrieval_reason in ("curated", "vector", "keyword", "expanded")


async def _passages(retriever: HybridRetriever, **kwargs):
    return (await retriever.retrieve_passages(CONCEPT, **kwargs)).passages


class TestDegradation:
    """§16's table, on the paths a Tier 1 test can reach."""

    async def test_an_embedding_outage_falls_back_to_keyword_only(self, plan_with):
        plan_with(curated=[candidate(1)], keyword=[candidate(2), candidate(3)])

        class DownEmbeddings:
            model_version = "down"

            async def embed(self, texts, *, input_type="document"):
                raise EmbeddingUnavailable("provider down")

        got = await HybridRetriever(
            embeddings=DownEmbeddings(), use_cache=False
        ).retrieve_passages(CONCEPT)

        assert got.degraded is True
        assert len(got.passages) == 3, "keyword hits and curated pointers survive"

    async def test_a_rerank_outage_keeps_the_fused_order(self, plan_with):
        plan_with(curated=[candidate(i) for i in range(5)])

        class DownReranker:
            async def rerank(self, query, documents, *, top_k):
                raise RerankUnavailable("provider down")

        got = await HybridRetriever(
            reranker=DownReranker(), use_cache=False
        ).retrieve_passages(CONCEPT)

        assert got.degraded is True
        assert len(got.passages) == 5

    async def test_a_skipped_rerank_does_not_flood_the_review_queue(self, plan_with):
        """Raw RRF scores are ~0.03. Leaking them into relevance_score would
        trip §13's 0.5 threshold on every call during an outage."""
        plan_with(curated=[candidate(i) for i in range(6)])

        class DownReranker:
            async def rerank(self, query, documents, *, top_k):
                raise RerankUnavailable("provider down")

        got = await HybridRetriever(
            reranker=DownReranker(), use_cache=False
        ).retrieve_passages(CONCEPT)

        assert got.thin_grounding is False, got.thin_grounding_reason
        assert all(p.relevance_score >= 0.5 for p in got.passages)

    async def test_a_search_failure_still_returns_curated_pointers(self, plan_with):
        plan_with(curated=[candidate(i) for i in range(4)], search_fails=True)
        got = await HybridRetriever(use_cache=False).retrieve_passages(CONCEPT)
        assert got.degraded is True
        assert len(got.passages) == 4


class TestCaching:
    """§14."""

    async def test_a_second_identical_call_hits_the_cache(self, plan_with):
        plan_with(curated=[candidate(i) for i in range(4)])
        retriever = HybridRetriever()

        await retriever.retrieve_passages(CONCEPT, stance="formal")
        await retriever.retrieve_passages(CONCEPT, stance="formal")
        assert retriever.cache.hits == 1

    async def test_a_different_stance_misses(self, plan_with):
        plan_with(curated=[candidate(i) for i in range(4)])
        retriever = HybridRetriever()

        await retriever.retrieve_passages(CONCEPT, stance="formal")
        await retriever.retrieve_passages(CONCEPT, stance="applied")
        assert retriever.cache.hits == 0

    async def test_a_degraded_result_is_not_cached(self, plan_with):
        """Caching one would hold a keyword-only answer for five minutes after
        the provider recovered."""
        plan_with(curated=[candidate(1)], search_fails=True)
        retriever = HybridRetriever()

        first = await retriever.retrieve_passages(CONCEPT)
        assert first.degraded is True
        assert retriever.cache.size == 0

    async def test_use_cache_false_bypasses_it_entirely(self, plan_with):
        plan_with(curated=[candidate(i) for i in range(4)])
        retriever = HybridRetriever(use_cache=False)

        await retriever.retrieve_passages(CONCEPT)
        await retriever.retrieve_passages(CONCEPT)
        assert retriever.cache.size == 0


class TestAtomicSiblings:
    """§6: k is a soft ceiling; the hard maximum is k + 3."""

    async def test_a_split_code_block_pulls_in_its_siblings(self, plan_with):
        plan_with(
            curated=[candidate(1, chunk_type="code")],
            siblings=[candidate(2, chunk_type="code"), candidate(3, chunk_type="code")],
        )
        got = await HybridRetriever(use_cache=False).retrieve_passages(CONCEPT, k=1)
        assert len(got.passages) == 3

    async def test_siblings_never_exceed_the_k_plus_three_ceiling(self, plan_with):
        plan_with(
            curated=[candidate(i, chunk_type="code") for i in range(6)],
            siblings=[candidate(50 + i, chunk_type="code") for i in range(10)],
        )
        got = await HybridRetriever(use_cache=False).retrieve_passages(CONCEPT, k=6)
        assert len(got.passages) <= 6 + 3

    async def test_a_sibling_does_not_inflate_the_average_score(self, plan_with):
        """It is returned for completeness, not because it scored, so it must
        not lift the average §13 tests against."""
        plan_with(
            curated=[candidate(1, chunk_type="code")],
            siblings=[candidate(2, chunk_type="code", reason="expanded")],
        )
        got = await HybridRetriever(use_cache=False).retrieve_passages(CONCEPT, k=1)

        siblings = [p for p in got.passages if p.retrieval_reason == "expanded"]
        ranked = [p for p in got.passages if p.retrieval_reason != "expanded"]
        assert siblings and ranked
        assert siblings[0].relevance_score <= min(p.relevance_score for p in ranked)


class TestQueryConstruction:
    """§9 "Query construction"."""

    def test_caller_text_is_anchored_by_the_concept_title(self):
        assert (
            _effective_query("Beta Reduction", "unused", "why does it terminate?")
            == "Beta Reduction: why does it terminate?"
        )

    def test_without_caller_text_the_description_is_used(self):
        assert (
            _effective_query("Beta Reduction", "Substituting an argument.", None)
            == "Beta Reduction. Substituting an argument."
        )

    def test_blank_caller_text_is_treated_as_absent(self):
        assert _effective_query("Beta Reduction", "A rule.", "   ") == "Beta Reduction. A rule."

    def test_a_concept_with_no_description_still_produces_a_query(self):
        assert _effective_query("Beta Reduction", "", None) == "Beta Reduction."

    async def test_the_query_used_is_reported_back(self, plan_with):
        """§13: a reviewer working the queue sees what was actually searched."""
        plan_with(curated=[candidate(1)])
        got = await HybridRetriever(use_cache=False).retrieve_passages(CONCEPT)
        assert got.query_used == "Beta Reduction. Substituting an argument into a body."


class TestThinGroundingReporting:
    async def test_the_reason_names_the_concept_and_the_query(self, plan_with):
        """§13: the row must be readable a month later without re-deriving what
        the retrieval was for."""
        plan_with(curated=[candidate(1)])
        got = await HybridRetriever(use_cache=False).retrieve_passages(CONCEPT)
        assert got.thin_grounding is True
        assert str(CONCEPT) in got.thin_grounding_reason
        assert "Beta Reduction" in got.thin_grounding_reason

    async def test_no_review_row_is_written_without_a_turn_to_attach_it_to(
        self, plan_with, monkeypatch
    ):
        """content_review_queue CHECKs for a target; at grounding time there is
        none. See DIVERGENCES-RETRIEVAL (S2)."""
        plan_with(curated=[candidate(1)])
        called = {"n": 0}

        async def spy(**kwargs):
            called["n"] += 1
            return uuid.uuid4()

        monkeypatch.setattr(service, "flag_thin_grounding", spy)

        got = await HybridRetriever(use_cache=False).retrieve_passages(CONCEPT)
        assert got.thin_grounding is True
        assert got.review_queue_id is None
        assert called["n"] == 0

    async def test_a_turn_id_lets_retrieval_write_the_row_itself(
        self, plan_with, monkeypatch
    ):
        plan_with(curated=[candidate(1)])
        written = uuid.uuid4()

        async def spy(**kwargs):
            return written

        monkeypatch.setattr(service, "flag_thin_grounding", spy)

        got = await HybridRetriever(use_cache=False).retrieve_passages(
            CONCEPT, session_turn_id=uuid.uuid4()
        )
        assert got.review_queue_id == written

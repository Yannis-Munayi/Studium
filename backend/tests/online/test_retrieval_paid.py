"""Tier 3: retrieval against the real Voyage API (retrieval §17).

These spend money and require ``VOYAGE_API_KEY`` plus an explicit opt-in. They
are not in CI, for the same reason the agent runtime's paid tier is not: their
assertions are about *provider behaviour*, which is not a property a commit
changes, so gating merges on them would spend money per branch and produce
flakiness unrelated to the diff.

Run them deliberately:

    STUDIUM_RUN_PAID_TESTS=1 python -m pytest tests/online/test_retrieval_paid.py -m anthropic

The marker is ``anthropic`` rather than a new ``voyage`` marker so the existing
CI deselection (``-m "not anthropic"``) already excludes them. A second paid
marker would need every workflow updated to stay excluded, and the one that got
missed would start billing.
"""

from __future__ import annotations

import os
import time

import pytest

from studium.retrieval.providers import (
    EMBEDDING_DIM,
    VoyageEmbeddings,
    VoyageReranker,
)

pytestmark = pytest.mark.anthropic


@pytest.fixture
def voyage_enabled():
    if os.environ.get("STUDIUM_RUN_PAID_TESTS") != "1":
        pytest.skip("paid tests are opt-in: set STUDIUM_RUN_PAID_TESTS=1")
    if not os.environ.get("VOYAGE_API_KEY"):
        pytest.skip("VOYAGE_API_KEY is not set")
    pytest.importorskip("voyageai", reason="install the 'retrieval' extra")


#: Hand-labelled: the first three genuinely answer the query, the rest do not.
#: §17's "ground-truth relevant chunks", small enough to stay honest.
LABELLED = [
    ("The redex (lambda x. M) N contracts to M with x replaced by N.", True),
    ("Beta reduction is the computational rule of the lambda calculus.", True),
    ("A term with no remaining redex is said to be in normal form.", True),
    ("The Peano axioms characterise the natural numbers by induction.", False),
    ("A Turing machine is a tuple of states, symbols, and a transition.", False),
    ("Photosynthesis converts light energy into chemical energy.", False),
]


class TestRealEmbeddings:
    async def test_a_round_trip_returns_well_formed_vectors(self, voyage_enabled):
        """§17: "asserts the returned vector has the expected 1024 dimensions
        and non-degenerate values"."""
        vectors = await VoyageEmbeddings().embed(
            ["Beta reduction contracts a redex."], input_type="document"
        )

        assert len(vectors) == 1
        vector = vectors[0]
        assert len(vector) == EMBEDDING_DIM
        assert any(v != 0.0 for v in vector), "an all-zero vector is degenerate"
        assert all(-2.0 < v < 2.0 for v in vector)

    async def test_the_dimension_matches_the_schema_column(self, voyage_enabled):
        """A mismatch here means every insert into source_chunk_embeddings
        fails -- worth catching against the real provider, not a stub."""
        from studium.models.corpus import EMBEDDING_DIM as COLUMN_DIM

        [vector] = await VoyageEmbeddings().embed(["x"], input_type="document")
        assert len(vector) == COLUMN_DIM

    async def test_similar_texts_embed_closer_than_unrelated_ones(self, voyage_enabled):
        import math

        texts = [
            "Beta reduction contracts a redex in the lambda calculus.",
            "A redex is contracted by substituting the argument into the body.",
            "Photosynthesis converts light energy into chemical energy.",
        ]
        a, b, c = await VoyageEmbeddings().embed(texts, input_type="document")

        def cosine(x, y):
            dot = sum(i * j for i, j in zip(x, y, strict=True))
            nx = math.sqrt(sum(i * i for i in x))
            ny = math.sqrt(sum(j * j for j in y))
            return dot / (nx * ny)

        assert cosine(a, b) > cosine(a, c)


class TestRealReranking:
    async def test_reranking_surfaces_the_relevant_chunks(self, voyage_enabled):
        """§17: "verify the top-6 by rerank score includes the ground-truth
        relevant chunks"."""
        documents = [text for text, _ in LABELLED]
        relevant = {i for i, (_, is_relevant) in enumerate(LABELLED) if is_relevant}

        scores = await VoyageReranker().rerank(
            "What does a beta redex reduce to?", documents, top_k=3
        )

        assert len(scores) == 3
        top = {index for index, _ in scores}
        assert top == relevant, f"expected the labelled three, got {top}"

    async def test_scores_are_normalised_into_the_range_section_13_assumes(
        self, voyage_enabled
    ):
        """§13's score threshold reads a 0-1 scale. A provider returning logits
        would silently disable that half of thin-grounding detection."""
        scores = await VoyageReranker().rerank(
            "beta reduction", [t for t, _ in LABELLED], top_k=6
        )
        assert all(0.0 <= score <= 1.0 for _, score in scores)

    async def test_relevant_chunks_score_above_the_thin_grounding_floor(
        self, voyage_enabled
    ):
        """A calibration check: if genuinely relevant passages scored under
        0.5, §13 would flag every well-grounded retrieval."""
        from studium.retrieval.types import THIN_GROUNDING_MIN_AVG_SCORE

        scores = await VoyageReranker().rerank(
            "What does a beta redex reduce to?",
            [t for t, _ in LABELLED],
            top_k=3,
        )
        average = sum(score for _, score in scores) / len(scores)
        assert average >= THIN_GROUNDING_MIN_AVG_SCORE, (
            f"the reranker scores good matches at {average:.2f}, below §13's "
            f"{THIN_GROUNDING_MIN_AVG_SCORE} floor -- the threshold needs "
            f"re-baselining against this provider (§18 v1.1 candidate)"
        )


class TestRealEndToEnd:
    @pytest.mark.postgres
    async def test_a_full_retrieval_completes_within_the_latency_budget(
        self, voyage_enabled, db, monkeypatch
    ):
        """§17: "returns non-empty results in under 5 seconds"."""
        import uuid

        from studium.retrieval import HybridRetriever
        from studium.retrieval.embedding_worker import EmbeddingWorker
        from tests.fixtures import lambda_calculus
        from tests.online.test_retrieval_db import _use_test_session

        fixture = lambda_calculus.build(
            db, email=f"paid-{uuid.uuid4().hex[:8]}@example.com"
        )
        lambda_calculus.extend_corpus(db, fixture)
        db.flush()

        _use_test_session(monkeypatch, db, write=True)

        embeddings = VoyageEmbeddings()
        await EmbeddingWorker(embeddings=embeddings, batch_size=64).drain()

        retriever = HybridRetriever(
            embeddings=embeddings, reranker=VoyageReranker(), use_cache=False
        )

        started = time.monotonic()
        result = await retriever.retrieve_passages(
            fixture.concept_id("beta-reduction"),
            stance="formal",
            query_text="what does a redex reduce to?",
        )
        elapsed = time.monotonic() - started

        assert result.passages, "a seeded corpus must ground its own concept"
        assert result.degraded is False
        assert elapsed < 5.0, f"retrieval took {elapsed:.1f}s"

        ids = [str(p.chunk_id) for p in result.passages]
        assert ids == sorted(ids), "§3's numbering contract holds on the real path"

"""Tier 1: providers, degradation, and the result cache (§8, §14, §16)."""

from __future__ import annotations

import uuid

import pytest

from studium.retrieval.cache import CacheKey, ResultCache
from studium.retrieval.providers import (
    BACKOFF_SECONDS,
    BREAKER_RESET_SECONDS,
    BREAKER_THRESHOLD,
    EMBEDDING_DIM,
    MAX_ATTEMPTS,
    MAX_BATCH,
    CircuitBreaker,
    IdentityReranker,
    StubEmbeddings,
    with_backoff,
)
from studium.retrieval.types import Passage, RetrievalResult

CONCEPT = uuid.UUID("00000000-0000-7000-8000-0000000000aa")
OTHER = uuid.UUID("00000000-0000-7000-8000-0000000000bb")


class FakeClock:
    """A clock the tests advance by hand, so no test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def result(*, passages: int = 3) -> RetrievalResult:
    return RetrievalResult(
        passages=[
            Passage(
                chunk_id=uuid.UUID(f"00000000-0000-7000-8000-00000000000{i}"),
                text=f"p{i}",
                relevance_score=0.9,
            )
            for i in range(passages)
        ],
        query_used="a query",
    )


class TestCircuitBreaker:
    """§16: three consecutive failures opens it for 60 seconds."""

    def test_it_opens_after_three_consecutive_failures(self):
        breaker = CircuitBreaker("test", clock=FakeClock())
        for _ in range(BREAKER_THRESHOLD - 1):
            breaker.record_failure()
            assert breaker.is_open is False
        breaker.record_failure()
        assert breaker.is_open is True

    def test_a_success_resets_the_run(self):
        breaker = CircuitBreaker("test", clock=FakeClock())
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        assert breaker.is_open is False, "the run must be consecutive"

    def test_it_closes_after_the_cooldown(self):
        clock = FakeClock()
        breaker = CircuitBreaker("test", clock=clock)
        for _ in range(BREAKER_THRESHOLD):
            breaker.record_failure()
        assert breaker.is_open is True

        clock.advance(BREAKER_RESET_SECONDS - 1)
        assert breaker.is_open is True, "it must not reopen early"

        clock.advance(2)
        assert breaker.is_open is False

    def test_one_probe_failure_reopens_it(self):
        """Half-open: the first call after cooldown decides, and it should not
        need three more failures to re-open on a still-broken provider."""
        clock = FakeClock()
        breaker = CircuitBreaker("test", clock=clock)
        for _ in range(BREAKER_THRESHOLD):
            breaker.record_failure()
        clock.advance(BREAKER_RESET_SECONDS + 1)
        assert breaker.is_open is False

        for _ in range(BREAKER_THRESHOLD):
            breaker.record_failure()
        assert breaker.is_open is True

    def test_breakers_are_independent(self):
        """An embedding outage must not take out a healthy reranker."""
        embed = CircuitBreaker("embed", clock=FakeClock())
        rerank = CircuitBreaker("rerank", clock=FakeClock())
        for _ in range(BREAKER_THRESHOLD):
            embed.record_failure()
        assert embed.is_open is True
        assert rerank.is_open is False


class TestBackoff:
    """§8: 1s, 4s, 16s, 64s, capped at four attempts."""

    def test_the_ladder_is_the_documented_one(self):
        assert BACKOFF_SECONDS == (1.0, 4.0, 16.0, 64.0)
        assert MAX_ATTEMPTS == 4

    async def test_it_retries_then_succeeds(self):
        attempts = {"n": 0}
        slept: list[float] = []

        async def flaky() -> str:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("transient")
            return "ok"

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        got = await with_backoff(flaky, what="test", sleep=sleep)
        assert got == "ok"
        assert attempts["n"] == 3
        assert slept == [1.0, 4.0]

    async def test_it_gives_up_after_four_attempts(self):
        attempts = {"n": 0}

        async def always_fails() -> str:
            attempts["n"] += 1
            raise RuntimeError("down")

        async def sleep(seconds: float) -> None:
            return None

        with pytest.raises(RuntimeError, match="down"):
            await with_backoff(always_fails, what="test", sleep=sleep)
        assert attempts["n"] == MAX_ATTEMPTS

    async def test_it_feeds_the_breaker(self):
        breaker = CircuitBreaker("test", clock=FakeClock())

        async def always_fails() -> str:
            raise RuntimeError("down")

        async def sleep(seconds: float) -> None:
            return None

        with pytest.raises(RuntimeError):
            await with_backoff(always_fails, what="test", breaker=breaker, sleep=sleep)
        assert breaker.is_open is True


class TestStubProviders:
    async def test_stub_embeddings_are_the_right_shape(self):
        vectors = await StubEmbeddings().embed(["one", "two"])
        assert len(vectors) == 2
        assert all(len(v) == EMBEDDING_DIM for v in vectors)

    async def test_stub_embeddings_are_deterministic(self):
        first = await StubEmbeddings().embed(["beta reduction"])
        second = await StubEmbeddings().embed(["beta reduction"])
        assert first == second

    async def test_different_texts_embed_differently(self):
        [a], [b] = (
            await StubEmbeddings().embed(["alpha"]),
            await StubEmbeddings().embed(["omega"]),
        )
        assert a != b

    async def test_the_identity_reranker_preserves_order_and_scores(self):
        scores = await IdentityReranker().rerank("q", ["a", "b", "c"], top_k=2)
        assert [i for i, _ in scores] == [0, 1]
        assert all(0.0 <= s <= 1.0 for _, s in scores)

    async def test_an_empty_batch_is_not_an_error(self):
        assert await StubEmbeddings().embed([]) == []
        assert await IdentityReranker().rerank("q", [], top_k=6) == []

    def test_the_batch_ceiling_is_voyages(self):
        assert MAX_BATCH == 128


class TestResultCache:
    """§14: five minutes, keyed on concept, stance, and query hash."""

    def test_a_hit_returns_the_stored_result(self):
        cache = ResultCache(clock=FakeClock())
        key = CacheKey.build(CONCEPT, "formal", None)
        cache.put(key, result())
        got = cache.get(key)
        assert got is not None
        assert len(got.passages) == 3
        assert cache.hits == 1

    def test_a_miss_returns_none(self):
        cache = ResultCache(clock=FakeClock())
        assert cache.get(CacheKey.build(CONCEPT, "formal", None)) is None
        assert cache.misses == 1

    def test_entries_expire_after_the_ttl(self):
        clock = FakeClock()
        cache = ResultCache(clock=clock)
        key = CacheKey.build(CONCEPT, "formal", None)
        cache.put(key, result())

        clock.advance(299)
        assert cache.get(key) is not None

        clock.advance(2)
        assert cache.get(key) is None

    def test_stance_is_part_of_the_key(self):
        """§11 makes ranking stance-specific, so sharing across stances would
        silently serve the wrong passages."""
        cache = ResultCache(clock=FakeClock())
        cache.put(CacheKey.build(CONCEPT, "formal", None), result())
        assert cache.get(CacheKey.build(CONCEPT, "intuitive", None)) is None

    def test_query_text_is_part_of_the_key(self):
        cache = ResultCache(clock=FakeClock())
        cache.put(CacheKey.build(CONCEPT, "formal", "why does it terminate?"), result())
        assert cache.get(CacheKey.build(CONCEPT, "formal", "what is a redex?")) is None

    def test_the_query_text_is_hashed_not_stored(self):
        """A learner's question can carry personal detail, and a cache key is
        exactly what ends up in a debug log."""
        key = CacheKey.build(CONCEPT, "formal", "my name is Yannis and I am confused")
        assert "Yannis" not in repr(key)
        assert len(key.query_hash) == 16

    def test_a_mutation_of_a_returned_result_does_not_leak_back(self):
        cache = ResultCache(clock=FakeClock())
        key = CacheKey.build(CONCEPT, "formal", None)
        cache.put(key, result())

        first = cache.get(key)
        first.review_queue_id = uuid.uuid4()
        first.passages.clear()

        second = cache.get(key)
        assert second.review_queue_id is None
        assert len(second.passages) == 3

    def test_invalidate_drops_only_that_concept(self):
        """A reviewer adding concept_sources rows should see the effect now,
        not in five minutes."""
        cache = ResultCache(clock=FakeClock())
        cache.put(CacheKey.build(CONCEPT, "formal", None), result())
        cache.put(CacheKey.build(CONCEPT, "applied", None), result())
        cache.put(CacheKey.build(OTHER, "formal", None), result())

        assert cache.invalidate(CONCEPT) == 2
        assert cache.get(CacheKey.build(OTHER, "formal", None)) is not None

    def test_it_evicts_rather_than_growing_without_bound(self):
        cache = ResultCache(clock=FakeClock(), max_entries=3)
        for i in range(6):
            cache.put(
                CacheKey.build(uuid.UUID(f"00000000-0000-7000-8000-00000000{i:04d}"), "d", None),
                result(),
            )
            cache.clock.advance(1)
        assert cache.size <= 3

    def test_hit_rate_reports_zero_before_any_lookup(self):
        assert ResultCache(clock=FakeClock()).hit_rate == 0.0

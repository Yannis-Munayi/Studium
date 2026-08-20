"""Tier 1: cache pre-warming (retrieval §14).

The loop is driven directly rather than left to run on its schedule -- a test
that waited out a 5-minute interval would be a test nobody runs.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from studium.retrieval import warming
from studium.retrieval.types import RetrievalResult
from studium.retrieval.warming import (
    MAX_CONCEPTS_PER_CYCLE,
    WARM_INTERVAL_SECONDS,
    WARM_STANCE,
    CacheWarmer,
)

CONCEPTS = [uuid.UUID(f"00000000-0000-7000-8000-00000000{i:04d}") for i in range(6)]


class RecordingRetriever:
    """Records what it was asked for, and can be told to fail or degrade."""

    def __init__(self, *, fail_on=(), degrade_on=()) -> None:
        self.calls: list[tuple[uuid.UUID, str, int]] = []
        self._fail = set(fail_on)
        self._degrade = set(degrade_on)

    async def retrieve_passages(self, concept_id, *, stance="default", k=6, **kw):
        self.calls.append((concept_id, stance, k))
        if concept_id in self._fail:
            raise RuntimeError("provider down")
        return RetrievalResult(degraded=concept_id in self._degrade)


@pytest.fixture
def no_stagger(monkeypatch):
    """Warming staggers its calls by half a second; tests should not wait."""
    return {"stagger": 0.0}


@pytest.fixture
def concepts(monkeypatch):
    def install(found: list[uuid.UUID] | Exception):
        async def fake_read_db(fn):
            if isinstance(found, Exception):
                raise found
            return list(found)

        monkeypatch.setattr(warming, "read_db", fake_read_db)

    return install


class TestWarmOnce:
    async def test_it_warms_every_load_bearing_concept(self, concepts, no_stagger):
        concepts(CONCEPTS[:4])
        retriever = RecordingRetriever()

        report = await CacheWarmer(retriever=retriever, **no_stagger).warm_once()

        assert report.warmed == 4
        assert report.failed == 0
        assert [c for c, _, _ in retriever.calls] == CONCEPTS[:4]

    async def test_it_warms_with_the_shape_a_first_segment_uses(
        self, concepts, no_stagger
    ):
        """§14: retrieve_passages(concept_id, 'default', 6)."""
        concepts(CONCEPTS[:1])
        retriever = RecordingRetriever()

        await CacheWarmer(retriever=retriever, **no_stagger).warm_once()

        [(_, stance, k)] = retriever.calls
        assert stance == WARM_STANCE == "default"
        assert k == 6

    async def test_one_failing_concept_does_not_stop_the_cycle(
        self, concepts, no_stagger
    ):
        concepts(CONCEPTS[:4])
        retriever = RecordingRetriever(fail_on={CONCEPTS[1]})

        report = await CacheWarmer(retriever=retriever, **no_stagger).warm_once()

        assert report.warmed == 3
        assert report.failed == 1
        assert len(retriever.calls) == 4, "it kept going past the failure"

    async def test_a_degraded_result_counts_as_skipped_not_warmed(
        self, concepts, no_stagger
    ):
        """The retriever does not cache degraded results (S10), so warming one
        achieved nothing and must not be reported as a success."""
        concepts(CONCEPTS[:3])
        retriever = RecordingRetriever(degrade_on={CONCEPTS[0], CONCEPTS[2]})

        report = await CacheWarmer(retriever=retriever, **no_stagger).warm_once()

        assert report.warmed == 1
        assert report.skipped == 2
        assert report.failed == 0

    async def test_an_empty_concept_list_is_not_an_error(self, concepts, no_stagger):
        concepts([])
        report = await CacheWarmer(retriever=RecordingRetriever(), **no_stagger).warm_once()
        assert report.attempted == 0

    async def test_a_database_failure_is_swallowed(self, concepts, no_stagger):
        """Warming is an optimisation; it must never propagate."""
        concepts(RuntimeError("database unreachable"))
        report = await CacheWarmer(retriever=RecordingRetriever(), **no_stagger).warm_once()
        assert report.attempted == 0

    async def test_the_per_cycle_cap_bounds_the_bill(self, concepts, no_stagger):
        """§15 prices warming per call; an uncapped loop discovers the cost of a
        large corpus the expensive way."""
        many = [uuid.uuid4() for _ in range(MAX_CONCEPTS_PER_CYCLE + 20)]
        concepts(many)
        retriever = RecordingRetriever()

        report = await CacheWarmer(retriever=retriever, **no_stagger).warm_once()

        assert report.warmed == MAX_CONCEPTS_PER_CYCLE
        assert len(retriever.calls) == MAX_CONCEPTS_PER_CYCLE


class TestSchedule:
    def test_the_interval_matches_the_cache_ttl(self):
        """§14 refreshes on a rolling 5-minute schedule against a 5-minute TTL.
        A longer interval means entries expire before they are replaced."""
        from studium.retrieval.cache import TTL_SECONDS

        assert WARM_INTERVAL_SECONDS == 300.0
        assert WARM_INTERVAL_SECONDS <= TTL_SECONDS

    async def test_the_loop_runs_repeatedly_and_stops_on_cancel(
        self, concepts, no_stagger
    ):
        concepts(CONCEPTS[:2])
        retriever = RecordingRetriever()
        warmer = CacheWarmer(retriever=retriever, interval=0.01, **no_stagger)

        warmer.start()
        assert warmer.running
        for _ in range(200):
            await asyncio.sleep(0.005)
            if warmer.cycles >= 3:
                break
        await warmer.stop()

        assert warmer.cycles >= 3, f"only {warmer.cycles} cycles ran"
        assert not warmer.running

    async def test_start_is_idempotent(self, concepts, no_stagger):
        concepts([])
        warmer = CacheWarmer(retriever=RecordingRetriever(), interval=60, **no_stagger)
        try:
            assert warmer.start() is warmer.start()
        finally:
            await warmer.stop()

    async def test_stopping_a_warmer_that_never_started_is_a_no_op(self):
        await CacheWarmer(retriever=RecordingRetriever()).stop()


class TestStatus:
    async def test_status_reports_the_last_cycle(self, concepts, no_stagger):
        concepts(CONCEPTS[:3])
        warmer = CacheWarmer(retriever=RecordingRetriever(), **no_stagger)

        assert warmer.status()["last_cycle"] is None

        await warmer.warm_once()
        status = warmer.status()

        assert status["cycles"] == 1
        assert status["total_warmed"] == 3
        assert status["last_cycle"]["warmed"] == 3
        assert status["running"] is False


class TestAppWiring:
    def test_warming_is_off_unless_explicitly_enabled(self, monkeypatch):
        """A test run or a developer poking one endpoint should not start
        billing a reranker on a timer."""
        from fastapi.testclient import TestClient

        from studium.api.app import WARM_CACHE_ENV, app

        monkeypatch.delenv(WARM_CACHE_ENV, raising=False)
        with TestClient(app) as client:
            body = client.get("/health").json()

        assert body["status"] == "ok"
        assert body["cache_warming"]["running"] is False

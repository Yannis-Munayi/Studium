"""Retrieval result caching and pre-warming (retrieval §14).

A retrieval call's latency is dominated by the reranker, which is a network
round trip inside a turn the learner is waiting on. Two forms of
pre-computation address that: caching what was just computed, and computing
ahead of time what is most likely to be asked for.

**The cache is per-process, not distributed.** Retrieval is a library call from
the agent runtime, which lives in the same process (§4), so a process-local
dict is the whole cache with no serialisation and no network. That stops being
true the moment Studium runs on more than one node, at which point §14 hands
the problem to Infrastructure (subsystem 7) and a Redis-backed implementation
of the same interface replaces this one.

**The key includes the stance.** §11 makes ranking stance-specific, so caching
on concept alone would serve a formal retrieval's passages to an intuitive
request -- a silent quality regression rather than a visible bug.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from .types import RetrievalResult

log = logging.getLogger(__name__)

#: §14: "cached in memory for 5 minutes".
TTL_SECONDS = 300.0

#: A ceiling so a long-running process cannot grow the cache without bound.
#: §14 does not name one; at MVP scale (one subject, ~11 concepts, 5 stances)
#: the working set is under 60 entries, so this is headroom rather than a
#: constraint anything realistic runs into.
MAX_ENTRIES = 512


@dataclass(frozen=True, slots=True)
class CacheKey:
    """§14: ``(concept_id, stance, query_text_hash)``."""

    concept_id: uuid.UUID
    stance: str
    query_hash: str

    @classmethod
    def build(
        cls, concept_id: uuid.UUID, stance: str, query_text: str | None
    ) -> CacheKey:
        # The query text is hashed rather than stored: a learner's free-form
        # question can be long and can carry personal detail, and a cache key
        # is exactly the kind of place that ends up in a debug log.
        digest = hashlib.sha256((query_text or "").encode("utf-8")).hexdigest()[:16]
        return cls(concept_id=concept_id, stance=stance, query_hash=digest)


@dataclass(slots=True)
class _Entry:
    result: RetrievalResult
    stored_at: float


@dataclass
class ResultCache:
    """Five-minute per-process cache of ``RetrievalResult`` (§14).

    A hit skips the entire pipeline -- neighbourhood, both searches, fusion,
    reranking. That is the point: the saving is the whole call, not a piece
    of it.

    The clock is injected so the Tier 1 tests can prove expiry without
    sleeping through five minutes.
    """

    ttl: float = TTL_SECONDS
    max_entries: int = MAX_ENTRIES
    clock: Callable[[], float] = time.monotonic
    _entries: dict[CacheKey, _Entry] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def get(self, key: CacheKey) -> RetrievalResult | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        if self.clock() - entry.stored_at >= self.ttl:
            del self._entries[key]
            self.misses += 1
            return None
        self.hits += 1
        # A defensive copy: the caller gets a result it may annotate (the
        # Lecturer sets review_queue_id on its own copy), and a mutation
        # leaking back into the cache would serve one turn's queue id to the
        # next turn that hits the same key.
        return entry.result.model_copy(deep=True)

    def put(self, key: CacheKey, result: RetrievalResult) -> None:
        if len(self._entries) >= self.max_entries:
            self._evict_oldest()
        self._entries[key] = _Entry(
            result=result.model_copy(deep=True), stored_at=self.clock()
        )

    def invalidate(self, concept_id: uuid.UUID) -> int:
        """Drop every entry for a concept.

        Called when a concept's curated pointers change: a reviewer working the
        thin-grounding queue adds ``concept_sources`` rows precisely so the next
        retrieval is better, and a five-minute stale cache would hide the fix
        they just made.
        """
        doomed = [k for k in self._entries if k.concept_id == concept_id]
        for key in doomed:
            del self._entries[key]
        return len(doomed)

    def clear(self) -> None:
        self._entries.clear()

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def _evict_oldest(self) -> None:
        oldest = min(self._entries.items(), key=lambda kv: kv[1].stored_at)
        del self._entries[oldest[0]]


def load_bearing_concepts(session: object) -> list[uuid.UUID]:
    """Concepts §14 pre-warms: load-bearing, in an active subject.

    ``is_load_bearing`` is the author's marker for "most other concepts depend
    on this", which is the same population most likely to be the first thing a
    learner touches in a session -- so it is both the cheapest set to warm and
    the one where a cold start is most likely to be felt.
    """
    from sqlalchemy import text as sql

    rows = session.execute(  # type: ignore[attr-defined]
        sql(
            """
            SELECT c.id
              FROM concepts c
              JOIN subjects s ON s.id = c.subject_id
             WHERE c.is_load_bearing
               AND s.deleted_at IS NULL
               AND s.status = 'active'
             ORDER BY c.subject_id, c.position
            """
        )
    ).all()
    return [r.id for r in rows]

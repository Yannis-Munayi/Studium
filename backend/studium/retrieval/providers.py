"""Embedding and reranking providers (retrieval §4, §8, §16).

Both sit behind protocols so the implementation can change without touching
anything downstream -- §3 puts "learned scoring" behind interfaces precisely
because relevance functions improve and chunking does not. §19's open question
4 expects Voyage to be reconsidered; the point of this module is that the
reconsideration is a one-line change here.

Two behaviours are policy rather than plumbing:

* **Retry with backoff** (§8): four attempts at 1s, 4s, 16s, 64s. That is a
  longer ladder than the agent runtime's §21 policy, and deliberately so --
  those retries happen inside a turn a learner is watching, while these happen
  in a background embedding worker where a 64-second wait costs nobody
  anything and saves re-queuing the batch.
* **A circuit breaker** (§16): three consecutive failures opens it for 60
  seconds. Without it, a Voyage outage turns every retrieval call into four
  slow failures, and a session-blocking chain of retries is a worse outage than
  the one that caused it. Open, the modality is skipped and hybrid search
  degrades to its other half.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: §4. 1024 dimensions, matching the ``vector(1024)`` column in data layer §6.3.
EMBEDDING_MODEL = "voyage-3"
EMBEDDING_DIM = 1024
RERANK_MODEL = "rerank-2"

#: §8. Voyage's per-call ceiling.
MAX_BATCH = 128

#: §8: "exponential backoff (1s, 4s, 16s, 64s), capped at four attempts".
BACKOFF_SECONDS: tuple[float, ...] = (1.0, 4.0, 16.0, 64.0)
MAX_ATTEMPTS = 4

#: §16. Consecutive failures before the breaker opens, and how long it stays.
BREAKER_THRESHOLD = 3
BREAKER_RESET_SECONDS = 60.0

#: §15 cost model. Voyage-3 input pricing, USD per token.
EMBEDDING_USD_PER_TOKEN = 0.06 / 1_000_000
#: §15: "Reranking 20 candidates against a 300-token query: ~$0.001". Priced
#: per call rather than per token because that is the shape §15 gives it; a
#: per-token rerank price would be a number this spec does not state.
RERANK_USD_PER_CALL = 0.001


class EmbeddingUnavailable(RuntimeError):
    """The embedding modality is unusable right now (§16).

    Distinct from a hard error: hybrid search catches this and falls back to
    keyword-only rather than failing the retrieval. A retrieval that returns
    keyword hits is worth much more to the learner than one that returns
    nothing because half its machinery was down.
    """


class RerankUnavailable(RuntimeError):
    """Reranking is unusable right now (§16).

    Caught by the retrieval service, which returns the fused hybrid-search
    order instead. Fusion ordering is worse than reranked ordering but it is
    still a real ranking, so the degradation is graceful.
    """


class EmbeddingProvider(Protocol):
    """§4. Alternatives (Cohere, OpenAI) plug in here; MVP uses Voyage."""

    model_version: str

    async def embed(
        self, texts: Sequence[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        """Embed a batch. Returns one vector per input, in input order."""
        ...


class Reranker(Protocol):
    """§4. Voyage rerank-2 at MVP; Cohere Rerank 3 is the named fallback."""

    async def rerank(
        self, query: str, documents: Sequence[str], *, top_k: int
    ) -> list[tuple[int, float]]:
        """Return ``(original_index, normalised_score)`` best-first."""
        ...


@dataclass(slots=True)
class CircuitBreaker:
    """§16's breaker. One per modality, so an embedding outage does not open
    the rerank breaker and take out a modality that is still healthy.

    Time is injected rather than read from ``time.monotonic`` directly so the
    Tier 1 tests can drive the 60-second window without sleeping through it.
    """

    name: str
    threshold: int = BREAKER_THRESHOLD
    reset_seconds: float = BREAKER_RESET_SECONDS
    clock: Callable[[], float] = time.monotonic
    consecutive_failures: int = 0
    opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if self.clock() - self.opened_at >= self.reset_seconds:
            # Half-open: the next call is allowed through and decides. Resetting
            # the counter here rather than on success means one probe failure
            # re-opens immediately instead of needing three more.
            self.opened_at = None
            self.consecutive_failures = 0
            log.info("circuit breaker %s closed after cooldown", self.name)
            return False
        return True

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold and self.opened_at is None:
            self.opened_at = self.clock()
            log.warning(
                "circuit breaker %s opened after %d consecutive failures; "
                "skipping this modality for %.0fs",
                self.name, self.consecutive_failures, self.reset_seconds,
            )


async def with_backoff[T](
    operation: Callable[[], Awaitable[T]],
    *,
    what: str,
    breaker: CircuitBreaker | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run ``operation`` under §8's retry ladder and §16's breaker.

    Raises the last exception when attempts run out. Callers map that to
    :class:`EmbeddingUnavailable` or :class:`RerankUnavailable` so the search
    layer handles one exception type per modality rather than a provider SDK's
    whole hierarchy.
    """
    last: BaseException | None = None

    for attempt in range(MAX_ATTEMPTS):
        try:
            result = await operation()
        except Exception as exc:  # noqa: BLE001 -- re-raised below
            last = exc
            if breaker is not None:
                breaker.record_failure()
            if attempt == MAX_ATTEMPTS - 1:
                log.warning("%s failed on attempt %d/%d; giving up", what, attempt + 1, MAX_ATTEMPTS)
                break
            delay = BACKOFF_SECONDS[attempt]
            log.info(
                "%s failed on attempt %d/%d (%s); retrying in %.0fs",
                what, attempt + 1, MAX_ATTEMPTS, type(exc).__name__, delay,
            )
            await sleep(delay)
        else:
            if breaker is not None:
                breaker.record_success()
            return result

    assert last is not None  # noqa: S101 -- the loop only breaks after a failure
    raise last


@dataclass
class VoyageEmbeddings:
    """Voyage-3 via the ``voyageai`` SDK (§4).

    The SDK is imported lazily and the client built on first use, so importing
    this module -- which the whole retrieval package does -- never requires the
    package to be installed. Tier 1 and Tier 2 run without it.
    """

    model: str = EMBEDDING_MODEL
    model_version: str = "voyage-3"
    breaker: CircuitBreaker = field(
        default_factory=lambda: CircuitBreaker("voyage-embed")
    )
    _client: Any = None

    def client(self) -> Any:
        if self._client is None:
            try:
                import voyageai
            except ImportError as exc:  # pragma: no cover -- import-time guard
                raise EmbeddingUnavailable(
                    "voyageai is not installed; install the 'retrieval' extra"
                ) from exc
            self._client = voyageai.AsyncClient()
        return self._client

    async def embed(
        self, texts: Sequence[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > MAX_BATCH:
            raise ValueError(f"batch of {len(texts)} exceeds Voyage's limit of {MAX_BATCH}")
        if self.breaker.is_open:
            raise EmbeddingUnavailable(f"{self.breaker.name} breaker is open")

        async def call() -> Any:
            return await self.client().embed(
                list(texts), model=self.model, input_type=input_type
            )

        try:
            response = await with_backoff(
                call, what="voyage.embed", breaker=self.breaker
            )
        except Exception as exc:  # noqa: BLE001 -- narrowed to the modality type
            raise EmbeddingUnavailable(str(exc)) from exc

        vectors = list(response.embeddings)
        if len(vectors) != len(texts):
            # A short batch would misalign vectors with chunks and attach every
            # embedding after the gap to the wrong text -- silently, since both
            # are lists of floats. Better to fail the batch.
            raise EmbeddingUnavailable(
                f"provider returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        return vectors

    @staticmethod
    def cost_usd(total_tokens: int) -> float:
        """§15: Voyage-3 at $0.06 per 1M input tokens."""
        return total_tokens * EMBEDDING_USD_PER_TOKEN


@dataclass
class VoyageReranker:
    """Voyage rerank-2 (§4). Same lazy-client shape as the embedder."""

    model: str = RERANK_MODEL
    breaker: CircuitBreaker = field(
        default_factory=lambda: CircuitBreaker("voyage-rerank")
    )
    _client: Any = None

    def client(self) -> Any:
        if self._client is None:
            try:
                import voyageai
            except ImportError as exc:  # pragma: no cover -- import-time guard
                raise RerankUnavailable(
                    "voyageai is not installed; install the 'retrieval' extra"
                ) from exc
            self._client = voyageai.AsyncClient()
        return self._client

    async def rerank(
        self, query: str, documents: Sequence[str], *, top_k: int
    ) -> list[tuple[int, float]]:
        if not documents:
            return []
        if self.breaker.is_open:
            raise RerankUnavailable(f"{self.breaker.name} breaker is open")

        async def call() -> Any:
            return await self.client().rerank(
                query=query,
                documents=list(documents),
                model=self.model,
                top_k=min(top_k, len(documents)),
            )

        try:
            # §16 gives reranking one retry at 2s, not the embedder's four-step
            # ladder: this call sits inside a learner-facing turn, and skipping
            # the reranker costs ordering quality while waiting costs the
            # learner a visible stall.
            response = await with_backoff(
                _limited(call, attempts=2), what="voyage.rerank", breaker=self.breaker
            )
        except Exception as exc:  # noqa: BLE001 -- narrowed to the modality type
            raise RerankUnavailable(str(exc)) from exc

        return [
            (int(r.index), _normalise(float(r.relevance_score)))
            for r in response.results
        ]

    @staticmethod
    def cost_usd() -> float:
        """§15: ~$0.001 per rerank call over up to 20 candidates."""
        return RERANK_USD_PER_CALL


def _limited[T](
    operation: Callable[[], Awaitable[T]], *, attempts: int
) -> Callable[[], Awaitable[T]]:
    """Wrap an operation so :func:`with_backoff` stops after ``attempts``.

    Implemented by counting here rather than parameterising ``with_backoff``
    because the backoff *ladder* is what §8 fixes; the attempt count is what
    §16 varies per modality.
    """
    state = {"calls": 0}

    async def limited() -> T:
        state["calls"] += 1
        if state["calls"] > attempts:
            raise RerankUnavailable(f"exhausted {attempts} attempt(s)")
        return await operation()

    return limited


def _normalise(score: float) -> float:
    """Clamp a provider score into the 0.0-1.0 §13 assumes.

    §13's score threshold reads ``relevance_score`` "on the normalized 0-1
    scale from the reranker". Voyage returns that range already; the clamp is
    here so a provider swap that returns logits cannot silently disable the
    thin-grounding score condition by making every average exceed 0.5.
    """
    return max(0.0, min(1.0, score))


@dataclass
class StubEmbeddings:
    """Deterministic embeddings for Tier 1 and Tier 2 (no API, no key).

    Hashes text into a unit vector. Two identical texts embed identically and
    two different ones almost never collide, which is all the offline tests
    need: they assert ordering and plumbing, not semantic quality.
    """

    model_version: str = "stub-1"
    dim: int = EMBEDDING_DIM

    async def embed(
        self, texts: Sequence[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        return [self.vector(t) for t in texts]

    def vector(self, text: str) -> list[float]:
        import hashlib
        import math

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [
            (digest[i % len(digest)] ^ (i * 31 % 251)) / 255.0 - 0.5
            for i in range(self.dim)
        ]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [v / norm for v in raw]


@dataclass
class IdentityReranker:
    """A reranker that preserves fusion order (Tier 1 and Tier 2).

    Scores decay linearly from 1.0 so ``relevance_score`` is populated and
    §13's average-score condition is exercisable without an API key. It is not
    a quality baseline -- §19's open question 3 asks whether real reranking
    beats fusion, and this cannot answer that.
    """

    async def rerank(
        self, query: str, documents: Sequence[str], *, top_k: int
    ) -> list[tuple[int, float]]:
        count = min(top_k, len(documents))
        if count == 0:
            return []
        return [(i, 1.0 - (i / max(count, 2)) * 0.5) for i in range(count)]

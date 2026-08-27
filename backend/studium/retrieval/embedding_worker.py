"""The background embedding worker (retrieval §8).

Chunks arrive from ingestion unembedded. This worker finds them, batches them
to the provider, and writes the vectors. It runs off ``ingestion_jobs`` rows
with ``kind = 'embed'``, in the same process at MVP scale.

Three decisions worth knowing before changing anything here:

* **A missing embedding is not an error.** A chunk without a vector is simply
  invisible to vector search until it has one, and hybrid search's keyword half
  still finds it. That is what makes a large ingestion usable while it is still
  embedding, rather than after.
* **Load-bearing material jumps the queue.** FIFO otherwise. The first learner
  to touch a newly ingested concept should not wait for the whole corpus.
* **A failing batch is retried once without the failure, then flagged.** One
  over-long chunk should not cost 127 good ones their embeddings, and it should
  not be retried forever either -- it goes to the review queue at severity 3
  and the worker moves on.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.asyncdb import run_db

from .providers import MAX_BATCH, EmbeddingProvider, EmbeddingUnavailable, StubEmbeddings
from .search import _as_vector

log = logging.getLogger(__name__)

#: §8. Severity for a chunk that could not be embedded after the retry.
FAILED_CHUNK_SEVERITY = 3


@dataclass(frozen=True, slots=True)
class PendingChunk:
    chunk_id: uuid.UUID
    text: str
    token_count: int


@dataclass(frozen=True, slots=True)
class BatchReport:
    """What one batch did. Returned so a caller can drive the worker in a loop."""

    embedded: int = 0
    failed: int = 0
    tokens: int = 0
    cost_usd: float = 0.0

    @property
    def did_work(self) -> bool:
        return bool(self.embedded or self.failed)


@dataclass
class EmbeddingWorker:
    """Embeds unembedded ``source_chunks`` in provider-sized batches (§8)."""

    embeddings: EmbeddingProvider = field(default_factory=StubEmbeddings)
    batch_size: int = MAX_BATCH

    async def run_once(self, *, source_id: uuid.UUID | None = None) -> BatchReport:
        """Embed one batch. Returns an empty report when there is nothing to do."""
        pending = await run_db(
            lambda s: _claim_batch(s, min(self.batch_size, MAX_BATCH), source_id)
        )
        if not pending:
            return BatchReport()
        return await self._embed_batch(pending)

    async def drain(
        self, *, source_id: uuid.UUID | None = None, max_batches: int = 1000
    ) -> BatchReport:
        """Keep embedding until nothing is left.

        ``max_batches`` bounds the loop: a bug that stopped marking chunks as
        embedded would otherwise spin against the provider indefinitely, which
        is the one failure mode here that costs real money per iteration.
        """
        totals = BatchReport()
        for _ in range(max_batches):
            report = await self.run_once(source_id=source_id)
            if not report.did_work:
                break
            totals = BatchReport(
                embedded=totals.embedded + report.embedded,
                failed=totals.failed + report.failed,
                tokens=totals.tokens + report.tokens,
                cost_usd=totals.cost_usd + report.cost_usd,
            )
        return totals

    async def _embed_batch(self, pending: list[PendingChunk]) -> BatchReport:
        try:
            vectors = await self.embeddings.embed(
                [c.text for c in pending], input_type="document"
            )
        except EmbeddingUnavailable as exc:
            log.warning("batch of %d failed (%s); retrying without stragglers", len(pending), exc)
            return await self._retry_without_failures(pending)

        tokens = sum(c.token_count for c in pending)
        await run_db(
            lambda s: _write_vectors(
                s, pending, vectors, self.embeddings.model_version
            )
        )
        return BatchReport(
            embedded=len(pending),
            tokens=tokens,
            cost_usd=_embedding_cost(tokens),
        )

    async def _retry_without_failures(self, pending: list[PendingChunk]) -> BatchReport:
        """§8: retry once with the failing chunk excluded.

        Which chunk failed is not reported by the provider, and the usual cause
        is a length overrun, so the longest chunk is the candidate. Excluding it
        and retrying recovers the rest of the batch in one more call rather than
        bisecting through seven.
        """
        if len(pending) <= 1:
            await _flag_failures(pending, "embedding failed for a single chunk")
            return BatchReport(failed=len(pending))

        suspect = max(pending, key=lambda c: c.token_count)
        remainder = [c for c in pending if c.chunk_id != suspect.chunk_id]

        try:
            vectors = await self.embeddings.embed(
                [c.text for c in remainder], input_type="document"
            )
        except EmbeddingUnavailable as exc:
            # The whole batch is still failing, so the provider is down rather
            # than one chunk being malformed. Nothing is flagged: these chunks
            # are fine and will be picked up on the next pass.
            log.warning("batch retry also failed (%s); leaving chunks unembedded", exc)
            return BatchReport()

        tokens = sum(c.token_count for c in remainder)
        await run_db(
            lambda s: _write_vectors(
                s, remainder, vectors, self.embeddings.model_version
            )
        )
        await _flag_failures(
            [suspect],
            f"Chunk could not be embedded (token_count={suspect.token_count}); "
            "it is invisible to vector search until this is fixed.",
        )
        return BatchReport(
            embedded=len(remainder),
            failed=1,
            tokens=tokens,
            cost_usd=_embedding_cost(tokens),
        )


def _claim_batch(
    session: Session, limit: int, source_id: uuid.UUID | None
) -> list[PendingChunk]:
    """Unembedded chunks, load-bearing material first (§8 "Batch scheduling").

    The ordering puts chunks curated onto a load-bearing concept at the head of
    the queue, then falls back to insertion order. ``ORDER BY`` on a boolean
    then ``created_at`` rather than two queries: one pass, one plan, and the
    priority is visible in the same place as the FIFO it overrides.
    """
    rows = session.execute(
        sql(
            """
            SELECT sc.id          AS chunk_id,
                   sc.text        AS text,
                   sc.token_count AS token_count,
                   EXISTS (
                       SELECT 1
                         FROM concept_sources cs
                         JOIN concepts c ON c.id = cs.concept_id
                        WHERE c.is_load_bearing
                          AND sc.id = ANY(cs.chunk_ids)
                   ) AS load_bearing
              FROM source_chunks sc
              JOIN sources s ON s.id = sc.source_id
             WHERE NOT EXISTS (
                       SELECT 1 FROM source_chunk_embeddings e
                        WHERE e.chunk_id = sc.id
                   )
               AND s.deleted_at IS NULL
               AND (:no_source OR sc.source_id = :source_id)
             ORDER BY load_bearing DESC, sc.created_at, sc.id
             LIMIT :limit
            """
        ),
        {
            "no_source": source_id is None,
            "source_id": source_id,
            "limit": limit,
        },
    ).all()

    return [
        PendingChunk(
            chunk_id=r.chunk_id,
            text=r.text,
            token_count=int(r.token_count or 0),
        )
        for r in rows
    ]


def _write_vectors(
    session: Session,
    chunks: list[PendingChunk],
    vectors: list[list[float]],
    model_version: str,
) -> int:
    """Insert one batch's vectors in one transaction (§8 "Batch shape").

    ``ON CONFLICT DO NOTHING`` because two workers can claim overlapping
    batches -- the claim query does not lock -- and a duplicate insert should
    be a no-op rather than a crash that loses the other 127 rows.
    """
    if len(chunks) != len(vectors):
        raise EmbeddingUnavailable(
            f"{len(vectors)} vectors for {len(chunks)} chunks; refusing to misalign"
        )

    # executemany over a parameter list rather than one unnest of text
    # literals. The text form makes Postgres parse a ~15 KB string per vector,
    # which is the same cost that dominated query latency before the binary
    # adapter went in (see studium.db._register_vector_type) -- and a batch is
    # 128 of them, so it is 128x the parse.
    session.execute(
        sql(
            """
            INSERT INTO source_chunk_embeddings (chunk_id, embedding, model_version)
            VALUES (:chunk_id, CAST(:embedding AS vector), :model_version)
            ON CONFLICT (chunk_id) DO NOTHING
            """
        ),
        [
            {
                "chunk_id": chunk.chunk_id,
                "embedding": _as_vector(vector),
                "model_version": model_version,
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
        ],
    )
    return len(chunks)


async def _flag_failures(chunks: list[PendingChunk], reason: str) -> None:
    """§8: persistent failures land in front of a reviewer at severity 3.

    This is where S3 was. The spec sends these to ``content_review_queue``,
    whose ``has_target`` CHECK requires an artifact or a session turn -- and a
    chunk that failed to embed at ingestion time has neither, so every insert
    failed the constraint. The only thing this function could do was log, which
    meant a chunk with no vector was invisible to vector search with nothing in
    front of a reviewer to say so.

    Subsystem 5 resolved it with ``ingestion_review_queue`` (ingestion §5
    addition 1), whose targets include ``source_chunk_id``. The row now goes
    where it was always meant to.

    Still logged as well as queued: the log line is what an operator watching a
    large ingestion sees in real time, and the queue row is what survives to be
    triaged afterwards.
    """
    from studium.ingestion.queue import flag_async

    for chunk in chunks:
        log.error(
            "chunk %s could not be embedded and is invisible to vector search: %s",
            chunk.chunk_id,
            reason,
        )
        try:
            await flag_async(
                flag_source="embedding_failure",
                source_chunk_id=chunk.chunk_id,
                reason=reason,
                severity=FAILED_CHUNK_SEVERITY,
                payload={"token_count": chunk.token_count},
            )
        except Exception as exc:  # noqa: BLE001
            # A queue write that fails must not fail the batch: the other 127
            # chunks in it embedded fine, and losing them to a bookkeeping
            # error would be a worse outcome than the log line this falls back
            # to. Logged at error so the gap is visible rather than swallowed.
            log.error("could not queue embedding_failure for %s: %s", chunk.chunk_id, exc)


def _embedding_cost(tokens: int) -> float:
    """§15: Voyage-3 at $0.06 per 1M input tokens."""
    from .providers import EMBEDDING_USD_PER_TOKEN

    return tokens * EMBEDDING_USD_PER_TOKEN

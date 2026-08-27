"""The ingestion review queue (ingestion §12).

The reviewer surface for everything ingestion-side that needs a human: pipeline
failures, license classification, and authoring conflicts. Distinct from
``content_review_queue`` in both schema and workflow -- that one holds review of
what the *runtime generated*, this one holds review of what *came in*.

The distinction is not bookkeeping. It is the S3 resolution: the content queue
requires an artifact or a session turn as its target, and an ingestion failure
has neither, so every ingestion-side write to it failed a CHECK. Retrieval's
embedding worker hit exactly that and logged instead, which meant a chunk that
never got a vector was invisible to vector search with no row anywhere saying
so. :func:`flag` is where that row now goes.

Severities, from §12.1, are a triage order rather than a taxonomy:

* **3** -- content is missing or wrong and a learner would notice.
  ``extractor_failure``, ``embedding_failure``, and the two validation errors
  that block a publish.
* **2** -- a decision is owed before publication. The license flags and
  concept-source conflicts.
* **1** -- worth a look, nothing is broken. Normalizer warnings, ambiguous
  chunk types.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.asyncdb import run_db

from .provenance import MissingProvenance, ingestion_writer

log = logging.getLogger(__name__)

#: §12.1. The default severity for each flag, so a caller that has no opinion
#: cannot invent one. Passing an explicit severity is for escalation (§12.3).
DEFAULT_SEVERITY: dict[str, int] = {
    "extractor_failure": 3,
    "normalizer_warning": 1,
    "embedding_failure": 3,
    "chunk_ambiguous_type": 1,
    "license_pending": 2,
    "license_conflict": 2,
    "concept_source_conflict": 2,
    "graph_validation_error": 3,
    "rubric_validation_error": 3,
}

#: The four columns the table's ``has_target`` CHECK accepts.
TARGET_COLUMNS = ("source_id", "source_chunk_id", "subject_id", "concept_id")


@dataclass(frozen=True, slots=True)
class QueueItem:
    """One row, flattened for a CLI or an admin page to render."""

    id: uuid.UUID
    flag_source: str
    reason: str
    severity: int
    status: str
    source_id: uuid.UUID | None = None
    source_chunk_id: uuid.UUID | None = None
    subject_id: uuid.UUID | None = None
    concept_id: uuid.UUID | None = None
    payload: dict[str, Any] | None = None
    resolution_note: str | None = None

    @property
    def target(self) -> str:
        for column in TARGET_COLUMNS:
            value = getattr(self, column)
            if value is not None:
                return f"{column.removesuffix('_id')}:{value}"
        return "none"  # pragma: no cover -- the CHECK makes this unreachable


@ingestion_writer
def flag(
    session: Session,
    *,
    flag_source: str,
    reason: str,
    source_id: uuid.UUID | None = None,
    source_chunk_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
    concept_id: uuid.UUID | None = None,
    severity: int | None = None,
    payload: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Write one review row. Returns its id.

    The four target parameters are optional individually and required
    collectively -- which the database also enforces, but a CHECK violation
    names a constraint, and the caller needs to be told which of four columns
    it forgot. Raising :class:`MissingProvenance` here names the gap in the
    vocabulary of §13, at the boundary where the gap is real.

    ``source_id`` is attribution-critical (§13) and still has a default here.
    That is not an exemption: the invariant forbids a *silent fallback*, and
    this raises. A queue row's target is genuinely one-of-four, so a signature
    demanding all four would be unsatisfiable by every caller.
    """
    targets = {
        "source_id": source_id,
        "source_chunk_id": source_chunk_id,
        "subject_id": subject_id,
        "concept_id": concept_id,
    }
    if not any(value is not None for value in targets.values()):
        raise MissingProvenance(
            "source_id",
            detail=(
                f"a {flag_source!r} row needs one of "
                f"{', '.join(TARGET_COLUMNS)}; a review item with no target "
                f"cannot be actioned"
            ),
        )

    if flag_source not in DEFAULT_SEVERITY:
        raise ValueError(
            f"unknown flag_source {flag_source!r}; "
            f"expected one of {', '.join(sorted(DEFAULT_SEVERITY))}"
        )

    row_id = session.execute(
        sql(
            """
            INSERT INTO ingestion_review_queue
                (source_id, source_chunk_id, subject_id, concept_id,
                 flag_source, reason, severity, payload)
            VALUES
                (:source_id, :source_chunk_id, :subject_id, :concept_id,
                 :flag_source, :reason, :severity, CAST(:payload AS jsonb))
            RETURNING id
            """
        ),
        {
            **targets,
            "flag_source": flag_source,
            "reason": reason,
            "severity": severity or DEFAULT_SEVERITY[flag_source],
            "payload": _json(payload or {}),
        },
    ).scalar_one()

    log.info("ingestion review queued: %s (%s)", flag_source, reason)
    return row_id


async def flag_async(**kwargs: Any) -> uuid.UUID:
    """:func:`flag` from an async caller. Used by the embedding worker."""
    return await run_db(lambda s: flag(s, **kwargs))


def pending(
    session: Session,
    *,
    severity: int | None = None,
    source_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[QueueItem]:
    """Pending items, most severe first (§12.2 ``studium queue list``).

    Ordered to match ``idx_ingestion_queue_pending`` so the partial index
    serves the reviewer's default view without a sort.
    """
    rows = session.execute(
        sql(
            """
            SELECT id, flag_source, reason, severity, status,
                   source_id, source_chunk_id, subject_id, concept_id,
                   payload, resolution_note
              FROM ingestion_review_queue
             WHERE status = 'pending'
               AND (:no_severity OR severity = :severity)
               AND (:no_source OR source_id = :source_id)
             ORDER BY severity DESC, created_at
             LIMIT :limit
            """
        ),
        {
            "no_severity": severity is None,
            "severity": severity,
            "no_source": source_id is None,
            "source_id": source_id,
            "limit": limit,
        },
    ).all()
    return [_as_item(row) for row in rows]


def get(session: Session, item_id: uuid.UUID) -> QueueItem | None:
    row = session.execute(
        sql(
            """
            SELECT id, flag_source, reason, severity, status,
                   source_id, source_chunk_id, subject_id, concept_id,
                   payload, resolution_note
              FROM ingestion_review_queue
             WHERE id = :id
            """
        ),
        {"id": item_id},
    ).one_or_none()
    return _as_item(row) if row else None


def resolve(
    session: Session,
    item_id: uuid.UUID,
    *,
    note: str,
    status: str = "resolved",
) -> bool:
    """Close an item (§12.3).

    ``resolved`` and ``dismissed`` are the same operation with different
    meanings: the problem was fixed, or it was not a problem. The distinction
    lives in the note, for whoever reads the row a year later -- which is why
    the note is required rather than optional. The row itself persists for
    audit until the retention job takes it.
    """
    if status not in {"resolved", "dismissed"}:
        raise ValueError(f"status must be 'resolved' or 'dismissed', not {status!r}")
    if not note.strip():
        raise ValueError("a resolution note is required; it is the audit record")

    result = session.execute(
        sql(
            """
            UPDATE ingestion_review_queue
               SET status = CAST(:status AS review_status),
                   resolution_note = :note,
                   resolved_at = NOW()
             WHERE id = :id
               AND status <> CAST(:status AS review_status)
            """
        ),
        {"id": item_id, "note": note, "status": status},
    )
    return bool(result.rowcount)


def escalate(session: Session, item_id: uuid.UUID, *, severity: int) -> bool:
    """Raise an item's severity (§12.3).

    Only upward. A reviewer who finds a severity-1 warning is really a
    severity-3 defect raises it; the reverse would let a triage pass quietly
    bury something, and dismissing with a note is the honest way to do that.
    """
    if not 1 <= severity <= 3:
        raise ValueError(f"severity must be 1-3, not {severity}")
    result = session.execute(
        sql(
            """
            UPDATE ingestion_review_queue
               SET severity = :severity
             WHERE id = :id
               AND severity < :severity
            """
        ),
        {"id": item_id, "severity": severity},
    )
    return bool(result.rowcount)


def depth(session: Session) -> dict[str, int]:
    """Pending count per flag source, for the §12.2 admin page's overview."""
    rows = session.execute(
        sql(
            """
            SELECT flag_source, COUNT(*) AS n
              FROM ingestion_review_queue
             WHERE status = 'pending'
             GROUP BY flag_source
             ORDER BY flag_source
            """
        )
    ).all()
    return {row.flag_source: int(row.n) for row in rows}


def _as_item(row: Any) -> QueueItem:
    return QueueItem(
        id=row.id,
        flag_source=row.flag_source,
        reason=row.reason,
        severity=int(row.severity),
        status=row.status,
        source_id=row.source_id,
        source_chunk_id=row.source_chunk_id,
        subject_id=row.subject_id,
        concept_id=row.concept_id,
        payload=row.payload or {},
        resolution_note=row.resolution_note,
    )


def _json(value: dict[str, Any]) -> str:
    import json

    from studium.asyncdb import jsonable

    return json.dumps(jsonable(value), ensure_ascii=False)

"""§6.13 Content ingestion and human review."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import (
    Base,
    created_at,
    ingestion_job_kind,
    ingestion_job_status,
    nullable_ts,
    review_flag_source,
    review_status,
    updated_at,
    uuid_pk,
)


class IngestionJob(Base):
    """Pipeline tracker for a source being processed into chunks, embeddings,
    and initial concept_sources mappings.

    Workers claim rows with ``SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1``.
    """

    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(ingestion_job_kind, nullable=False)
    status: Mapped[str] = mapped_column(
        ingestion_job_status, nullable=False, server_default=text("'pending'")
    )
    attempt: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    started_at: Mapped[dt.datetime | None] = nullable_ts()
    finished_at: Mapped[dt.datetime | None] = nullable_ts()
    error: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[float] = mapped_column(
        Numeric(10, 4), nullable=False, server_default=text("0")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    __table_args__ = (
        Index(
            "idx_ingestion_jobs_pending",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("idx_ingestion_jobs_source", "source_id", "kind", "status"),
    )


class ContentReviewQueueItem(Base):
    """Items flagged for human review.

    Populated by the Confusion-Tracker when a generated lecture seems to have
    caused unusual confusion, by the Evaluator when a grading looks suspicious,
    and by learners via "report this".
    """

    __tablename__ = "content_review_queue"

    id: Mapped[uuid.UUID] = uuid_pk()
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("content_artifacts.id", ondelete="CASCADE")
    )
    session_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("session_turns.id", ondelete="CASCADE")
    )
    source: Mapped[str] = mapped_column(review_flag_source, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("2")
    )
    status: Mapped[str] = mapped_column(
        review_status, nullable=False, server_default=text("'pending'")
    )
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[dt.datetime | None] = nullable_ts()
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    __table_args__ = (
        # A review item pointing at neither an artifact nor a turn has no subject.
        CheckConstraint(
            "artifact_id IS NOT NULL OR session_turn_id IS NOT NULL",
            name="has_target",
        ),
        CheckConstraint("severity BETWEEN 1 AND 3", name="severity_range"),
        Index(
            "idx_review_queue_pending",
            text("severity DESC"),
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "idx_review_queue_assigned",
            "assigned_to",
            "status",
            postgresql_where=text("assigned_to IS NOT NULL"),
        ),
        Index("idx_review_queue_artifact", "artifact_id"),
        Index("idx_review_queue_turn", "session_turn_id"),
    )

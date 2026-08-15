"""§6.4 Content artifacts.

Generated content: lecture segments, worked examples, practice problems,
rubric prompts, tutorial seeds. Every artifact is grounded (linked to source
chunks), versioned (via superseded_by), and stanced.
"""

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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import (
    Base,
    agent_identity,
    artifact_kind,
    artifact_stance,
    artifact_status,
    created_at,
    nullable_ts,
    sha256,
    sha256_check,
    updated_at,
    uuid_pk,
)


class ContentArtifact(Base):
    __tablename__ = "content_artifacts"

    id: Mapped[uuid.UUID] = uuid_pk()
    concept_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("concepts.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(artifact_kind, nullable=False)
    stance: Mapped[str] = mapped_column(
        artifact_stance, nullable=False, server_default=text("'default'")
    )
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    #: markdown
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: Varies by kind; the per-kind shapes belong to the Content Ingestion
    #: spec. For practice_problem: {"difficulty": 3, "expected_minutes": 10,
    #: "hint_ladder": [...]}. For lecture_segment: {"segment_index": 4,
    #: "of": 12, "estimated_minutes": 8}.
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    generated_at: Mapped[dt.datetime] = created_at()
    generated_by: Mapped[str] = mapped_column(agent_identity, nullable=False)
    #: Exact API model id used, e.g. 'claude-opus-4-8'. Kept so cost rows can
    #: be joined back to per-model pricing.
    model: Mapped[str | None] = mapped_column(Text)
    prompt_hash: Mapped[str | None] = sha256(nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 4))
    #: Lifecycle: draft -> reviewed -> active -> retired. Orthogonal to
    #: retired_at, which timestamps replacement of a previously active row --
    #: an artifact rejected in review is 'retired' with retired_at NULL.
    status: Mapped[str] = mapped_column(
        artifact_status, nullable=False, server_default=text("'draft'")
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[dt.datetime | None] = nullable_ts()
    #: The session whose activity triggered this generation, when there was
    #: one. Spec v1.1 §6.12 attributes content cost to "the learner in whose
    #: session the generation was triggered", and NULL for Curator-initiated
    #: pre-generation -- but gives content_artifacts no session or user column
    #: to attribute through. This is that column; NULL books the cost to the
    #: system account instead. See DIVERGENCES.md (V2).
    generated_for_session_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("learning_sessions.id", ondelete="SET NULL")
    )
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("content_artifacts.id", ondelete="SET NULL")
    )
    retired_at: Mapped[dt.datetime | None] = nullable_ts()
    deleted_at: Mapped[dt.datetime | None] = nullable_ts()
    # Spec §6.4 omits both, but the table is mutable (status, reviewed_by,
    # superseded_by) and §9's optimistic-concurrency scheme reads updated_at
    # on exactly this table. See DIVERGENCES (A3).
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    citations: Mapped[list[ContentCitation]] = relationship(back_populates="artifact")

    __table_args__ = (
        sha256_check("prompt_hash"),
        Index(
            "idx_artifacts_concept_kind_active",
            "concept_id",
            "kind",
            "stance",
            postgresql_where=text(
                "status = 'active' AND retired_at IS NULL AND deleted_at IS NULL"
            ),
        ),
        # Unconditional companion: the partial index above cannot serve a
        # concept cascade, which must also find draft and retired rows. (A5)
        Index("idx_artifacts_concept", "concept_id"),
        Index(
            "idx_artifacts_status",
            "status",
            postgresql_where=text(
                "status IN ('draft', 'reviewed') AND deleted_at IS NULL"
            ),
        ),
        Index(
            "idx_artifacts_review_queue",
            "generated_at",
            postgresql_where=text("status = 'draft' AND deleted_at IS NULL"),
        ),
        Index(
            "idx_artifacts_reviewed_by",
            "reviewed_by",
            postgresql_where=text("reviewed_by IS NOT NULL"),
        ),
        Index(
            "idx_artifacts_superseded_by",
            "superseded_by",
            postgresql_where=text("superseded_by IS NOT NULL"),
        ),
        Index(
            "idx_artifacts_generated_for_session",
            "generated_for_session_id",
            postgresql_where=text("generated_for_session_id IS NOT NULL"),
        ),
        # Makes "the current segment" single-valued: without this, two active
        # rows can share a slot and the Lecturer's ORDER BY is arbitrary.
        Index(
            "idx_artifacts_active_slot",
            "concept_id",
            "kind",
            "stance",
            text("((metadata ->> 'segment_index'))"),
            unique=True,
            postgresql_where=text(
                "status = 'active' AND retired_at IS NULL AND deleted_at IS NULL"
            ),
        ),
    )


class ContentCitation(Base):
    """Links an artifact to the passages that ground it. Every non-trivial
    factual claim in a generated lecture must be traceable through this table."""

    __tablename__ = "content_citations"

    id: Mapped[uuid.UUID] = uuid_pk()
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("content_artifacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: RESTRICT, not CASCADE: if a chunk would be deleted while artifacts still
    #: cite it, the delete is blocked. Resolving that is the re-ingestion
    #: workflow's job; the schema refuses to silently orphan citations.
    source_chunk_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("source_chunks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: The exact substring cited, if any.
    quoted_span: Mapped[str | None] = mapped_column(Text)
    #: Why this citation supports the artifact.
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = created_at()

    artifact: Mapped[ContentArtifact] = relationship(back_populates="citations")

    __table_args__ = (
        UniqueConstraint(
            "artifact_id", "source_chunk_id", name="uq_content_citations_pair"
        ),
        Index("idx_content_citations_chunk", "source_chunk_id"),
    )


class RubricCriterion(Base):
    """The mastery-assessment rubric per concept.

    ``key_points`` never leaves the server: §11 forbids learner reads, and the
    projection helpers in ``studium.acl`` strip it from every learner-facing
    serialisation, including assessment_responses.criterion_snapshot.
    """

    __tablename__ = "rubric_criteria"

    id: Mapped[uuid.UUID] = uuid_pk()
    concept_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("concepts.id", ondelete="CASCADE"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    #: The question shown to the learner.
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    #: Array of {point, weight, source_chunk_id?}.
    key_points: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    weight: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    min_words: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("20")
    )
    status: Mapped[str] = mapped_column(
        artifact_status, nullable=False, server_default=text("'draft'")
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[dt.datetime | None] = nullable_ts()
    retired_at: Mapped[dt.datetime | None] = nullable_ts()
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    __table_args__ = (
        UniqueConstraint("concept_id", "slug", name="uq_rubric_criteria_concept_slug"),
        CheckConstraint("weight IN (1, 2, 3)", name="weight_range"),
        CheckConstraint("jsonb_typeof(key_points) = 'array'", name="key_points_array"),
        Index(
            "idx_rubric_criteria_concept_active",
            "concept_id",
            postgresql_where=text("status = 'active' AND retired_at IS NULL"),
        ),
        Index("idx_rubric_criteria_reviewed_by", "reviewed_by"),
    )

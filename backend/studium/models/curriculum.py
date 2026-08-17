"""§6.2 Curriculum: the concept graph.

The graph is the pedagogical spine: subjects, concepts, typed edges, a mapping
to source passages, and a computed-statistics table.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    REAL,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import (
    TIMESTAMPTZ,
    Base,
    artifact_status,
    concept_edge_kind,
    concept_source_role,
    created_at,
    nullable_ts,
    updated_at,
    uuid_pk,
)


class Subject(Base):
    __tablename__ = "subjects"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    short_description: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    #: markdown, learner-facing
    long_description: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    #: Bumped when the graph is substantially reshaped (nodes added, edges
    #: retyped). Point edits to individual concepts do not bump it.
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    authored_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        artifact_status, nullable=False, server_default=text("'draft'")
    )
    published_at: Mapped[dt.datetime | None] = nullable_ts()
    #: Pass mark for assessments scoped to this subject. Spec §6.8 puts the
    #: override in subject_metadata, which holds computed graph statistics and
    #: has no such column -- it belongs on the authored row. See DIVERGENCES (B2).
    assessment_threshold: Mapped[float] = mapped_column(
        REAL, nullable=False, server_default=text("0.75")
    )
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()
    deleted_at: Mapped[dt.datetime | None] = nullable_ts()

    concepts: Mapped[list[Concept]] = relationship(
        back_populates="subject", passive_deletes=True
    )
    meta_row: Mapped[SubjectMetadata | None] = relationship(
        back_populates="subject", uselist=False, passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint(r"slug ~ '^[a-z0-9][a-z0-9-]{1,63}$'", name="slug_format"),
        CheckConstraint(
            "assessment_threshold >= 0 AND assessment_threshold <= 1",
            name="assessment_threshold_range",
        ),
        Index("idx_subjects_authored_by", "authored_by"),
        # §14 asserts every soft-delete table has a partial index excluding
        # deleted rows; the spec gives subjects no index at all. (A3/A5)
        Index(
            "idx_subjects_active",
            "slug",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class Concept(Base):
    """The node table. One row is one concept in one subject's graph."""

    __tablename__ = "concepts"

    id: Mapped[uuid.UUID] = uuid_pk()
    subject_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    short_description: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    #: markdown
    long_description: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    #: Pedagogical difficulty band, 1 = introductory .. 5 = advanced. Used by
    #: the Curator to sequence and by the Reviewer to weight review cost.
    depth: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1")
    )
    #: The small set of concepts most others depend on. The map view weights
    #: these visually; the Reviewer prioritises them.
    is_load_bearing: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    #: Curator's estimate for a first-pass lecture; feeds session scheduling.
    estimated_minutes: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("30")
    )
    #: Sort order within the subject.
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: Optional grouping label, added by migration 0002. The draft's Module is
    #: not a first-class entity in this schema -- modules become a grouping of
    #: concepts, which is all the draft ever used them for.
    module_slug: Mapped[str | None] = mapped_column(Text)
    #: Per-concept defaults, including the BKT parameters copied onto each
    #: concept_mastery row at creation. Spec §6.5 refers to concepts.metadata
    #: without defining the column. See DIVERGENCES (B1).
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    subject: Mapped[Subject] = relationship(back_populates="concepts")

    __table_args__ = (
        UniqueConstraint("subject_id", "slug", name="uq_concepts_subject_slug"),
        CheckConstraint(r"slug ~ '^[a-z0-9][a-z0-9-]{1,80}$'", name="slug_format"),
        CheckConstraint("depth BETWEEN 1 AND 5", name="depth_range"),
        CheckConstraint(
            r"module_slug IS NULL OR module_slug ~ '^[a-z0-9][a-z0-9-]{1,63}$'",
            name="module_slug_format",
        ),
        Index("idx_concepts_subject_position", "subject_id", "position"),
        Index(
            "idx_concepts_load_bearing",
            "subject_id",
            postgresql_where=text("is_load_bearing"),
        ),
        Index(
            "idx_concepts_module",
            "subject_id",
            "module_slug",
            postgresql_where=text("module_slug IS NOT NULL"),
        ),
    )


class ConceptEdge(Base):
    """Typed directed edges between concepts within a subject.

    Prerequisite edges drive gating: a concept is unlocked when every incoming
    prerequisite edge's source has mastery above threshold. Dependency edges
    drive retrieval and review-neighbourhood queries -- a course can
    dependency-mention an earlier concept without requiring it.

    Acyclicity is enforced in application code at publish time
    (``studium.graph.assert_acyclic``), not by a constraint: Postgres cannot
    cheaply enforce it without a recursive CTE on every insert.
    """

    __tablename__ = "concept_edges"

    id: Mapped[uuid.UUID] = uuid_pk()
    subject_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_concept_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("concepts.id", ondelete="CASCADE"),
        nullable=False,
    )
    to_concept_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("concepts.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(concept_edge_kind, nullable=False)
    #: Soft signal for the Curator when the graph offers several viable paths.
    #: 1.0 is strong; lower marks the edge as suggestive rather than required.
    weight: Mapped[float] = mapped_column(
        REAL, nullable=False, server_default=text("1.0")
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = created_at()

    __table_args__ = (
        UniqueConstraint(
            "from_concept_id", "to_concept_id", "kind", name="uq_concept_edges_triple"
        ),
        CheckConstraint("from_concept_id <> to_concept_id", name="no_self_edge"),
        CheckConstraint("weight > 0 AND weight <= 1.0", name="weight_range"),
        Index("idx_concept_edges_from", "from_concept_id", "kind"),
        Index("idx_concept_edges_to", "to_concept_id", "kind"),
        Index("idx_concept_edges_subject", "subject_id"),
    )


class ConceptSource(Base):
    """Links a concept to the corpus passages that define or illustrate it."""

    __tablename__ = "concept_sources"

    id: Mapped[uuid.UUID] = uuid_pk()
    concept_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("concepts.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: source_chunks.id values. Deliberately an array rather than a join table:
    #: this is a curated pointer set for retrieval to prefer, and a broken
    #: reference should degrade gracefully rather than block a lecture. The
    #: daily consistency check flags dangling IDs for reviewer attention.
    chunk_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PgUUID(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    role: Mapped[str] = mapped_column(concept_source_role, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = created_at()

    __table_args__ = (
        UniqueConstraint(
            "concept_id", "source_id", "role", name="uq_concept_sources_triple"
        ),
        Index("idx_concept_sources_concept", "concept_id", "role"),
        Index("idx_concept_sources_source", "source_id"),
    )


class SubjectMetadata(Base):
    """Computed graph statistics, one-to-one with subjects.

    Refreshed by ``refresh_subject_metadata(subject_id)`` after graph
    mutations, not by row triggers: an MVP-scale bulk import would fire a row
    trigger thousands of times, and one call after the import is cheaper.
    """

    __tablename__ = "subject_metadata"

    subject_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    concept_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    edge_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    load_bearing_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    estimated_total_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_computed_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=text("NOW()")
    )
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    subject: Mapped[Subject] = relationship(back_populates="meta_row")

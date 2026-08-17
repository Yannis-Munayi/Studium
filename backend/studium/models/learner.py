"""§6.5 Learner state: enrollments and mastery.

``concept_mastery`` is the system's canonical statement of what the learner
knows. Any agent whose interaction produces evidence writes to it; no agent
owns it. That is what lets the Reviewer, the Curator, and the frontend read a
consistent picture without asking a model to synthesise one on demand.
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import (
    Base,
    created_at,
    mastery_event_kind,
    nullable_ts,
    updated_at,
    uuid_pk,
)


class LearnerSubject(Base):
    """One enrollment: a learner working through one subject."""

    __tablename__ = "learner_subjects"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Captured at enrollment. Freezing the number lets us detect that the
    #: graph moved and offer a migration on next session start (via the
    #: graph_migration_needed flag in preferences). Note it does not by itself
    #: pin the learner to the old graph -- concepts are not versioned rows.
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    enrolled_at: Mapped[dt.datetime] = created_at()
    current_focus_concept_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("concepts.id", ondelete="SET NULL")
    )
    #: The Curator's ordered list of concept ids -- a plan, not a schedule.
    #: Regenerated as mastery evolves; stored for auditability and the roadmap.
    syllabus_plan: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    intake_summary: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    #: Overrides user_profiles.preferences at the per-subject level, e.g. a
    #: slower pace for one subject and a normal one for another.
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    archived_at: Mapped[dt.datetime | None] = nullable_ts()
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    __table_args__ = (
        UniqueConstraint("user_id", "subject_id", name="uq_learner_subjects_pair"),
        # Target for the composite FKs on learning_sessions, journal_entries,
        # assessment_attempts and portfolio_items: those tables denormalise
        # user_id alongside learner_subject_id, and nothing in the spec stops
        # the two disagreeing. See DIVERGENCES (C9).
        UniqueConstraint("id", "user_id", name="uq_learner_subjects_id_user"),
        Index(
            "idx_learner_subjects_user_active",
            "user_id",
            postgresql_where=text("archived_at IS NULL"),
        ),
        Index("idx_learner_subjects_subject", "subject_id"),
        Index("idx_learner_subjects_focus", "current_focus_concept_id"),
    )


class ConceptMastery(Base):
    """Per-(enrollment, concept) mastery state.

    ``p_known`` is the raw BKT posterior after the most recent evidence.
    ``p_known_decayed`` is that value under an exponential forgetting curve
    keyed on ``last_evidence_at``.

    Gating reads the decayed value. Because a stored column is only as fresh
    as the last decay job, ``studium.mastery.decayed()`` recomputes it at read
    time and the prerequisite query in ``studium.queries`` computes decay in
    SQL rather than trusting the column. The column remains as a materialised
    convenience for sorting and reporting. See DIVERGENCES (C1).
    """

    __tablename__ = "concept_mastery"

    id: Mapped[uuid.UUID] = uuid_pk()
    learner_subject_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("learner_subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    concept_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("concepts.id", ondelete="CASCADE"),
        nullable=False,
    )
    p_known: Mapped[float] = mapped_column(
        REAL, nullable=False, server_default=text("0.1")
    )
    p_known_decayed: Mapped[float] = mapped_column(
        REAL, nullable=False, server_default=text("0.1")
    )
    evidence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_evidence_at: Mapped[dt.datetime | None] = nullable_ts()
    first_reached_mastery_at: Mapped[dt.datetime | None] = nullable_ts()
    #: Per-concept BKT parameters: {"p_init", "p_transit", "p_slip", "p_guess"}.
    #: Copied from concepts.metadata at row creation. Held per learner row so
    #: per-learner tuning is a later refinement, not a migration.
    bkt_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    events: Mapped[list[MasteryEvent]] = relationship(
        back_populates="mastery", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint(
            "learner_subject_id", "concept_id", name="uq_concept_mastery_pair"
        ),
        CheckConstraint("p_known >= 0.0 AND p_known <= 1.0", name="p_known_range"),
        CheckConstraint(
            "p_known_decayed >= 0.0 AND p_known_decayed <= 1.0",
            name="p_known_decayed_range",
        ),
        # Sorted on the decayed value: that is what the Curator, the Reviewer
        # and the frontend read. The spec indexes p_known. See DIVERGENCES (C1).
        Index(
            "idx_concept_mastery_learner",
            "learner_subject_id",
            text("p_known_decayed DESC"),
        ),
        Index("idx_concept_mastery_concept", "concept_id"),
        Index(
            "idx_concept_mastery_stale",
            "last_evidence_at",
            postgresql_where=text("last_evidence_at IS NOT NULL"),
        ),
    )


class MasteryEvent(Base):
    """Append-only evidence log.

    Every write to ``concept_mastery.p_known`` is preceded by an insert here,
    in the same transaction, so any current value can be reconstructed from
    its history -- and mastery can be re-derived under a corrected BKT
    parameterisation without losing evidence.

    UPDATE and DELETE are revoked from the application role in migration 0003.
    """

    __tablename__ = "mastery_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    concept_mastery_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("concept_mastery.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("learning_sessions.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(mastery_event_kind, nullable=False)
    p_known_before: Mapped[float] = mapped_column(REAL, nullable=False)
    p_known_after: Mapped[float] = mapped_column(REAL, nullable=False)
    #: Shape varies by kind. practice_incorrect: {"artifact_id", "learner_response",
    #: "expected_key_points", "missed_points"}. assessment_scored:
    #: {"attempt_id", "criterion_id", "score"}.
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = created_at()

    mastery: Mapped[ConceptMastery] = relationship(back_populates="events")

    __table_args__ = (
        Index(
            "idx_mastery_events_mastery_time",
            "concept_mastery_id",
            text("created_at DESC"),
        ),
        Index(
            "idx_mastery_events_session",
            "session_id",
            postgresql_where=text("session_id IS NOT NULL"),
        ),
    )

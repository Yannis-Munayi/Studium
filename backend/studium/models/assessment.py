"""§6.8 Assessment: the summative counterpart to formative interaction.

Passing is ``score >= threshold`` computed in ``studium.assessment.compute_pass``,
never by the grading agent -- §3's first invariant.

The two nullable owner columns exist so §10's right-to-erasure can run: these
rows are *retained* as de-identified evidence, which is impossible if a user or
enrollment delete cascades them away. Ratified in spec v1.1 §6.8 ("A note on
nullability"), which carries the same reasoning.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    REAL,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
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
    assessment_mode,
    assessment_trigger,
    created_at,
    nullable_ts,
    updated_at,
    uuid_pk,
)


class AssessmentAttempt(Base):
    __tablename__ = "assessment_attempts"

    id: Mapped[uuid.UUID] = uuid_pk()
    #: SET NULL rather than CASCADE: erasure de-identifies the row instead of
    #: deleting it, so the aggregate survives with no PII attached.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    learner_subject_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    scope_concept_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("concepts.id", ondelete="SET NULL")
    )
    mode: Mapped[str] = mapped_column(
        assessment_mode, nullable=False, server_default=text("'formative'")
    )
    triggered_by: Mapped[str] = mapped_column(assessment_trigger, nullable=False)
    score: Mapped[float | None] = mapped_column(REAL)
    #: Derived, never model-judged. NULL until graded.
    passed: Mapped[bool | None] = mapped_column()
    #: Snapshot of subjects.assessment_threshold at attempt time, so a later
    #: change to the subject does not retroactively flip a historical pass.
    threshold: Mapped[float] = mapped_column(
        REAL, nullable=False, server_default=text("0.75")
    )
    proctored: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    started_at: Mapped[dt.datetime] = created_at()
    submitted_at: Mapped[dt.datetime | None] = nullable_ts()
    graded_at: Mapped[dt.datetime | None] = nullable_ts()
    grading_cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 4))
    overall_feedback: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("learning_sessions.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    responses: Mapped[list[AssessmentResponse]] = relationship(
        back_populates="attempt"
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["learner_subject_id", "user_id"],
            ["learner_subjects.id", "learner_subjects.user_id"],
            ondelete="SET NULL",
            name="fk_assessment_attempts_enrollment",
        ),
        CheckConstraint("score >= 0 AND score <= 1", name="score_range"),
        CheckConstraint("threshold >= 0 AND threshold <= 1", name="threshold_range"),
        # passed is only meaningful once a score exists.
        CheckConstraint(
            "(passed IS NULL) = (score IS NULL)", name="passed_requires_score"
        ),
        Index("idx_assessment_attempts_user_time", "user_id", text("started_at DESC")),
        Index(
            "idx_assessment_attempts_scope",
            "learner_subject_id",
            "scope_concept_id",
            text("submitted_at DESC"),
        ),
        Index("idx_assessment_attempts_concept", "scope_concept_id"),
        Index("idx_assessment_attempts_session", "session_id"),
    )


class AssessmentResponse(Base):
    """One graded answer per rubric criterion per attempt."""

    __tablename__ = "assessment_responses"

    id: Mapped[uuid.UUID] = uuid_pk()
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("assessment_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    rubric_criterion_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("rubric_criteria.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: The criterion's state at grading time -- what makes a three-week-old
    #: attempt still show the criterion the learner was actually assessed
    #: against. Contains key_points, so it is stripped from learner-facing
    #: serialisations by studium.acl.project_assessment_response. (C5)
    criterion_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    prompt_shown: Mapped[str] = mapped_column(Text, nullable=False)
    learner_response: Mapped[str] = mapped_column(Text, nullable=False)
    grade: Mapped[int | None] = mapped_column(SmallInteger)
    feedback: Mapped[str | None] = mapped_column(Text)
    missing_points: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    graded_at: Mapped[dt.datetime | None] = nullable_ts()
    created_at: Mapped[dt.datetime] = created_at()

    attempt: Mapped[AssessmentAttempt] = relationship(back_populates="responses")

    __table_args__ = (
        UniqueConstraint(
            "attempt_id", "rubric_criterion_id", name="uq_assessment_responses_pair"
        ),
        CheckConstraint("grade IN (0, 1, 2)", name="grade_range"),
        Index("idx_assessment_responses_criterion", "rubric_criterion_id"),
    )

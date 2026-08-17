"""§6.9 Review schedule (FSRS).

Every concept the learner has evidence for gets a ``review_cards`` row; FSRS
updates stability and difficulty after each review. The schema does not
enforce the state machine -- ``studium.review.fsrs`` is trusted to compute
``stability_after``, ``difficulty_after`` and ``due_at``, and the event log
makes miscalculations recoverable.
"""

from __future__ import annotations

import datetime as dt
import uuid

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
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import (
    TIMESTAMPTZ,
    Base,
    created_at,
    nullable_ts,
    updated_at,
    uuid_pk,
)


class ReviewCard(Base):
    __tablename__ = "review_cards"

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
    stability: Mapped[float] = mapped_column(
        REAL, nullable=False, server_default=text("1.0")
    )
    #: FSRS scale, 1..10.
    difficulty: Mapped[float] = mapped_column(
        REAL, nullable=False, server_default=text("5.0")
    )
    #: Computed at last review.
    retrievability: Mapped[float] = mapped_column(
        REAL, nullable=False, server_default=text("1.0")
    )
    due_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMPTZ, nullable=False, server_default=text("NOW()")
    )
    last_reviewed_at: Mapped[dt.datetime | None] = nullable_ts()
    reps: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    lapses: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'new'")
    )
    suspended: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    events: Mapped[list[ReviewEvent]] = relationship(
        back_populates="card", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint(
            "learner_subject_id", "concept_id", name="uq_review_cards_pair"
        ),
        CheckConstraint(
            "state IN ('new', 'learning', 'review', 'relearning')", name="state"
        ),
        # The spec comments these ranges but does not constrain them.
        CheckConstraint("difficulty >= 1 AND difficulty <= 10", name="difficulty_range"),
        CheckConstraint(
            "retrievability >= 0 AND retrievability <= 1", name="retrievability_range"
        ),
        CheckConstraint("stability > 0", name="stability_positive"),
        Index(
            "idx_review_cards_due",
            "learner_subject_id",
            "due_at",
            postgresql_where=text("NOT suspended"),
        ),
        Index(
            "idx_review_cards_state",
            "learner_subject_id",
            "state",
            postgresql_where=text("NOT suspended"),
        ),
        Index("idx_review_cards_concept", "concept_id"),
    )


class ReviewEvent(Base):
    """Append-only FSRS history."""

    __tablename__ = "review_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    card_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("review_cards.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("learning_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("content_artifacts.id", ondelete="SET NULL")
    )
    #: again / hard / good / easy
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    response_text: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    elapsed_seconds: Mapped[int | None] = mapped_column(Integer)
    stability_before: Mapped[float] = mapped_column(REAL, nullable=False)
    stability_after: Mapped[float] = mapped_column(REAL, nullable=False)
    difficulty_before: Mapped[float] = mapped_column(REAL, nullable=False)
    difficulty_after: Mapped[float] = mapped_column(REAL, nullable=False)
    created_at: Mapped[dt.datetime] = created_at()

    card: Mapped[ReviewCard] = relationship(back_populates="events")

    __table_args__ = (
        CheckConstraint("rating IN (1, 2, 3, 4)", name="rating_range"),
        Index("idx_review_events_card_time", "card_id", text("created_at DESC")),
        Index("idx_review_events_session", "session_id"),
        Index("idx_review_events_artifact", "artifact_id"),
    )

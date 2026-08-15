"""§6.7 Confusion journal: the running record of what the learner cannot yet do.

Three text fields, deliberately separate. ``summary`` is what shows on the
journal card. ``hypothesis`` is a Tutor-facing prompt aid that must not be
shown to the learner verbatim -- it can be wrong, and reading it can be
dispiriting. ``learner_note`` is the learner's own space.

Because §11 grants learners read access to their own journal rows, that
"must not be shown" rule is a column-level projection, applied in
``studium.acl.project_journal_entry``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import (
    Base,
    created_at,
    journal_event_kind,
    journal_status,
    nullable_ts,
    updated_at,
    uuid_pk,
)


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    learner_subject_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False
    )
    concept_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("concepts.id", ondelete="SET NULL")
    )
    first_session_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("learning_sessions.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        journal_status, nullable=False, server_default=text("'open'")
    )
    #: Learner- or Tutor-authored statement of the confusion.
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    #: The Confusion-Tracker's inference about the underlying gap. Never
    #: serialised to a learner.
    hypothesis: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    #: Learner-authored, editable.
    learner_note: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[dt.datetime] = created_at()
    last_touched_at: Mapped[dt.datetime] = created_at()
    resolved_at: Mapped[dt.datetime | None] = nullable_ts()
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    events: Mapped[list[JournalEvent]] = relationship(back_populates="entry")

    __table_args__ = (
        ForeignKeyConstraint(
            ["learner_subject_id", "user_id"],
            ["learner_subjects.id", "learner_subjects.user_id"],
            ondelete="CASCADE",
            name="fk_journal_entries_enrollment",
        ),
        CheckConstraint(
            "origin IN ('learner_flagged', 'tracker_inferred', "
            "'check_failed', 'assessment_gap')",
            name="origin",
        ),
        Index(
            "idx_journal_entries_user_open",
            "user_id",
            text("last_touched_at DESC"),
            postgresql_where=text("status IN ('open', 'partial')"),
        ),
        Index(
            "idx_journal_entries_learner_subject",
            "learner_subject_id",
            "status",
            text("last_touched_at DESC"),
        ),
        Index(
            "idx_journal_entries_concept",
            "concept_id",
            postgresql_where=text("concept_id IS NOT NULL"),
        ),
        Index("idx_journal_entries_first_session", "first_session_id"),
    )


class JournalEvent(Base):
    """Append-only history of one journal entry."""

    __tablename__ = "journal_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    entry_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("journal_entries.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("learning_sessions.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(journal_event_kind, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = created_at()

    entry: Mapped[JournalEntry] = relationship(back_populates="events")

    __table_args__ = (
        Index("idx_journal_events_entry_time", "entry_id", text("created_at DESC")),
        Index("idx_journal_events_session", "session_id"),
    )

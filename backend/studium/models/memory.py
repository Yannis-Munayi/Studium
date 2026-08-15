"""§6.11 Cross-session memory: what the app remembers about you.

Fed by session close, read at session start.
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
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, agent_identity, created_at, updated_at, uuid_pk


class SessionSummary(Base):
    """Read-through cache of session closes, written once at session end.

    A separate table rather than columns on learning_sessions because a
    summary can be regenerated (say, after a Curator prompt improvement) and
    generating one is a real LLM call with real cost that must not fire on
    every session read. Existence of a row means "this session is summarised".
    """

    __tablename__ = "session_summaries"

    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("learning_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    key_points: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    open_threads: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    concepts_touched: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PgUUID(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    generated_at: Mapped[dt.datetime] = created_at()
    generated_by: Mapped[str] = mapped_column(
        agent_identity, nullable=False, server_default=text("'orchestrator'")
    )
    cost_usd: Mapped[float] = mapped_column(
        Numeric(10, 4), nullable=False, server_default=text("0")
    )
    #: A summary can be regenerated after a prompt improvement, so the row is
    #: mutable and needs the timestamp the regenerability claim implies.
    updated_at: Mapped[dt.datetime] = updated_at()


class RetrievalCheck(Base):
    """The brief test at session start that verifies whether the prior
    session's material stuck.

    The overall score is fed into mastery_events as review_correct /
    review_incorrect against the relevant concepts, so the check both
    diagnoses retention and updates mastery. The Curator uses the result to
    decide whether to proceed to new material or revisit yesterday's.
    """

    __tablename__ = "retrieval_checks"

    id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("learning_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: SET NULL, not CASCADE: the prior session ages out under the 2-year
    #: retention long before this row does, and losing the newer session's
    #: check because its subject was pruned is not what §10 intends. (C14)
    based_on_session_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("learning_sessions.id", ondelete="SET NULL")
    )
    #: Generated retrieval prompts.
    prompts: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    #: Learner responses.
    responses: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    #: Per-prompt {correct, partial, incorrect}.
    scores: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    overall_score: Mapped[float] = mapped_column(REAL, nullable=False)
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    __table_args__ = (
        CheckConstraint(
            "overall_score >= 0 AND overall_score <= 1", name="overall_score_range"
        ),
        Index("idx_retrieval_checks_session", "session_id"),
        Index("idx_retrieval_checks_based_on", "based_on_session_id"),
    )

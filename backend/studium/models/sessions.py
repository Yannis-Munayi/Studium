"""§6.6 Learning sessions and turns.

Agent context isolation is structural here: there is no shared "conversation"
table. Each agent's turns are ``session_turns`` rows tagged with the acting
agent, so an Evaluator grading a response cannot read a Tutor's encouraging
preamble as evidence of correctness, and a Tutor is not conditioned on the
Confusion-Tracker's private hypothesis. Agents share the session, not the
context.
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
    Integer,
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
    created_at,
    nullable_ts,
    session_mode,
    sha256,
    sha256_check,
    updated_at,
    uuid_pk,
)


class LearningSession(Base):
    """One continuous working period.

    Note there are no summary / key_points / open_threads columns: the spec
    puts them here *and* on session_summaries, which is two sources of truth
    for the same three values. session_summaries wins -- it is what the
    session-start query reads, and it can be regenerated. See DIVERGENCES (B5).
    """

    __tablename__ = "learning_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    learner_subject_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False
    )
    mode: Mapped[str] = mapped_column(session_mode, nullable=False)
    focus_concept_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("concepts.id", ondelete="SET NULL")
    )
    target_duration_minutes: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("90")
    )
    started_at: Mapped[dt.datetime] = created_at()
    ended_at: Mapped[dt.datetime | None] = nullable_ts()
    end_reason: Mapped[str | None] = mapped_column(Text)
    #: Denormalised from the session's traces for an O(1) "what did this cost".
    #: Maintained by a trigger on agent_traces insert -- session_turns carries
    #: no cost column, so the spec's trigger target cannot work. (A4)
    total_cost_usd: Mapped[float] = mapped_column(
        Numeric(10, 4), nullable=False, server_default=text("0")
    )
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    turns: Mapped[list[SessionTurn]] = relationship(back_populates="session")

    __table_args__ = (
        ForeignKeyConstraint(
            ["learner_subject_id", "user_id"],
            ["learner_subjects.id", "learner_subjects.user_id"],
            ondelete="CASCADE",
            name="fk_learning_sessions_enrollment",
        ),
        CheckConstraint(
            "end_reason IN ('completed', 'time_up', 'learner_stop', "
            "'idle_timeout', 'error')",
            name="end_reason",
        ),
        Index("idx_learning_sessions_user_time", "user_id", text("started_at DESC")),
        Index(
            "idx_learning_sessions_active",
            "user_id",
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "idx_learning_sessions_learner_subject",
            "learner_subject_id",
            text("started_at DESC"),
        ),
        Index("idx_learning_sessions_focus", "focus_concept_id"),
    )


class SessionTurn(Base):
    """The message log: every LLM interaction, learner input, tool invocation."""

    __tablename__ = "session_turns"

    id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("learning_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Unique per session. Assigned in-transaction from MAX(turn_index) + 1
    #: rather than an in-memory counter -- load-balancer affinity pins a
    #: machine, not a process, so two workers can serve one session. (C7)
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(agent_identity, nullable=False)
    #: Opaque to the schema; shapes are per-actor in the Agent Runtime spec.
    #: learner: {"kind": "utterance", "text": ...} or {"kind": "primitive", ...}.
    #: tutor: {"kind": "reply", "text": ..., "next_intent": "await_response"}.
    input: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    output: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: Denormalised from input when the learner invoked a tutorial primitive
    #: ('explain_differently', 'prove_it_to_me', ...). A column because
    #: "how often does this learner say I'm lost" is a common query.
    primitive: Mapped[str | None] = mapped_column(Text)
    concept_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("concepts.id", ondelete="SET NULL")
    )
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("content_artifacts.id", ondelete="SET NULL")
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = created_at()

    session: Mapped[LearningSession] = relationship(back_populates="turns")
    trace: Mapped[AgentTrace | None] = relationship(back_populates="turn", uselist=False)

    __table_args__ = (
        UniqueConstraint("session_id", "turn_index", name="uq_session_turns_index"),
        Index(
            "idx_session_turns_concept",
            "concept_id",
            text("created_at DESC"),
            postgresql_where=text("concept_id IS NOT NULL"),
        ),
        Index("idx_session_turns_actor", "session_id", "actor"),
        Index("idx_session_turns_artifact", "artifact_id"),
    )


class AgentTrace(Base):
    """The raw LLM call record, one-to-one with any non-learner turn.

    Append-only; UPDATE/DELETE revoked from the application role. Retention is
    90 days by default (§10) because storing full prompts has privacy and cost
    implications; longer only when linked to a review queue item.
    """

    __tablename__ = "agent_traces"

    id: Mapped[uuid.UUID] = uuid_pk()
    session_turn_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("session_turns.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    #: Denormalised so the daily cost roll-up and the erasure sweep do not
    #: have to walk session_turns -> learning_sessions on a 90-day table. (C3)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent: Mapped[str] = mapped_column(agent_identity, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    #: Stored as-sent so a session can be replayed exactly.
    prompt_messages: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    #: SHA-256 of the system prompt at call time. Prompts are byte-stable for a
    #: given agent version, so this clusters traces by prompt version without
    #: storing the full prompt on every row.
    system_prompt_hash: Mapped[str] = sha256()
    completion: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    tools_used: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    stop_reason: Mapped[str | None] = mapped_column(Text)
    #: The API reports input_tokens as the *uncached remainder*. Total prompt
    #: size is tokens_in + cache_read_tokens + both cache-write columns.
    tokens_in: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    tokens_out: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: Cache writes are split by TTL because they are priced differently
    #: (5-minute writes bill at 1.25x base input, 1-hour writes at 2x). A
    #: single column cannot re-derive cost once both TTLs are in use. (C3)
    cache_write_5m_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cache_write_1h_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: Server-side tool calls are billed per request, not per token, so
    #: token columns alone cannot reconstruct the cost of a search-heavy call.
    server_tool_use: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    langfuse_trace_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = created_at()

    turn: Mapped[SessionTurn] = relationship(back_populates="trace")

    __table_args__ = (
        sha256_check("system_prompt_hash"),
        # An agent trace is by definition not a learner turn.
        CheckConstraint("agent <> 'learner'", name="agent_not_learner"),
        Index("idx_agent_traces_created", text("created_at DESC")),
        Index("idx_agent_traces_model_created", "model", text("created_at DESC")),
        Index("idx_agent_traces_user_created", "user_id", text("created_at DESC")),
    )

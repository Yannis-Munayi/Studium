"""§6.12 Operational: cost and audit.

Uncontrolled LLM cost is the failure mode most likely to end the project, so
cost tracking is first-class: every call writes a trace, and a daily roll-up
supports budget enforcement without scanning traces at query time.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Computed,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, created_at, updated_at, uuid_pk


class CostLedger(Base):
    """Daily roll-up of LLM cost per user per model (spec v1.1 §6.12).

    Five cost categories rather than one. v1.0 summed only ``agent_traces``,
    leaving content generation, ingestion, summarisation and grading out of the
    number budget enforcement reads -- plausibly the dominant spend at MVP,
    where content is generated once and read many times. Keeping the categories
    as separate columns answers "what is the money going on" without a second
    query, and ``cost_usd`` is generated from them so the total cannot drift
    from its parts.

    Attribution rules for the four non-trace sources are in
    ``studium.jobs.cost_rollup``.
    """

    __tablename__ = "cost_ledger"

    id: Mapped[uuid.UUID] = uuid_pk()
    #: SET NULL so the de-identified aggregate survives erasure (§10 step 2).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    day: Mapped[dt.date] = mapped_column(Date, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_in: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    tokens_out: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    #: Split by TTL because the two price differently -- a 5-minute cache write
    #: bills at 1.25x base input, a 1-hour write at 2x. One column cannot
    #: re-derive cost once both TTLs are in use.
    cache_write_5m_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    cache_write_1h_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )

    cost_agent_usd: Mapped[float] = mapped_column(
        Numeric(12, 4), nullable=False, server_default=text("0")
    )
    cost_content_usd: Mapped[float] = mapped_column(
        Numeric(12, 4), nullable=False, server_default=text("0")
    )
    cost_ingestion_usd: Mapped[float] = mapped_column(
        Numeric(12, 4), nullable=False, server_default=text("0")
    )
    cost_summary_usd: Mapped[float] = mapped_column(
        Numeric(12, 4), nullable=False, server_default=text("0")
    )
    cost_grading_usd: Mapped[float] = mapped_column(
        Numeric(12, 4), nullable=False, server_default=text("0")
    )
    #: Generated, never written. An ON CONFLICT update that tried to set this
    #: is rejected by Postgres, which is the intended guard: the total is
    #: always exactly the sum of its parts.
    cost_usd: Mapped[float] = mapped_column(
        Numeric(12, 4),
        Computed(
            "cost_agent_usd + cost_content_usd + cost_ingestion_usd"
            " + cost_summary_usd + cost_grading_usd",
            persisted=True,
        ),
    )

    session_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    __table_args__ = (
        # NULLS NOT DISTINCT so de-identified rows still merge per (day, model)
        # instead of accumulating one duplicate per erased user.
        UniqueConstraint(
            "user_id",
            "day",
            "model",
            name="uq_cost_ledger_day",
            postgresql_nulls_not_distinct=True,
        ),
        Index(
            "idx_cost_ledger_user_day",
            "user_id",
            text("day DESC"),
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index("idx_cost_ledger_day", text("day DESC")),
    )


class UserBudgetCap(Base):
    """Per-user spend limits, checked on session start.

    Defaults match the $75/learner-month soft ceiling with 33% headroom before
    hard-blocking. The reviewer's own caps are reasonably higher; override per
    user with an UPDATE.
    """

    __tablename__ = "user_budget_caps"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    daily_soft_usd: Mapped[float] = mapped_column(
        Numeric(10, 2), nullable=False, server_default=text("5.00")
    )
    daily_hard_usd: Mapped[float] = mapped_column(
        Numeric(10, 2), nullable=False, server_default=text("8.00")
    )
    monthly_soft_usd: Mapped[float] = mapped_column(
        Numeric(10, 2), nullable=False, server_default=text("75.00")
    )
    monthly_hard_usd: Mapped[float] = mapped_column(
        Numeric(10, 2), nullable=False, server_default=text("100.00")
    )
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()


class AuditLog(Base):
    """Privileged actions, for accountability.

    Not a general-purpose event log: only actions that mutate another user's
    data, retire content, adjust rubric criteria, or override gating.
    Append-only; UPDATE and DELETE revoked from the application role.
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = uuid_pk()
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    ip_address: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[dt.datetime] = created_at()

    __table_args__ = (
        Index("idx_audit_log_actor_time", "actor_user_id", text("created_at DESC")),
        Index(
            "idx_audit_log_target", "target_type", "target_id", text("created_at DESC")
        ),
    )

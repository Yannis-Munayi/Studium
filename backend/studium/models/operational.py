"""§6.12 Operational: cost and audit, plus infrastructure §12's retention log.

Uncontrolled LLM cost is the failure mode most likely to end the project, so
cost tracking is first-class: every call writes a trace, and a daily roll-up
supports budget enforcement without scanning traces at query time.

The two retention tables at the bottom belong to the infrastructure spec
(§12.2, §12.4) rather than to the data layer, and are here because they are
operational in exactly the sense the rest of this module is: nobody learns
anything from them, and the reviewer reads them when a number looks wrong.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
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

from .base import Base, created_at, nullable_ts, updated_at, uuid_pk


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


class RetentionAction(Base):
    """What the nightly retention worker deleted (infrastructure §12.2).

    "Audit trail for any question of 'why did that data go away' -- the answer
    is either 'retention policy X, on date Y, deleted N rows' or 'something
    else happened, investigate.'" The second half is the useful one: an empty
    result for a window is itself evidence, which is only true if the worker
    writes a row *per policy per pass* rather than only when it deleted
    something. It does -- ``rows_deleted = 0`` is a recorded observation, not a
    skipped one.

    **Append-only, and revoked from the application role in migration 0012.**
    An audit trail the process under audit can rewrite answers nothing. The
    worker connects as ``studium_owner`` like the deletes it is recording.

    **``metadata`` carries the run's structure.** Infrastructure §18 counts a
    "``metadata`` field with structured provenance" as its own item in the data
    layer v1.2 batch, and it is what keeps the column list identical to §12.2's
    DDL while still answering "did last night's pass complete": every row from
    one pass shares a ``run_id``, and each carries the policy window and
    predicate that produced it. A column for each would have been a wider
    divergence from a DDL the spec wrote out in full. See
    DIVERGENCES-INFRASTRUCTURE (N2).
    """

    __tablename__ = "retention_actions"

    id: Mapped[uuid.UUID] = uuid_pk()
    #: §12.2's own name for the timestamp. Serves as the creation time; there
    #: is no separate created_at, because a retention action is an instant.
    ran_at: Mapped[dt.datetime] = created_at()
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    rows_deleted: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    action_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        # Negative counts would mean the worker mis-read a rowcount, which is
        # worth failing on rather than storing: the whole value of this table
        # is that its numbers can be trusted without re-deriving them.
        CheckConstraint("rows_deleted >= 0", name="rows_deleted_non_negative"),
        CheckConstraint("duration_ms >= 0", name="duration_non_negative"),
        Index("idx_retention_actions_recent", text("ran_at DESC")),
        # §12.2's index serves "what happened last night"; this one serves
        # "what has ever been deleted from this table", which is the question a
        # reviewer chasing one missing row actually asks.
        Index("idx_retention_actions_table", "table_name", text("ran_at DESC")),
    )


class RetentionHold(Base):
    """A row the retention worker must not delete (infrastructure §12.4).

    "Occasionally a specific dataset needs to be retained beyond policy for
    legitimate reasons (evidence in a rights dispute, research study,
    longitudinal analysis)."

    **Released, never deleted.** A hold that vanishes when lifted destroys the
    only record that the data was deliberately kept -- which is the thing a
    rights dispute would later ask about. ``released_at`` retires it; the row
    stays.

    **Holds apply only to tables the worker deletes from directly.** Several
    policies delete a parent and let Postgres cascade
    (``learning_sessions`` reaches turns, traces, summaries and retrieval
    checks that way), and a cascade runs with the referencing table's owner
    privileges and consults nothing. A hold placed on a cascade-reached row
    would read as protection and provide none, so
    ``studium.ops.retention.place_hold`` refuses those tables by name and says
    to hold the parent instead. See DIVERGENCES-INFRASTRUCTURE (N3).
    """

    __tablename__ = "retention_holds"

    id: Mapped[uuid.UUID] = uuid_pk()
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    row_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    #: Required and free-text. §12.4's examples are all reasons a human has to
    #: write down; a hold with no stated reason is indistinguishable from one
    #: placed by accident, and the whole point is that someone can later decide
    #: it has expired.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    placed_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    placed_at: Mapped[dt.datetime] = created_at()
    released_at: Mapped[dt.datetime | None] = nullable_ts()
    released_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    __table_args__ = (
        CheckConstraint("length(reason) > 0", name="reason_not_empty"),
        CheckConstraint(
            "released_at IS NULL OR released_at >= placed_at",
            name="released_after_placed",
        ),
        # The lookup the worker does once per policy per pass. Partial on the
        # live holds because a released one must not slow down the query whose
        # answer is "is this row protected right now".
        Index(
            "idx_retention_holds_active",
            "table_name",
            "row_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
        # Leading index for the FK, per §7. Also the "what am I still holding"
        # query when an operator's access is reviewed.
        Index("idx_retention_holds_placed_by", "placed_by"),
    )

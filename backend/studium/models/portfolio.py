"""§6.10 Portfolio: the record of substantive work the learner produced.

Items are hash-chained so "did the learner actually do this work" is
verifiable. The spec describes a per-learner-subject chain but gives it no
ordering column, so concurrent inserts would fork it silently -- ``chain_index``
makes the order explicit and unique. See DIVERGENCES (C14).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import (
    Base,
    created_at,
    portfolio_item_kind,
    sha256,
    sha256_check,
    uuid_pk,
)


class PortfolioItem(Base):
    __tablename__ = "portfolio_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    learner_subject_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False
    )
    #: Position in this enrollment's hash chain, 0-based and gapless.
    chain_index: Mapped[int] = mapped_column(Integer, nullable=False)
    concept_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("concepts.id", ondelete="SET NULL")
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("learning_sessions.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(portfolio_item_kind, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    #: markdown or code
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: for code
    language: Mapped[str | None] = mapped_column(Text)
    #: for larger artifacts (notebook exports)
    storage_path: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str] = sha256()
    #: Revision chains: a draft essay revised three times is four rows.
    parent_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("portfolio_items.id", ondelete="SET NULL")
    )
    #: Manifest: previous item's SHA-256, this item's SHA-256, session id,
    #: user id, timestamp, signed with a server-held key. Key management
    #: belongs to the Infrastructure spec.
    signature: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["learner_subject_id", "user_id"],
            ["learner_subjects.id", "learner_subjects.user_id"],
            ondelete="CASCADE",
            name="fk_portfolio_items_enrollment",
        ),
        UniqueConstraint(
            "learner_subject_id", "chain_index", name="uq_portfolio_chain"
        ),
        sha256_check("content_sha256"),
        Index("idx_portfolio_user_created", "user_id", text("created_at DESC")),
        Index(
            "idx_portfolio_concept",
            "concept_id",
            text("created_at DESC"),
            postgresql_where=text("concept_id IS NOT NULL"),
        ),
        Index(
            "idx_portfolio_session",
            "session_id",
            postgresql_where=text("session_id IS NOT NULL"),
        ),
        Index("idx_portfolio_parent", "parent_item_id"),
    )

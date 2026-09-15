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
    Boolean,
    CheckConstraint,
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

#: The two ``portfolio_item_kind`` values that are credentials rather than
#: work (evaluation §12.1, added to the enum by migration 0011). Defined here
#: rather than in ``studium.eval.credentials`` because migration 0013's CHECK
#: constraint is written against it: the schema is now the thing that decides
#: what a credential is, and one definition it can be checked against beats
#: three that agree until they don't.
CREDENTIAL_KINDS: tuple[str, ...] = ("assessment_pass", "subject_completion")


class PortfolioItem(Base):
    __tablename__ = "portfolio_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Nullable only so a credential can outlive the enrollment it was earned
    #: under (amendment v1.2.1 §3.1, migration 0013). Every item is written
    #: with one; erasure nulls it on the credentials it retains, which is also
    #: what satisfies the MATCH SIMPLE composite key below once the
    #: ``learner_subjects`` row is gone.
    learner_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
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
    #: Redundant with ``kind`` by construction, and a CHECK constraint keeps it
    #: that way (migration 0013). It exists so §10's erasure can say which
    #: items survive in a predicate that cannot be misread, rather than
    #: repeating an enum membership test in the one procedure where getting it
    #: wrong destroys a credential permanently.
    is_credential: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
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
        # Migration 0013. The flag is a denormalisation of ``kind`` and this is
        # what stops it becoming a second, disagreeing answer to "is this a
        # credential" -- which erasure reads, and which decides whether the row
        # survives.
        CheckConstraint(
            "is_credential = (kind::text IN ("
            + ", ".join(f"'{k}'" for k in CREDENTIAL_KINDS)
            + "))",
            name="is_credential_matches_kind",
        ),
        Index("idx_portfolio_user_created", "user_id", text("created_at DESC")),
        # Erasure's "which of this learner's items survive", and the monthly
        # credential audit's "what is currently retained".
        Index(
            "idx_portfolio_credentials",
            "user_id",
            text("created_at DESC"),
            postgresql_where=text("is_credential"),
        ),
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

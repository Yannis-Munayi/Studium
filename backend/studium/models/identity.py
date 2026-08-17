"""§6.1 Identity and users."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import CITEXT, INET, JSONB
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

#: The synthetic account that owns cost with no requesting learner behind it --
#: Curator pre-generation, corpus imports. Spec v1.1 §6.12 attributes such cost
#: to "the system synthetic user" without defining one, and it cannot be booked
#: to NULL because a NULL owner in cost_ledger already means "post-erasure
#: aggregate". Seeded by migration 0008. See DIVERGENCES.md (V3).
SYSTEM_USER_ID = uuid.UUID("00000000-0000-7000-8000-000000000001")
SYSTEM_USER_EMAIL = "system@studium.internal"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    #: NULL when the account is OAuth-only.
    password_hash: Mapped[str | None] = mapped_column(Text)
    auth_provider: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'local'")
    )
    #: Distinguishes the human-in-the-loop reviewer from ordinary learners;
    #: gates the review queue and admin endpoints. 'admin' is unused at MVP.
    role: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'learner'")
    )
    locale: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'en-CA'")
    )
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'America/Toronto'")
    )
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()
    deleted_at: Mapped[dt.datetime | None] = nullable_ts()

    profile: Mapped[UserProfile | None] = relationship(
        back_populates="user", uselist=False, passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint(
            "auth_provider IN ('local', 'google', 'github')", name="auth_provider"
        ),
        CheckConstraint("role IN ('learner', 'reviewer', 'admin')", name="role"),
        # Spec §6.1 has a plain UNIQUE on email. That holds the address for the
        # 30-day soft-delete window in §10, so a user who closes an account and
        # re-registers is blocked for a month. Partial-unique instead, which
        # also subsumes the spec's idx_users_email_active. See DIVERGENCES (C12).
        Index(
            "idx_users_email_active",
            "email",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class UserProfile(Base):
    """One-to-one with users. Split off because profile data is larger,
    mutates more often, and is not needed on every auth check."""

    __tablename__ = "user_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: markdown, user-authored
    background_notes: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    #: markdown, user-authored
    stated_goals: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    #: Shape documented for consumers, validated by Pydantic at the API edge,
    #: deliberately not constrained here so adding a key needs no migration.
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    accessibility: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()

    user: Mapped[User] = relationship(back_populates="profile")


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: SHA-256 of the session token. The raw token exists only in the cookie.
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[dt.datetime] = created_at()
    expires_at: Mapped[dt.datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    revoked_at: Mapped[dt.datetime | None] = nullable_ts()

    __table_args__ = (
        CheckConstraint("length(token_hash) = 64", name="token_hash_len"),
        Index(
            "idx_auth_sessions_user_active",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "idx_auth_sessions_expires",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

"""§6.3 Corpus: source materials.

The ground truth agents cite from. Chunking strategy, embedding model choice,
and hybrid search ranking belong to the Retrieval spec (subsystem 3); this
module only says the rows exist and how they are reached.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import (
    Base,
    artifact_status,
    created_at,
    license_kind,
    nullable_ts,
    sha256,
    sha256_check,
    updated_at,
    uuid_pk,
)

#: Voyage-3 dimensionality. Changing the embedding model means a new table
#: variant, background re-embed, read cutover, then drop -- not an ALTER.
EMBEDDING_DIM = 1024


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = uuid_pk()
    subject_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    authors: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    publication_year: Mapped[int | None] = mapped_column(Integer)
    #: Not a suggestion. A 'user_uploaded' source is readable only by its
    #: uploader or an admin -- enforced in the query layer, not RLS, because
    #: the check depends on session context the database does not hold.
    license: Mapped[str] = mapped_column(license_kind, nullable=False)
    license_notes: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Opaque to the schema: a Fly volume path at MVP, an R2 object key later.
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = sha256()
    page_count: Mapped[int | None] = mapped_column(Integer)
    token_count: Mapped[int | None] = mapped_column(Integer)
    ingested_at: Mapped[dt.datetime | None] = nullable_ts()
    status: Mapped[str] = mapped_column(
        artifact_status, nullable=False, server_default=text("'draft'")
    )
    created_at: Mapped[dt.datetime] = created_at()
    updated_at: Mapped[dt.datetime] = updated_at()
    deleted_at: Mapped[dt.datetime | None] = nullable_ts()

    chunks: Mapped[list[SourceChunk]] = relationship(
        back_populates="source", passive_deletes=True
    )

    __table_args__ = (
        # Prevents re-uploading the same text into one subject. Cross-subject
        # duplicates are allowed: TAPL can reasonably ground both a lambda
        # calculus subject and a type theory subject.
        UniqueConstraint("subject_id", "content_sha256", name="uq_sources_subject_hash"),
        sha256_check("content_sha256"),
        Index(
            "idx_sources_subject_status",
            "subject_id",
            "status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Unconditional, unlike the partial index above: a subject cascade must
        # find soft-deleted rows too, or it degrades to a sequential scan. (A5)
        Index("idx_sources_subject", "subject_id"),
        Index(
            "idx_sources_uploaded_by",
            "uploaded_by",
            postgresql_where=text("uploaded_by IS NOT NULL"),
        ),
    )


class SourceChunk(Base):
    """Retrievable units produced by the ingestion pipeline."""

    __tablename__ = "source_chunks"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text_: Mapped[str] = mapped_column("text", Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    #: Ordered breadcrumbs, e.g. ["Ch 3", "3.2", "3.2.1"]. JSONB rather than
    #: TEXT[] so it can carry hierarchical annotations without a rigid shape.
    section_path: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[dt.datetime] = created_at()

    source: Mapped[Source] = relationship(back_populates="chunks")
    embedding_row: Mapped[SourceChunkEmbedding | None] = relationship(
        back_populates="chunk", uselist=False, passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("source_id", "chunk_index", name="uq_source_chunks_index"),
        Index("idx_source_chunks_source", "source_id"),
    )


class SourceChunkEmbedding(Base):
    """One-to-one with source_chunks, kept separate so re-embedding under a new
    model does not lock the chunk table."""

    __tablename__ = "source_chunk_embeddings"

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("source_chunks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    #: e.g. 'voyage-3.1'
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = created_at()

    chunk: Mapped[SourceChunk] = relationship(back_populates="embedding_row")

    __table_args__ = (
        # HNSW with pgvector defaults, appropriate to a few million chunks.
        # The Retrieval spec revisits m/ef_construction as the corpus grows.
        Index(
            "idx_source_chunk_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

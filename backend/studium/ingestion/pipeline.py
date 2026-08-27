"""The ingestion pipeline (ingestion §6).

    upload -> extract -> normalize -> chunk -> embed

Each stage reads its input from the schema or from storage, does one
transformation, writes its output in one transaction, marks its
``ingestion_jobs`` row done, and enqueues the next. On failure it retries per
its own policy and then writes to ``ingestion_review_queue`` with the specific
failure named -- never a silent drop, and never a generic "ingestion failed"
that a reviewer cannot act on.

Two properties hold across every stage and are worth stating once.

**Every stage is deterministic.** Re-running one against unchanged input
produces the same output, including the same failure. That is what makes the
retry policies safe (a retry cannot make things worse) and what makes §14's
"every failure is deterministic until the underlying condition changes" true
rather than aspirational.

**The pipeline finishes without a human, and publishes nothing.** §3: a source
that has been through all five stages is fully indexed and still ``draft``.
Automation gets the corpus ready; a reviewer decides it is fit to teach from.
Nothing here sets ``status = 'active'``.

The embed stage is the exception to "does one transformation": it enqueues, and
retrieval §8's worker executes. That boundary is deliberate -- embedding costs
money per token and belongs with the subsystem that owns the provider budget.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.asyncdb import jsonable, run_db
from studium.retrieval.chunking import Block, chunk_blocks, count_tokens

from . import storage
from .extract import (
    MIN_DOCUMENT_TOKENS,
    ExtractedContent,
    ExtractionError,
    ExtractorUnavailable,
    get_extractor,
)
from .licensing import HONEST_DEFAULT, flag_pending
from .normalize import normalize_document
from .provenance import ingestion_writer, require
from .queue import flag

log = logging.getLogger(__name__)

#: §14. 200MB. Generous for a textbook and small enough that one upload cannot
#: fill the Fly volume.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

#: Every PDF starts with this. Checked instead of trusting the filename or the
#: client's content-type, both of which the uploader controls.
PDF_MAGIC = b"%PDF-"

#: §6.3. Extraction is deterministic, so a retry only helps against a transient
#: fault (memory pressure, a blipped read). Two of them, then flag.
EXTRACT_MAX_ATTEMPTS = 3
#: §6.4, §6.5. Deterministic against local state; one retry covers an I/O or
#: connection blip and nothing else would be fixed by a third.
NORMALIZE_MAX_ATTEMPTS = 2
CHUNK_MAX_ATTEMPTS = 2


class UploadRejected(ValueError):
    """An upload the system will not accept. Carries the §14 HTTP status."""

    status_code = 400


class DuplicateSource(UploadRejected):
    status_code = 409

    def __init__(self, existing_id: uuid.UUID, title: str) -> None:
        self.existing_id = existing_id
        super().__init__(
            f"this content is already ingested as source {existing_id} ({title!r})"
        )


class SourceTooLarge(UploadRejected):
    status_code = 413

    def __init__(self, size: int) -> None:
        super().__init__(
            f"{size} bytes exceeds the {MAX_UPLOAD_BYTES}-byte upload limit"
        )


class UnsupportedFormat(UploadRejected):
    status_code = 415

    def __init__(self, detail: str) -> None:
        super().__init__(f"only PDF sources are supported: {detail}")


@dataclass(frozen=True, slots=True)
class StageResult:
    kind: str
    source_id: uuid.UUID
    status: str
    detail: str = ""
    #: Review queue rows this stage created.
    flagged: tuple[uuid.UUID, ...] = ()
    #: Stage-specific counts, for a CLI to report and a test to assert on.
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "done"


# --- §6.2 upload -----------------------------------------------------------


@ingestion_writer
def upload(
    session: Session,
    *,
    data: bytes,
    filename: str,
    subject_id: uuid.UUID,
    uploaded_by: uuid.UUID | None,
    title: str | None = None,
) -> uuid.UUID:
    """Accept a PDF and enqueue its extraction. Returns the new source id.

    ``uploaded_by`` has no default and rejects ``None``. §13.2 names this case
    exactly: defaulting it to a system user when no user is in the request
    context produces a corpus where "who put this here" is unanswerable for
    precisely the rows where it matters -- an unattributed source cannot be
    budgeted against, and cannot be taken down on the uploader's request
    because nobody knows whose it was. A CLI import passes ``SYSTEM_USER_ID``
    explicitly, which is a claim someone made rather than one the code invented.

    The title likewise is not guessed from the filename. §11's lesson is about
    licenses, but the same reasoning holds here: ``1_TuringMachines.pdf`` is not
    a title, and storing it as one produces a citation that reads like a
    bibliographic record and is not.
    """
    require("uploaded_by", uploaded_by, detail="an upload must be attributable (§13.2)")

    if len(data) > MAX_UPLOAD_BYTES:
        raise SourceTooLarge(len(data))
    if not data.startswith(PDF_MAGIC):
        raise UnsupportedFormat(f"{filename!r} does not begin with %PDF-")

    content_sha256 = hashlib.sha256(data).hexdigest()

    # §6.2 step 1. Checked before the row is written rather than relying on the
    # unique constraint, so the error can name the existing source -- which is
    # what the uploader needs to know, and what an IntegrityError cannot say.
    existing = session.execute(
        sql(
            """
            SELECT id, title FROM sources
             WHERE subject_id = :subject_id
               AND content_sha256 = :hash
               AND deleted_at IS NULL
            """
        ),
        {"subject_id": subject_id, "hash": content_sha256},
    ).one_or_none()
    if existing is not None:
        raise DuplicateSource(existing.id, existing.title)

    metadata = _pdf_metadata(data)
    resolved_title = title or metadata.get("title") or Path(filename).stem

    source_id = session.execute(
        sql(
            """
            INSERT INTO sources
                (subject_id, title, authors, license, uploaded_by,
                 storage_path, content_sha256, page_count, status)
            VALUES
                (:subject_id, :title, :authors, CAST(:license AS license_kind),
                 :uploaded_by, :storage_path, :hash, :page_count, 'draft')
            RETURNING id
            """
        ),
        {
            "subject_id": subject_id,
            "title": resolved_title,
            "authors": metadata.get("authors") or [],
            # §6.2 step 3 and §11.1. Not negotiable and not a parameter: there
            # is no code path in ingestion that sets a new upload to anything
            # else, which Tier 1 asserts by reading this module.
            "license": HONEST_DEFAULT,
            "uploaded_by": uploaded_by,
            "storage_path": "",
            "hash": content_sha256,
            "page_count": metadata.get("page_count"),
        },
    ).scalar_one()

    # The path is derived from the id, so it cannot be written in the same
    # INSERT that generates the id. Storing the file first would leave an
    # orphan on disk if the UPDATE failed; this way a crash leaves a row whose
    # storage_path is empty, which the extract stage reports as a missing file
    # rather than silently extracting nothing.
    path = storage.store_pdf(source_id, data)
    session.execute(
        sql("UPDATE sources SET storage_path = :path WHERE id = :id"),
        {"path": str(path), "id": source_id},
    )

    flag_pending(session, source_id=source_id, title=resolved_title)
    enqueue(session, source_id=source_id, kind="extract_text")

    log.info("source %s uploaded (%s, %d bytes)", source_id, resolved_title, len(data))
    return source_id


def _pdf_metadata(data: bytes) -> dict[str, Any]:
    """Title, authors and page count via pypdf (§4 "PDF metadata extraction").

    A separate library from the text extractor on purpose: this runs before
    extraction, to populate the row, and it must not fail the upload. A PDF
    with a corrupt metadata dictionary is still a PDF worth ingesting, so
    anything unreadable here yields an empty dict and the reviewer fills the
    gap.
    """
    import io

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        info = reader.metadata or {}
        authors = str(info.get("/Author") or "").strip()
        return {
            "title": (str(info.get("/Title") or "").strip() or None),
            "authors": [authors] if authors else [],
            "page_count": len(reader.pages),
        }
    except Exception as exc:  # noqa: BLE001 -- any parse failure is advisory
        log.warning("could not read PDF metadata: %s", exc)
        return {}


# --- job plumbing ----------------------------------------------------------


def enqueue(
    session: Session,
    *,
    source_id: uuid.UUID,
    kind: str,
    payload: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Insert a pending ``ingestion_jobs`` row for the next stage."""
    import json

    return session.execute(
        sql(
            """
            INSERT INTO ingestion_jobs (source_id, kind, payload)
            VALUES (:source_id, CAST(:kind AS ingestion_job_kind),
                    CAST(:payload AS jsonb))
            RETURNING id
            """
        ),
        {
            "source_id": source_id,
            "kind": kind,
            "payload": json.dumps(jsonable(payload or {})),
        },
    ).scalar_one()


def claim(session: Session, *, kind: str | None = None) -> dict[str, Any] | None:
    """Claim one pending job.

    ``FOR UPDATE SKIP LOCKED`` per the data layer's note on this table: two
    workers running the same stage take different rows instead of one blocking
    on the other, and a worker that dies mid-job leaves the row claimed rather
    than lost.
    """
    row = session.execute(
        sql(
            """
            UPDATE ingestion_jobs
               SET status = 'running',
                   attempt = attempt + 1,
                   started_at = NOW()
             WHERE id = (
                   SELECT id FROM ingestion_jobs
                    WHERE status = 'pending'
                      AND (:no_kind OR kind = CAST(:kind AS ingestion_job_kind))
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
             )
            RETURNING id, source_id, kind, attempt, payload
            """
        ),
        {"no_kind": kind is None, "kind": kind},
    ).one_or_none()
    if row is None:
        return None
    return {
        "id": row.id,
        "source_id": row.source_id,
        "kind": row.kind,
        "attempt": int(row.attempt),
        "payload": row.payload or {},
    }


def finish(
    session: Session, job_id: uuid.UUID, *, status: str, error: str | None = None
) -> None:
    session.execute(
        sql(
            """
            UPDATE ingestion_jobs
               SET status = CAST(:status AS ingestion_job_status),
                   finished_at = NOW(),
                   error = :error
             WHERE id = :id
            """
        ),
        {"id": job_id, "status": status, "error": error},
    )


def release(session: Session, job_id: uuid.UUID) -> None:
    """Put a claimed job back for another attempt (§6.3 retry policy)."""
    session.execute(
        sql(
            """
            UPDATE ingestion_jobs
               SET status = 'pending', started_at = NULL
             WHERE id = :id
            """
        ),
        {"id": job_id},
    )


# --- §6.3 extract ----------------------------------------------------------


async def run_extract(
    source_id: uuid.UUID, *, extractor_name: str | None = None
) -> StageResult:
    """Extract text and structure, then enqueue normalisation."""
    row = await run_db(lambda s: _source_row(s, source_id))
    if row is None:
        raise LookupError(f"no source {source_id}")

    extractor = get_extractor(extractor_name)
    path = Path(row["storage_path"] or storage.source_pdf_path(source_id))

    try:
        content = await extractor.extract(path)
    except ExtractorUnavailable:
        # Deliberately not caught below. A missing extractor is an operator
        # problem, not a document problem: the source is fine, a retry will not
        # help, and no reviewer can action it. Filing it as extractor_failure
        # would put one queue row in front of a reviewer per source in the
        # batch -- hundreds of identical rows burying the real failures, all
        # saying something only whoever deployed the process can fix.
        raise
    except ExtractionError as exc:
        # Bound to a local before the lambda closes over it. Python deletes the
        # `as exc` name when the except block exits, so a closure referring to
        # it is only safe while the block is still on the stack -- true here by
        # accident of the await landing inside it, and false the moment anyone
        # defers the call. The failure would be a NameError on the error path,
        # which is the worst place to find one.
        detail = f"extraction failed: {exc}"
        flagged = await run_db(
            lambda s: flag(
                s,
                flag_source="extractor_failure",
                source_id=source_id,
                reason=detail,
                payload={"extractor": extractor.version, "path": str(path)},
            )
        )
        return StageResult(
            kind="extract_text",
            source_id=source_id,
            status="failed",
            detail=detail,
            flagged=(flagged,),
        )

    tokens = count_tokens(content.text)

    # §6.3 step 4. Empty and near-empty are the same failure with different
    # causes: a scanned PDF gives nothing, a mostly-image PDF gives a caption.
    # Both leave a source that is nominally ingested and retrieves nothing, so
    # both stop here rather than producing four chunks of page furniture.
    if content.is_empty or tokens < MIN_DOCUMENT_TOKENS:
        reason = (
            f"extraction produced {tokens} tokens (minimum {MIN_DOCUMENT_TOKENS}); "
            f"this is usually a scanned PDF, which needs OCR (v2) rather than "
            f"a text extractor"
        )
        flagged = await run_db(
            lambda s: flag(
                s,
                flag_source="extractor_failure",
                source_id=source_id,
                reason=reason,
                payload={
                    "extractor": extractor.version,
                    "tokens": tokens,
                    "pages": len(content.pages),
                },
            )
        )
        return StageResult(
            kind="extract_text",
            source_id=source_id,
            status="failed",
            detail=reason,
            flagged=(flagged,),
            counts={"tokens": tokens, "pages": len(content.pages)},
        )

    storage.write_jsonl(
        storage.extracted_path(source_id), _extracted_records(content)
    )

    await run_db(
        lambda s: _record_extraction(
            s, source_id=source_id, extractor_version=extractor.version, content=content
        )
    )
    await run_db(lambda s: enqueue(s, source_id=source_id, kind="normalize"))

    return StageResult(
        kind="extract_text",
        source_id=source_id,
        status="done",
        detail=f"{len(content.pages)} pages, {tokens} tokens",
        counts={"pages": len(content.pages), "tokens": tokens},
    )


def _extracted_records(content: ExtractedContent) -> list[dict[str, Any]]:
    return [
        {
            "page_number": page.page_number,
            "text": page.text,
            "section_hierarchy": list(page.section_hierarchy),
            "tables": [table.as_text() for table in page.tables],
            "confidence": page.confidence,
        }
        for page in content.pages
    ]


@ingestion_writer
def _record_extraction(
    session: Session,
    *,
    source_id: uuid.UUID,
    extractor_version: str | None,
    content: ExtractedContent,
) -> None:
    """Stamp the extractor onto the source (§5 addition 2).

    ``extractor_version`` is attribution-critical and required: a source whose
    text came from an unknown extractor cannot be included in or excluded from
    a corpus-wide re-extraction, so the only safe treatment is to redo it --
    which means paying to re-embed a book that may not have needed it.
    """
    require("extractor_version", extractor_version)
    session.execute(
        sql(
            """
            UPDATE sources
               SET extractor_version = :version,
                   page_count = :page_count
             WHERE id = :id
            """
        ),
        {
            "version": extractor_version,
            "page_count": len(content.pages),
            "id": source_id,
        },
    )


# --- §6.4 normalize --------------------------------------------------------


async def run_normalize(source_id: uuid.UUID) -> StageResult:
    """Normalise the extracted text, then enqueue chunking."""
    records = list(storage.read_jsonl(storage.extracted_path(source_id)))
    pages = [(int(r["page_number"]), r.get("text") or "") for r in records]
    hierarchies = [r.get("section_hierarchy") or [] for r in records]

    result = normalize_document(pages, section_hierarchies=hierarchies)

    storage.write_jsonl(
        storage.normalized_path(source_id),
        [
            {
                "page_number": page.page_number,
                "text": page.text,
                "section_hierarchy": list(page.section_hierarchy),
                "tables": records[index].get("tables") or [],
                "confidence": records[index].get("confidence", 1.0),
            }
            for index, page in enumerate(result.pages)
        ],
    )

    flagged: list[uuid.UUID] = []

    def _write(session: Session) -> list[uuid.UUID]:
        _record_normalization(
            session, source_id=source_id, normalizer_version=result.version
        )
        ids: list[uuid.UUID] = []
        for warning in result.warnings:
            ids.append(
                flag(
                    session,
                    flag_source="normalizer_warning",
                    source_id=source_id,
                    reason=warning.detail,
                    payload=warning.as_payload(),
                )
            )
        enqueue(session, source_id=source_id, kind="chunk")
        return ids

    flagged = await run_db(_write)

    return StageResult(
        kind="normalize",
        source_id=source_id,
        status="done",
        detail=f"{len(result.pages)} pages, {len(result.warnings)} warning(s)",
        flagged=tuple(flagged),
        counts={"pages": len(result.pages), "warnings": len(result.warnings)},
    )


@ingestion_writer
def _record_normalization(
    session: Session, *, source_id: uuid.UUID, normalizer_version: str | None
) -> None:
    require("normalizer_version", normalizer_version)
    session.execute(
        sql("UPDATE sources SET normalizer_version = :version WHERE id = :id"),
        {"version": normalizer_version, "id": source_id},
    )


# --- §6.5 chunk ------------------------------------------------------------


async def run_chunk(source_id: uuid.UUID) -> StageResult:
    """Chunk the normalised text into ``source_chunks``, then enqueue embedding.

    The algorithm is retrieval's (§7 there, out of scope here). This stage
    turns pages into the ``Block`` sequence it consumes, calls it, and writes
    the rows.
    """
    records = list(storage.read_jsonl(storage.normalized_path(source_id)))
    blocks = _blocks_from_pages(records)
    chunks = chunk_blocks(blocks)

    if not chunks:
        # §14: rare, and it means normalisation stripped everything -- which is
        # an extraction-quality problem wearing a chunking costume, so it is
        # re-flagged as one rather than reported against the chunker.
        reason = (
            "chunking produced no chunks; normalisation stripped the document "
            "to nothing, which indicates the extracted text was page furniture"
        )
        flagged = await run_db(
            lambda s: flag(
                s,
                flag_source="extractor_failure",
                source_id=source_id,
                reason=reason,
                payload={"stage": "chunk", "pages": len(records)},
            )
        )
        return StageResult(
            kind="chunk",
            source_id=source_id,
            status="failed",
            detail=reason,
            flagged=(flagged,),
        )

    confidence_by_page = {
        int(r["page_number"]): float(r.get("confidence", 1.0)) for r in records
    }

    def _write(session: Session) -> tuple[int, list[uuid.UUID]]:
        written = _write_chunks(
            session,
            source_id=source_id,
            chunks=chunks,
            confidence_by_page=confidence_by_page,
        )
        ids = _flag_ambiguous(session, source_id=source_id, chunks=chunks)
        enqueue(session, source_id=source_id, kind="embed")
        return written, ids

    written, flagged = await run_db(_write)

    return StageResult(
        kind="chunk",
        source_id=source_id,
        status="done",
        detail=f"{written} chunks",
        flagged=tuple(flagged),
        counts={"chunks": written, "ambiguous": len(flagged)},
    )


def _blocks_from_pages(records: list[dict[str, Any]]) -> list[Block]:
    """Turn normalised pages into the chunker's ``Block`` sequence.

    Paragraphs become body blocks; a page's tables become one block each,
    tagged so the chunker keeps them whole rather than breaking a table across
    two chunks and citing half a grid as evidence.

    ``starts_section`` is set on the first block whose section path differs
    from the previous one -- retrieval §7's preference-1 break point, and the
    only signal that stops a chunk straddling a section boundary and filing
    half its text under the wrong heading.
    """
    import re

    blocks: list[Block] = []
    previous_path: tuple[str, ...] | None = None

    for record in records:
        page = int(record["page_number"])
        path = tuple(record.get("section_hierarchy") or ())
        text = record.get("text") or ""

        first_on_page = True
        for paragraph in re.split(r"\n\s*\n", text):
            if not paragraph.strip():
                continue
            blocks.append(
                Block(
                    text=paragraph.strip(),
                    section_path=path,
                    page=page,
                    starts_section=first_on_page and path != previous_path,
                )
            )
            first_on_page = False
            previous_path = path

        for table in record.get("tables") or ():
            if table and table.strip():
                # Not "code", though the chunker treats both as atomic: the
                # type is what retrieval filters and displays on, and a table
                # shown as a code block reads as a bug in the hover card.
                blocks.append(
                    Block(
                        text=table.strip(),
                        section_path=path,
                        page=page,
                        kind="figure_caption",
                    )
                )

    return blocks


@ingestion_writer
def _write_chunks(
    session: Session,
    *,
    source_id: uuid.UUID | None,
    chunks: list[Any],
    confidence_by_page: dict[int, float],
) -> int:
    """Insert one source's chunks (§6.5 step 3).

    Replaces any existing chunks for the source, so a re-chunk after a chunker
    change is idempotent rather than doubling the corpus. Existing rows are
    marked superseded rather than deleted --
    ``content_citations.source_chunk_id`` is ``ON DELETE RESTRICT``, so a
    citation already written against an old chunk must keep resolving (§7.3).
    """
    require("source_id", source_id)

    session.execute(
        sql(
            """
            UPDATE source_chunks
               SET superseded_at = NOW()
             WHERE source_id = :source_id
               AND superseded_at IS NULL
            """
        ),
        {"source_id": source_id},
    )

    # chunk_index restarts at 0 for each generation, and the table's
    # uq_source_chunks_index is on (source_id, chunk_index) -- so a re-chunk
    # would collide with the rows just superseded. Offsetting past the highest
    # index already used keeps both generations addressable.
    offset = (
        session.execute(
            sql(
                "SELECT COALESCE(MAX(chunk_index) + 1, 0) FROM source_chunks "
                "WHERE source_id = :source_id"
            ),
            {"source_id": source_id},
        ).scalar_one()
        or 0
    )

    import json

    session.execute(
        sql(
            """
            INSERT INTO source_chunks
                (source_id, chunk_index, text, token_count, page_start,
                 page_end, section_path, chunk_type, extraction_confidence)
            VALUES
                (:source_id, :chunk_index, :text, :token_count, :page_start,
                 :page_end, CAST(:section_path AS jsonb),
                 CAST(:chunk_type AS chunk_kind), :confidence)
            """
        ),
        [
            {
                "source_id": source_id,
                "chunk_index": offset + chunk.chunk_index,
                "text": chunk.text,
                "token_count": chunk.token_count,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "section_path": json.dumps(list(chunk.section_path)),
                "chunk_type": chunk.chunk_type,
                # The lowest confidence of any page the chunk spans. A chunk is
                # only as trustworthy as its worst page, and averaging would
                # let one good page hide a bad one inside the same passage.
                "confidence": _chunk_confidence(chunk, confidence_by_page),
            }
            for chunk in chunks
        ],
    )

    session.execute(
        sql("UPDATE sources SET ingested_at = NOW(), token_count = :tokens WHERE id = :id"),
        {"tokens": sum(c.token_count for c in chunks), "id": source_id},
    )
    _flag_orphaned_curation(session, source_id=source_id)
    return len(chunks)


def _flag_orphaned_curation(session: Session, *, source_id: uuid.UUID) -> list[uuid.UUID]:
    """Report curated pointers that a re-chunk left pointing at dead chunks.

    ``concept_sources.chunk_ids`` is an array of specific chunk ids a domain
    expert chose. Superseding a source's chunks does not rewrite them, and
    nothing can: the new chunks have new ids and new boundaries, so which of
    them corresponds to the passage the author picked is a judgement the author
    made and the pipeline cannot repeat.

    Left alone this fails silently and expensively. Retrieval filters
    superseded chunks (§7.3), so the concept keeps its ``concept_sources`` row,
    the row resolves to nothing, and the Lecturer falls back to subject-wide
    search -- the "curated" path degrading to the "expanded" one with no signal
    anywhere. §13's invariant is the reason this is a queue row instead: the
    pipeline does not know the right answer, so it says so to a reviewer rather
    than picking one.
    """
    orphaned = session.execute(
        sql(
            """
            SELECT cs.concept_id, c.slug AS concept_slug,
                   cardinality(cs.chunk_ids) AS pointed_at
              FROM concept_sources cs
              JOIN concepts c ON c.id = cs.concept_id
             WHERE cs.source_id = :source_id
               AND cardinality(cs.chunk_ids) > 0
               AND NOT EXISTS (
                     SELECT 1 FROM source_chunks sc
                      WHERE sc.id = ANY(cs.chunk_ids)
                        AND sc.superseded_at IS NULL
               )
            """
        ),
        {"source_id": source_id},
    ).all()

    return [
        flag(
            session,
            flag_source="concept_source_conflict",
            source_id=source_id,
            concept_id=row.concept_id,
            reason=(
                f"re-chunking superseded every chunk curated onto concept "
                f"{row.concept_slug!r} ({row.pointed_at} pointer(s)); the "
                f"concept has no curated grounding until the links are "
                f"re-authored against the new chunks"
            ),
            payload={"concept": row.concept_slug, "pointers": int(row.pointed_at)},
        )
        for row in orphaned
    ]


def _chunk_confidence(chunk: Any, by_page: dict[int, float]) -> float:
    start = chunk.page_start
    end = chunk.page_end
    if start is None and end is None:
        return 1.0
    pages = range(start or end or 0, (end or start or 0) + 1)
    values = [by_page.get(page, 1.0) for page in pages]
    return min(values) if values else 1.0


def _flag_ambiguous(
    session: Session, *, source_id: uuid.UUID, chunks: list[Any]
) -> list[uuid.UUID]:
    """§6.5 step 4: chunks the chunker could not confidently type.

    The chunker has no "unsure" output -- ``classify_block`` always returns a
    type. What it does have is a case where the type came from a content
    heuristic with nothing structural behind it: a ``body`` chunk that is
    mostly symbols is very likely mangled mathematics, which pdfplumber is
    documented as producing (§7.1) and which retrieval will happily return as
    prose evidence for a claim it does not support.
    """
    flagged: list[uuid.UUID] = []
    for chunk in chunks:
        if chunk.chunk_type != "body" or not chunk.text:
            continue
        symbols = sum(1 for ch in chunk.text if not ch.isalnum() and not ch.isspace())
        ratio = symbols / len(chunk.text)
        if ratio < 0.35:
            continue
        flagged.append(
            flag(
                session,
                flag_source="chunk_ambiguous_type",
                source_id=source_id,
                reason=(
                    f"chunk {chunk.chunk_index} is {ratio:.0%} symbols and is "
                    f"typed 'body'; it is probably mathematics the extractor "
                    f"could not lay out"
                ),
                payload={
                    "chunk_index": chunk.chunk_index,
                    "symbol_ratio": round(ratio, 3),
                    "preview": chunk.text[:200],
                },
            )
        )
    return flagged


# --- §6.6 embed ------------------------------------------------------------


async def run_embed(source_id: uuid.UUID) -> StageResult:
    """Hand the source's chunks to retrieval's embedding worker (§6.6).

    No new code here by design: this subsystem enqueues, retrieval §8 executes.
    Failures land in *this* subsystem's queue, which is the S3 resolution
    operationalised -- see ``studium.retrieval.embedding_worker._flag_failures``.
    """
    from studium.retrieval.embedding_worker import EmbeddingWorker

    report = await EmbeddingWorker().drain(source_id=source_id)
    return StageResult(
        kind="embed",
        source_id=source_id,
        status="done" if not report.failed else "failed",
        detail=f"{report.embedded} embedded, {report.failed} failed",
        counts={"embedded": report.embedded, "failed": report.failed},
    )


# --- driving the whole thing ----------------------------------------------


async def run_pipeline(
    source_id: uuid.UUID,
    *,
    extractor_name: str | None = None,
    embed: bool = True,
) -> list[StageResult]:
    """Run every stage for one source, stopping at the first failure.

    The stage-at-a-time worker is what runs in production, off ``ingestion_jobs``
    rows. This drives the same functions in order, for the CLI's synchronous
    import and for the Tier 2 tests -- which need the whole pipeline to have
    finished before they can assert on its output.

    Stopping at the first failure is the point: a source whose extraction
    produced nothing has nothing to normalise, and running the rest would bury
    one actionable ``extractor_failure`` under three derived ones.
    """
    results: list[StageResult] = []

    extract = await run_extract(source_id, extractor_name=extractor_name)
    results.append(extract)
    if not extract.ok:
        return results

    normalize = await run_normalize(source_id)
    results.append(normalize)
    if not normalize.ok:
        return results

    chunk = await run_chunk(source_id)
    results.append(chunk)
    if not chunk.ok or not embed:
        return results

    results.append(await run_embed(source_id))
    return results


async def run_worker_once() -> StageResult | None:
    """Claim one pending job and run its stage (§6.1).

    The production shape: a loop over this drains the queue, and the retry
    policies live here rather than in the stage functions so a stage stays a
    pure transformation that a test can call directly.
    """
    job = await run_db(lambda s: claim(s))
    if job is None:
        return None

    kind = job["kind"]
    source_id = job["source_id"]
    limits = {
        "extract_text": EXTRACT_MAX_ATTEMPTS,
        "normalize": NORMALIZE_MAX_ATTEMPTS,
        "chunk": CHUNK_MAX_ATTEMPTS,
        "embed": 1,
    }

    try:
        if kind == "extract_text":
            result = await run_extract(
                source_id, extractor_name=job["payload"].get("extractor")
            )
        elif kind == "normalize":
            result = await run_normalize(source_id)
        elif kind == "chunk":
            result = await run_chunk(source_id)
        elif kind == "embed":
            result = await run_embed(source_id)
        else:
            await run_db(
                lambda s: finish(s, job["id"], status="cancelled", error=f"unhandled kind {kind}")
            )
            return None
    except Exception as exc:  # noqa: BLE001 -- the retry decision is here
        # Same reason as the extract handler above: `exc` does not survive the
        # except block, and the lambda below is what writes the failure record.
        message = str(exc)
        attempts = limits.get(kind, 1)
        if job["attempt"] < attempts:
            log.warning(
                "%s for source %s failed on attempt %d/%d: %s",
                kind, source_id, job["attempt"], attempts, message,
            )
            await run_db(lambda s: release(s, job["id"]))
            return StageResult(
                kind=kind, source_id=source_id, status="retrying", detail=message
            )
        await run_db(lambda s: finish(s, job["id"], status="failed", error=message))
        raise

    await run_db(
        lambda s: finish(
            s,
            job["id"],
            status="done" if result.ok else "failed",
            error=None if result.ok else result.detail,
        )
    )
    return result


def _source_row(session: Session, source_id: uuid.UUID) -> dict[str, Any] | None:
    row = session.execute(
        sql(
            """
            SELECT id, subject_id, title, storage_path, status, license
              FROM sources
             WHERE id = :id AND deleted_at IS NULL
            """
        ),
        {"id": source_id},
    ).one_or_none()
    if row is None:
        return None
    return {
        "id": row.id,
        "subject_id": row.subject_id,
        "title": row.title,
        "storage_path": row.storage_path,
        "status": row.status,
        "license": row.license,
    }

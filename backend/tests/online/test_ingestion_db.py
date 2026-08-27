"""Ingestion against a real database, Tier 2 (ingestion §16).

Everything here needs Postgres, and needs it because the thing being tested is
the interaction between the pipeline and the schema: the constraints that fire,
the queue rows that land, the transactions that hold. A mocked session would
assert that the code calls the functions the code calls.

The S3 case is the reason this file exists in the shape it does. That defect was
invisible to every offline test -- the code looked right, the SQL looked right,
and the row was rejected by a CHECK constraint that only a real table has. The
lesson generalised: anything that writes a row gets asserted against a real one.

Embedding is stubbed throughout. Real vectors are Tier 3, in
``test_ingestion_paid``; the point here is that chunks reach the database in the
right shape, which does not need a provider.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.ingestion import authoring, licensing, pipeline, publish
from studium.ingestion import queue as review_queue
from studium.ingestion.licensing import HONEST_DEFAULT
from studium.models.identity import SYSTEM_USER_ID
from tests.fixtures import lambda_calculus, pdfs

pytestmark = pytest.mark.postgres


@pytest.fixture
def storage(tmp_path: Path, monkeypatch) -> Path:
    """Point ingestion's file storage at a temporary directory.

    Without this the pipeline writes to ``/data/sources`` -- the deployment's
    Fly volume layout -- on whatever machine runs the tests.
    """
    monkeypatch.setattr(
        "studium.config.settings.source_storage_root", str(tmp_path / "sources")
    )
    return tmp_path


@pytest.fixture
def seeded(runtime_db: Session):
    """The lambda calculus fixture, committed onto a savepoint.

    Reuses subsystem 1's fixture rather than defining a second seed, so
    ingestion is exercised against the same data every other subsystem's tests
    use.
    """
    fixture = lambda_calculus.build(
        runtime_db, email=f"ingest-{uuid.uuid4().hex[:8]}@example.com"
    )
    runtime_db.flush()
    runtime_db.commit()
    return fixture


def _upload(db: Session, subject_id, path: Path, **kwargs) -> uuid.UUID:
    source_id = pipeline.upload(
        db,
        data=path.read_bytes(),
        filename=path.name,
        subject_id=subject_id,
        uploaded_by=SYSTEM_USER_ID,
        **kwargs,
    )
    db.commit()
    return source_id


# --- §6.2 upload -----------------------------------------------------------


def test_upload_creates_a_draft_source(runtime_db, seeded, storage, tmp_path) -> None:
    """§6.2: a new source is draft, at the honest default, with its file stored."""
    path = pdfs.simple(tmp_path / "simple.pdf", pages=2)
    source_id = _upload(runtime_db, seeded.subject.id, path)

    row = runtime_db.execute(
        sql(
            "SELECT status, license, content_sha256, storage_path, page_count, "
            "uploaded_by FROM sources WHERE id = :id"
        ),
        {"id": source_id},
    ).one()

    assert row.status == "draft"
    assert row.license == HONEST_DEFAULT
    assert len(row.content_sha256) == 64
    assert row.uploaded_by == SYSTEM_USER_ID
    assert Path(row.storage_path).exists()
    assert row.page_count == 2


def test_upload_queues_a_license_pending_row(runtime_db, seeded, storage, tmp_path) -> None:
    """§6.2 step 5. The row is what makes the license gate visible to a reviewer."""
    path = pdfs.simple(tmp_path / "simple.pdf", pages=1)
    source_id = _upload(runtime_db, seeded.subject.id, path)

    items = review_queue.pending(runtime_db, source_id=source_id)
    assert [item.flag_source for item in items] == ["license_pending"]
    assert items[0].severity == 2


def test_upload_enqueues_the_extract_job(runtime_db, seeded, storage, tmp_path) -> None:
    path = pdfs.simple(tmp_path / "simple.pdf", pages=1)
    source_id = _upload(runtime_db, seeded.subject.id, path)

    kinds = runtime_db.execute(
        sql("SELECT kind, status FROM ingestion_jobs WHERE source_id = :id"),
        {"id": source_id},
    ).all()
    assert [(r.kind, r.status) for r in kinds] == [("extract_text", "pending")]


def test_a_duplicate_upload_is_refused_by_name(runtime_db, seeded, storage, tmp_path) -> None:
    """§14: HTTP 409, naming the existing source.

    Checked before the insert rather than relying on the unique constraint, so
    the error can say *which* source -- which is what the uploader needs and
    what an IntegrityError cannot tell them.
    """
    path = pdfs.simple(tmp_path / "simple.pdf", pages=1)
    first = _upload(runtime_db, seeded.subject.id, path)

    with pytest.raises(pipeline.DuplicateSource) as excinfo:
        _upload(runtime_db, seeded.subject.id, path)
    assert excinfo.value.existing_id == first
    assert excinfo.value.status_code == 409


def test_the_same_content_may_be_uploaded_to_a_second_subject(
    runtime_db, seeded, storage, tmp_path
) -> None:
    """The unique constraint is per subject, deliberately.

    A text can reasonably ground two subjects; refusing the second upload would
    make the corpus a function of which subject was authored first.
    """
    other = runtime_db.execute(
        sql(
            "INSERT INTO subjects (slug, title) VALUES (:slug, 'Other') RETURNING id"
        ),
        {"slug": f"other-{uuid.uuid4().hex[:8]}"},
    ).scalar_one()
    runtime_db.commit()

    path = pdfs.simple(tmp_path / "simple.pdf", pages=1)
    _upload(runtime_db, seeded.subject.id, path)
    second = _upload(runtime_db, other, path)
    assert second is not None


def test_a_non_pdf_is_refused(runtime_db, seeded, storage, tmp_path) -> None:
    """§14: HTTP 415, on the magic bytes rather than the filename.

    The filename is chosen by whoever is uploading.
    """
    path = pdfs.not_a_pdf(tmp_path / "lies.pdf")
    with pytest.raises(pipeline.UnsupportedFormat) as excinfo:
        _upload(runtime_db, seeded.subject.id, path)
    assert excinfo.value.status_code == 415


def test_an_oversized_upload_is_refused(runtime_db, seeded, storage) -> None:
    """§14: HTTP 413, with the limit in the message."""
    oversized = b"%PDF-" + b"\0" * pipeline.MAX_UPLOAD_BYTES
    with pytest.raises(pipeline.SourceTooLarge) as excinfo:
        pipeline.upload(
            runtime_db,
            data=oversized,
            filename="huge.pdf",
            subject_id=seeded.subject.id,
            uploaded_by=SYSTEM_USER_ID,
        )
    assert excinfo.value.status_code == 413


def test_an_upload_without_an_uploader_is_refused(runtime_db, seeded, storage, tmp_path) -> None:
    """§13.2, at the boundary where the gap is real.

    Not defaulted to the system user. An unattributed source cannot be
    budgeted against or taken down on request, and inventing an uploader makes
    that unrecoverable rather than merely missing.
    """
    from studium.ingestion.provenance import MissingProvenance

    path = pdfs.simple(tmp_path / "simple.pdf", pages=1)
    with pytest.raises(MissingProvenance) as excinfo:
        pipeline.upload(
            runtime_db,
            data=path.read_bytes(),
            filename=path.name,
            subject_id=seeded.subject.id,
            uploaded_by=None,
        )
    assert excinfo.value.column == "uploaded_by"


# --- the whole pipeline ----------------------------------------------------


def test_the_full_pipeline_produces_chunks(runtime_db, seeded, storage, tmp_path) -> None:
    """§16 Tier 2: upload a fixture, run all stages, verify chunks land."""
    path = pdfs.simple(tmp_path / "simple.pdf", pages=3)
    source_id = _upload(runtime_db, seeded.subject.id, path)

    results = asyncio.run(pipeline.run_pipeline(source_id, embed=False))
    assert all(r.ok for r in results), [(r.kind, r.detail) for r in results]

    row = runtime_db.execute(
        sql(
            """
            SELECT extractor_version, normalizer_version, ingested_at,
                   token_count,
                   (SELECT count(*) FROM source_chunks WHERE source_id = s.id) AS chunks
              FROM sources s WHERE s.id = :id
            """
        ),
        {"id": source_id},
    ).one()

    assert row.chunks > 0
    assert row.extractor_version.startswith("pdfplumber/")
    assert row.normalizer_version.startswith("studium.normalize/")
    assert row.ingested_at is not None
    assert row.token_count > 0


def test_chunks_carry_their_provenance(runtime_db, seeded, storage, tmp_path) -> None:
    """Page numbers and section paths are what a citation renders."""
    path = pdfs.simple(tmp_path / "simple.pdf", pages=3)
    source_id = _upload(runtime_db, seeded.subject.id, path)
    asyncio.run(pipeline.run_pipeline(source_id, embed=False))

    rows = runtime_db.execute(
        sql(
            """
            SELECT chunk_index, page_start, section_path, chunk_type,
                   extraction_confidence, superseded_at, token_count
              FROM source_chunks WHERE source_id = :id ORDER BY chunk_index
            """
        ),
        {"id": source_id},
    ).all()

    assert rows
    assert [r.chunk_index for r in rows] == list(range(len(rows)))
    assert all(r.page_start is not None for r in rows)
    assert all(r.superseded_at is None for r in rows)
    assert all(r.extraction_confidence == pytest.approx(1.0) for r in rows)
    assert all(r.token_count > 0 for r in rows)
    assert any(r.section_path for r in rows), "no chunk carried a section path"


def test_the_source_stays_draft_after_ingestion(runtime_db, seeded, storage, tmp_path) -> None:
    """§6.7, §3. Automation gets the corpus ready; a reviewer decides it teaches."""
    path = pdfs.simple(tmp_path / "simple.pdf", pages=2)
    source_id = _upload(runtime_db, seeded.subject.id, path)
    asyncio.run(pipeline.run_pipeline(source_id, embed=False))

    status = runtime_db.execute(
        sql("SELECT status FROM sources WHERE id = :id"), {"id": source_id}
    ).scalar_one()
    assert status == "draft"


def test_a_scanned_pdf_fails_cleanly(runtime_db, seeded, storage, tmp_path) -> None:
    """§14: extractor_failure at severity 3, and the source stays draft.

    The alternative -- ingesting it as an empty source -- produces a subject
    that retrieves nothing with no indication why.
    """
    path = pdfs.scanned(tmp_path / "scan.pdf")
    source_id = _upload(runtime_db, seeded.subject.id, path)

    results = asyncio.run(pipeline.run_pipeline(source_id, embed=False))
    assert not results[0].ok
    assert results[0].kind == "extract_text"

    items = review_queue.pending(runtime_db, source_id=source_id)
    failures = [i for i in items if i.flag_source == "extractor_failure"]
    assert failures, [i.flag_source for i in items]
    assert failures[0].severity == 3
    assert "OCR" in failures[0].reason

    status = runtime_db.execute(
        sql("SELECT status FROM sources WHERE id = :id"), {"id": source_id}
    ).scalar_one()
    assert status == "draft"


def test_a_tiny_pdf_is_below_the_token_floor(runtime_db, seeded, storage, tmp_path) -> None:
    path = pdfs.tiny(tmp_path / "tiny.pdf")
    source_id = _upload(runtime_db, seeded.subject.id, path)
    results = asyncio.run(pipeline.run_pipeline(source_id, embed=False))
    assert not results[0].ok


def test_normalizer_warnings_reach_the_queue(runtime_db, seeded, storage, tmp_path) -> None:
    """§8.3 at severity 1: worth a look, nothing is broken."""
    path = pdfs.running_heads(tmp_path / "heads.pdf", pages=8)
    source_id = _upload(runtime_db, seeded.subject.id, path)
    asyncio.run(pipeline.run_pipeline(source_id, embed=False))

    items = review_queue.pending(runtime_db, source_id=source_id)
    warnings = [i for i in items if i.flag_source == "normalizer_warning"]
    assert warnings, "furniture stripping produced no reviewer-visible record"
    assert all(w.severity == 1 for w in warnings)


# --- §7.3 re-extraction and supersession -----------------------------------


def test_re_chunking_supersedes_rather_than_deletes(
    runtime_db, seeded, storage, tmp_path
) -> None:
    """§7.3. Old chunks stay so citations against them keep resolving."""
    path = pdfs.simple(tmp_path / "simple.pdf", pages=2)
    source_id = _upload(runtime_db, seeded.subject.id, path)
    asyncio.run(pipeline.run_pipeline(source_id, embed=False))

    first = runtime_db.execute(
        sql("SELECT id FROM source_chunks WHERE source_id = :id"), {"id": source_id}
    ).scalars().all()

    asyncio.run(pipeline.run_chunk(source_id))

    counts = runtime_db.execute(
        sql(
            """
            SELECT count(*) FILTER (WHERE superseded_at IS NULL)     AS live,
                   count(*) FILTER (WHERE superseded_at IS NOT NULL) AS dead
              FROM source_chunks WHERE source_id = :id
            """
        ),
        {"id": source_id},
    ).one()

    assert counts.dead == len(first), "the old generation was deleted, not superseded"
    assert counts.live > 0

    still_there = runtime_db.execute(
        sql("SELECT count(*) FROM source_chunks WHERE id = ANY(:ids)"),
        {"ids": first},
    ).scalar_one()
    assert still_there == len(first)


def test_re_chunking_does_not_collide_on_chunk_index(
    runtime_db, seeded, storage, tmp_path
) -> None:
    """``uq_source_chunks_index`` is on (source_id, chunk_index).

    A second generation restarting at 0 would violate it, so the new run is
    offset past the highest index already used.
    """
    path = pdfs.simple(tmp_path / "simple.pdf", pages=2)
    source_id = _upload(runtime_db, seeded.subject.id, path)
    asyncio.run(pipeline.run_pipeline(source_id, embed=False))
    asyncio.run(pipeline.run_chunk(source_id))
    asyncio.run(pipeline.run_chunk(source_id))

    duplicates = runtime_db.execute(
        sql(
            """
            SELECT count(*) FROM (
                SELECT chunk_index FROM source_chunks
                 WHERE source_id = :id
                 GROUP BY chunk_index HAVING count(*) > 1
            ) AS d
            """
        ),
        {"id": source_id},
    ).scalar_one()
    assert duplicates == 0


def test_re_chunking_flags_orphaned_curation(
    runtime_db, seeded, storage, tmp_path
) -> None:
    """The gap §7.3 leaves open: curated pointers die in a re-extraction.

    ``concept_sources.chunk_ids`` names specific chunks a domain expert chose.
    Superseding them does not rewrite the array and nothing can -- which chunk
    of the new generation corresponds to the passage they picked is a judgement
    the pipeline cannot repeat. Retrieval then filters the superseded rows, so
    the concept silently loses its curated grounding and falls back to
    subject-wide search. The queue row is what stops that being silent.
    """
    path = pdfs.simple(tmp_path / "simple.pdf", pages=3)
    source_id = _upload(runtime_db, seeded.subject.id, path)
    asyncio.run(pipeline.run_pipeline(source_id, embed=False))

    chunk_ids = runtime_db.execute(
        sql(
            "SELECT id FROM source_chunks WHERE source_id = :id "
            "AND superseded_at IS NULL LIMIT 2"
        ),
        {"id": source_id},
    ).scalars().all()

    runtime_db.execute(
        sql(
            """
            INSERT INTO concept_sources (concept_id, source_id, chunk_ids, role)
            VALUES (:concept_id, :source_id, :chunk_ids, 'primary_exposition')
            """
        ),
        {
            "concept_id": seeded.concept_id("beta-reduction"),
            "source_id": source_id,
            "chunk_ids": chunk_ids,
        },
    )
    runtime_db.commit()

    asyncio.run(pipeline.run_chunk(source_id))

    items = review_queue.pending(runtime_db, source_id=source_id)
    conflicts = [i for i in items if i.flag_source == "concept_source_conflict"]
    assert conflicts, "orphaned curation was not reported to a reviewer"
    assert "beta-reduction" in conflicts[0].reason


# --- §12 the review queue --------------------------------------------------


def test_the_queue_accepts_a_chunk_target(runtime_db, seeded) -> None:
    """The S3 resolution, asserted directly.

    ``content_review_queue`` rejects this row -- its ``has_target`` CHECK needs
    an artifact or a session turn, and an embedding failure has neither. That
    is why a chunk with no vector used to leave nothing behind but a log line.
    """
    item_id = review_queue.flag(
        runtime_db,
        flag_source="embedding_failure",
        source_chunk_id=seeded.chunks[0].id,
        reason="chunk could not be embedded after retries",
    )
    runtime_db.commit()

    item = review_queue.get(runtime_db, item_id)
    assert item is not None
    assert item.source_chunk_id == seeded.chunks[0].id
    assert item.severity == 3


def test_the_old_queue_still_rejects_a_targetless_row(runtime_db) -> None:
    """The reason S3 was fixed with a new table, not a relaxed CHECK.

    ``has_target`` is correct for the rows it governs: a *content* review item
    with no artifact and no turn has no subject. Relaxing it to admit ingestion
    rows would have broken that guarantee for every existing caller.
    """
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        runtime_db.execute(
            sql(
                "INSERT INTO content_review_queue (source, reason) "
                "VALUES ('system_confidence', 'no target')"
            )
        )
    runtime_db.rollback()


def test_a_queue_row_needs_a_target(runtime_db) -> None:
    """Refused in Python, naming the four columns, before the CHECK fires."""
    from studium.ingestion.provenance import MissingProvenance

    with pytest.raises(MissingProvenance) as excinfo:
        review_queue.flag(
            runtime_db, flag_source="license_pending", reason="orphan row"
        )
    assert "source_chunk_id" in str(excinfo.value)


def test_resolving_records_the_note(runtime_db, seeded) -> None:
    """§12.3: the row persists for audit and the note says why."""
    item_id = review_queue.flag(
        runtime_db,
        flag_source="normalizer_warning",
        source_id=seeded.source.id,
        reason="unusual line lengths",
    )
    assert review_queue.resolve(runtime_db, item_id, note="checked; the source is fine")
    runtime_db.commit()

    item = review_queue.get(runtime_db, item_id)
    assert item is not None
    assert item.status == "resolved"
    assert item.resolution_note == "checked; the source is fine"
    assert review_queue.pending(runtime_db, source_id=seeded.source.id) == []


def test_resolving_twice_is_reported_not_silent(runtime_db, seeded) -> None:
    item_id = review_queue.flag(
        runtime_db,
        flag_source="normalizer_warning",
        source_id=seeded.source.id,
        reason="x",
    )
    assert review_queue.resolve(runtime_db, item_id, note="first")
    assert not review_queue.resolve(runtime_db, item_id, note="second")


def test_escalation_only_raises(runtime_db, seeded) -> None:
    """§12.3. Lowering a severity would let a triage pass quietly bury something.

    Dismissing with a note is the honest way to say "not a problem".
    """
    item_id = review_queue.flag(
        runtime_db,
        flag_source="chunk_ambiguous_type",
        source_id=seeded.source.id,
        reason="mostly symbols",
    )
    assert review_queue.escalate(runtime_db, item_id, severity=3)
    assert not review_queue.escalate(runtime_db, item_id, severity=1)

    item = review_queue.get(runtime_db, item_id)
    assert item is not None and item.severity == 3


def test_pending_is_ordered_by_severity(runtime_db, seeded) -> None:
    """Matches ``idx_ingestion_queue_pending`` so the partial index serves it."""
    for flag_source in ("normalizer_warning", "extractor_failure", "license_pending"):
        review_queue.flag(
            runtime_db,
            flag_source=flag_source,
            source_id=seeded.source.id,
            reason=flag_source,
        )
    runtime_db.commit()

    severities = [i.severity for i in review_queue.pending(runtime_db, source_id=seeded.source.id)]
    assert severities == sorted(severities, reverse=True)


def test_deleting_a_source_cascades_its_queue_rows(runtime_db, seeded, storage, tmp_path) -> None:
    """The FK is ON DELETE CASCADE, and the indexes exist so it is not a scan."""
    path = pdfs.simple(tmp_path / "simple.pdf", pages=1)
    source_id = _upload(runtime_db, seeded.subject.id, path)
    assert review_queue.pending(runtime_db, source_id=source_id)

    runtime_db.execute(sql("DELETE FROM sources WHERE id = :id"), {"id": source_id})
    runtime_db.commit()

    remaining = runtime_db.execute(
        sql("SELECT count(*) FROM ingestion_review_queue WHERE source_id = :id"),
        {"id": source_id},
    ).scalar_one()
    assert remaining == 0


# --- §11 licensing ---------------------------------------------------------


def test_classification_records_the_basis(runtime_db, seeded, storage, tmp_path) -> None:
    path = pdfs.simple(tmp_path / "simple.pdf", pages=1)
    source_id = _upload(runtime_db, seeded.subject.id, path)

    state = licensing.classify(
        runtime_db,
        source_id=source_id,
        license="public_domain",
        note="Church 1936; US publication before 1929 equivalent",
    )
    runtime_db.commit()

    assert state.license == "public_domain"
    assert state.is_classified
    assert "Church 1936" in (state.license_notes or "")


def test_an_unclassified_source_is_listed(runtime_db, seeded, storage, tmp_path) -> None:
    path = pdfs.simple(tmp_path / "simple.pdf", pages=1)
    source_id = _upload(runtime_db, seeded.subject.id, path)

    unclassified = licensing.unclassified(runtime_db, subject_id=seeded.subject.id)
    assert source_id in {s.source_id for s in unclassified}

    licensing.classify(
        runtime_db, source_id=source_id, license="cc_by", note="CC-BY on the author page"
    )
    runtime_db.commit()

    unclassified = licensing.unclassified(runtime_db, subject_id=seeded.subject.id)
    assert source_id not in {s.source_id for s in unclassified}


def test_the_default_with_a_note_counts_as_classified(
    runtime_db, seeded, storage, tmp_path
) -> None:
    """§11.2's genuine outcome, distinct from the untouched default.

    A reviewer who looked, could not establish more than "the uploader claims
    rights", and wrote that down has classified the source. The note is what
    separates that from nobody having looked.
    """
    path = pdfs.simple(tmp_path / "simple.pdf", pages=1)
    source_id = _upload(runtime_db, seeded.subject.id, path)

    licensing.classify(
        runtime_db,
        source_id=source_id,
        license="permission_granted",
        note="Michaelson emailed permission 2026-08-20; Dover confirmation pending",
    )
    runtime_db.commit()

    assert source_id not in {
        s.source_id for s in licensing.unclassified(runtime_db, subject_id=seeded.subject.id)
    }


# --- §9, §10 authoring -----------------------------------------------------


GRAPH_YAML = """
subject:
  slug: {slug}
  title: Imported Subject
  short_description: A subject imported from YAML.

concepts:
  - slug: syntax
    title: Syntax
    depth: 1
    is_load_bearing: true
  - slug: reduction
    title: Reduction
    depth: 2
    is_load_bearing: false

edges:
  - from: syntax
    to: reduction
    kind: prerequisite
"""


def _import_graph(db: Session, tmp_path: Path, slug: str):
    directory = tmp_path / "graph"
    directory.mkdir(exist_ok=True)
    (directory / "graph.yaml").write_text(GRAPH_YAML.format(slug=slug), encoding="utf-8")
    graph = authoring.parse_graph(authoring.yaml_files(directory))
    diff = authoring.diff_graph(db, graph)
    result = authoring.apply_graph(db, graph=graph, diff=diff)
    db.commit()
    return graph, result


def test_graph_import_creates_rows(runtime_db, storage, tmp_path) -> None:
    """§16 Tier 2: subjects, concepts and concept_edges land correctly."""
    slug = f"imported-{uuid.uuid4().hex[:8]}"
    _, result = _import_graph(runtime_db, tmp_path, slug)

    row = runtime_db.execute(
        sql(
            """
            SELECT s.status,
                   (SELECT count(*) FROM concepts WHERE subject_id = s.id) AS concepts,
                   (SELECT count(*) FROM concept_edges WHERE subject_id = s.id) AS edges
              FROM subjects s WHERE s.slug = :slug
            """
        ),
        {"slug": slug},
    ).one()

    assert row.concepts == 2
    assert row.edges == 1
    assert row.status == "draft", "importing a graph must not publish it"
    assert result.counts == {"concepts": 2, "edges": 1}


def test_graph_import_is_idempotent(runtime_db, storage, tmp_path) -> None:
    """Re-importing an unchanged graph changes nothing.

    The reviewer's loop is edit-import-edit-import, so a second import of the
    same file must not duplicate concepts or bump the version.
    """
    slug = f"imported-{uuid.uuid4().hex[:8]}"
    _import_graph(runtime_db, tmp_path, slug)
    before = runtime_db.execute(
        sql("SELECT version FROM subjects WHERE slug = :slug"), {"slug": slug}
    ).scalar_one()

    _, result = _import_graph(runtime_db, tmp_path, slug)
    after = runtime_db.execute(
        sql(
            """
            SELECT s.version,
                   (SELECT count(*) FROM concepts WHERE subject_id = s.id) AS concepts
              FROM subjects s WHERE s.slug = :slug
            """
        ),
        {"slug": slug},
    ).one()

    assert after.concepts == 2
    assert after.version == before, "an unchanged re-import bumped the version"
    assert result.diff.is_empty


def test_a_concept_removed_from_the_files_is_reported_not_deleted(
    runtime_db, storage, tmp_path
) -> None:
    """Deleting a concept cascades to concept_mastery.

    A slug typo'd out of a YAML file is not consent to erase every learner's
    recorded progress on it, so the diff reports the removal and the row stays.
    """
    slug = f"imported-{uuid.uuid4().hex[:8]}"
    _import_graph(runtime_db, tmp_path, slug)

    # The edge goes with the concept. Leaving it behind would fail validation
    # for a different and correct reason -- an edge to a concept that is not in
    # the graph -- and this test would pass without exercising removal at all.
    directory = tmp_path / "graph"
    (directory / "graph.yaml").write_text(
        f"""
subject:
  slug: {slug}
  title: Imported Subject
concepts:
  - slug: syntax
    title: Syntax
    depth: 1
    is_load_bearing: true
""",
        encoding="utf-8",
    )
    graph = authoring.parse_graph(authoring.yaml_files(directory))
    diff = authoring.diff_graph(runtime_db, graph)

    assert any("reduction" in item for item in diff.removed)

    authoring.apply_graph(runtime_db, graph=graph, diff=diff)
    runtime_db.commit()

    survived = runtime_db.execute(
        sql(
            "SELECT count(*) FROM concepts c JOIN subjects s ON s.id = c.subject_id "
            "WHERE s.slug = :slug AND c.slug = 'reduction'"
        ),
        {"slug": slug},
    ).scalar_one()
    assert survived == 1


def test_graph_warnings_are_logged_to_the_queue(runtime_db, storage, tmp_path) -> None:
    """§9.2 step 9: warnings the reviewer accepted but wanted recorded."""
    slug = f"orphan-{uuid.uuid4().hex[:8]}"
    directory = tmp_path / "graph"
    directory.mkdir(exist_ok=True)
    (directory / "graph.yaml").write_text(
        f"""
subject:
  slug: {slug}
  title: Orphaned
concepts:
  - slug: connected-a
    title: A
  - slug: connected-b
    title: B
  - slug: lonely
    title: Lonely
edges:
  - from: connected-a
    to: connected-b
    kind: prerequisite
""",
        encoding="utf-8",
    )
    graph = authoring.parse_graph(authoring.yaml_files(directory))
    diff = authoring.diff_graph(runtime_db, graph)
    result = authoring.apply_graph(runtime_db, graph=graph, diff=diff)
    runtime_db.commit()

    assert result.applied, "a warning must not block the import"
    assert result.flagged
    item = review_queue.get(runtime_db, result.flagged[0])
    assert item is not None
    assert item.flag_source == "graph_validation_error"
    assert "lonely" in item.reason


def test_concept_source_import_resolves_selectors(
    runtime_db, seeded, storage, tmp_path
) -> None:
    """§16 Tier 2: by_page_range and by_text_match both resolve."""
    path = pdfs.simple(tmp_path / "simple.pdf", pages=3)
    source_id = _upload(runtime_db, seeded.subject.id, path)
    asyncio.run(pipeline.run_pipeline(source_id, embed=False))

    sample = runtime_db.execute(
        sql(
            "SELECT text FROM source_chunks WHERE source_id = :id "
            "AND superseded_at IS NULL ORDER BY chunk_index LIMIT 1"
        ),
        {"id": source_id},
    ).scalar_one()
    # One whole line, and a long one. A slice across a newline would be
    # escaped by the YAML repr and match nothing; a short slice would match
    # several chunks and hit the ambiguity guard. Both are fixture bugs that
    # look like resolution bugs.
    excerpt = max(sample.split("\n"), key=len).strip()
    assert len(excerpt) > 30, "the fixture chunk has no line long enough to be unique"

    directory = tmp_path / "links"
    directory.mkdir(exist_ok=True)
    (directory / "links.yaml").write_text(
        f"""
source: {source_id}
subject: {seeded.subject.slug}
links:
  - concept: beta-reduction
    role: primary_exposition
    chunks:
      - by_page_range: [1, 2]
  - concept: alpha-equivalence
    role: worked_example
    chunks:
      - by_text_match: {excerpt!r}
""",
        encoding="utf-8",
    )

    documents = authoring.parse_concept_sources(authoring.yaml_files(directory))
    resolved_source, links = authoring.resolve_links(runtime_db, document=documents[0])
    assert resolved_source == source_id
    assert all(link.chunk_ids for link in links)

    authoring.apply_concept_sources(
        runtime_db,
        subject_slug=seeded.subject.slug,
        source_id=resolved_source,
        links=links,
    )
    runtime_db.commit()

    count = runtime_db.execute(
        sql("SELECT count(*) FROM concept_sources WHERE source_id = :id"),
        {"id": source_id},
    ).scalar_one()
    assert count == 2


def test_an_ambiguous_text_match_aborts_the_import(
    runtime_db, seeded, storage, tmp_path
) -> None:
    """§14: a hard error, because a curated pointer is the thing retrieval prefers.

    Guessing which chunk was meant would put a passage the author did not
    choose at the top of a lecture's evidence.
    """
    path = pdfs.simple(tmp_path / "simple.pdf", pages=3)
    source_id = _upload(runtime_db, seeded.subject.id, path)
    asyncio.run(pipeline.run_pipeline(source_id, embed=False))

    directory = tmp_path / "links"
    directory.mkdir(exist_ok=True)
    (directory / "links.yaml").write_text(
        f"""
source: {source_id}
subject: {seeded.subject.slug}
links:
  - concept: beta-reduction
    role: primary_exposition
    chunks:
      - by_text_match: "page"
""",
        encoding="utf-8",
    )
    documents = authoring.parse_concept_sources(authoring.yaml_files(directory))
    with pytest.raises(authoring.AuthoringError) as excinfo:
        authoring.resolve_links(runtime_db, document=documents[0])
    assert "matched" in str(excinfo.value)


def test_a_page_range_matching_nothing_aborts(runtime_db, seeded, storage, tmp_path) -> None:
    path = pdfs.simple(tmp_path / "simple.pdf", pages=2)
    source_id = _upload(runtime_db, seeded.subject.id, path)
    asyncio.run(pipeline.run_pipeline(source_id, embed=False))

    directory = tmp_path / "links"
    directory.mkdir(exist_ok=True)
    (directory / "links.yaml").write_text(
        f"""
source: {source_id}
subject: {seeded.subject.slug}
links:
  - concept: beta-reduction
    role: primary_exposition
    chunks:
      - by_page_range: [900, 999]
""",
        encoding="utf-8",
    )
    documents = authoring.parse_concept_sources(authoring.yaml_files(directory))
    with pytest.raises(authoring.AuthoringError, match="matched no chunks"):
        authoring.resolve_links(runtime_db, document=documents[0])


def test_rubric_import_rejects_a_missing_concept(runtime_db, seeded, tmp_path) -> None:
    """§14: a hard error, not a warning."""
    with pytest.raises(authoring.AuthoringError, match="not in subject"):
        authoring.apply_rubrics(
            runtime_db,
            subject_slug=seeded.subject.slug,
            criteria=[
                {
                    "concept": "no-such-concept",
                    "slug": "x",
                    "prompt": "P",
                    "key_points": [],
                    "weight": 2,
                    "min_words": 20,
                }
            ],
        )


# --- §9.3 publishing -------------------------------------------------------


def test_publish_is_blocked_without_curated_sources(runtime_db, storage, tmp_path) -> None:
    """§9.3 step 2: a concept with no curated pointers cannot be taught."""
    slug = f"imported-{uuid.uuid4().hex[:8]}"
    _import_graph(runtime_db, tmp_path, slug)

    result = publish.publish_subject(runtime_db, subject_slug=slug)
    runtime_db.commit()

    assert not result.published
    names = {check.name for check in result.failures}
    assert "every concept has curated sources" in names

    status = runtime_db.execute(
        sql("SELECT status FROM subjects WHERE slug = :slug"), {"slug": slug}
    ).scalar_one()
    assert status == "draft"


def test_publish_is_blocked_without_rubrics_on_load_bearing_concepts(
    runtime_db, storage, tmp_path
) -> None:
    """§9.3 step 3, §14: a load-bearing concept without a rubric cannot be assessed."""
    slug = f"imported-{uuid.uuid4().hex[:8]}"
    _import_graph(runtime_db, tmp_path, slug)

    _, checks = publish.validate(runtime_db, slug)
    failed = {c.name for c in checks if not c.passed}
    assert "load-bearing concepts have rubrics" in failed


def test_publish_is_blocked_by_an_unclassified_source(
    runtime_db, seeded, storage, tmp_path
) -> None:
    """§14: publish blocked with a message naming the unclassified sources."""
    path = pdfs.simple(tmp_path / "simple.pdf", pages=1)
    _upload(runtime_db, seeded.subject.id, path)

    _, checks = publish.validate(runtime_db, seeded.subject.slug)
    license_check = next(
        c for c in checks if c.name == "every source has a license determination"
    )
    assert not license_check.passed
    assert "unclassified" in license_check.detail


def test_publish_reports_every_failure_at_once(runtime_db, storage, tmp_path) -> None:
    """A reviewer fixing a subject wants the full list, not the first item.

    Finding four problems one run at a time is four round trips through a
    domain expert's afternoon.
    """
    slug = f"imported-{uuid.uuid4().hex[:8]}"
    _import_graph(runtime_db, tmp_path, slug)
    result = publish.publish_subject(runtime_db, subject_slug=slug)
    assert len(result.failures) >= 2


def test_a_complete_subject_publishes(runtime_db, seeded, storage, tmp_path) -> None:
    """The whole gate, passed. Sources go active with the subject.

    A published subject whose sources are still draft teaches nothing:
    retrieval filters on source status, so every lecture would ground in an
    empty result set.
    """
    path = pdfs.simple(tmp_path / "simple.pdf", pages=3)
    source_id = _upload(runtime_db, seeded.subject.id, path)
    asyncio.run(pipeline.run_pipeline(source_id, embed=False))
    licensing.classify(
        runtime_db, source_id=source_id, license="public_domain", note="test fixture"
    )

    # Curate every concept and give the load-bearing ones a rubric.
    chunk_ids = runtime_db.execute(
        sql(
            "SELECT id FROM source_chunks WHERE source_id = :id "
            "AND superseded_at IS NULL LIMIT 2"
        ),
        {"id": source_id},
    ).scalars().all()

    concepts = runtime_db.execute(
        sql("SELECT id, slug, is_load_bearing FROM concepts WHERE subject_id = :id"),
        {"id": seeded.subject.id},
    ).all()

    for concept in concepts:
        runtime_db.execute(
            sql(
                """
                INSERT INTO concept_sources (concept_id, source_id, chunk_ids, role)
                VALUES (:cid, :sid, :chunks, 'primary_exposition')
                ON CONFLICT DO NOTHING
                """
            ),
            {"cid": concept.id, "sid": source_id, "chunks": chunk_ids},
        )
        if concept.is_load_bearing:
            runtime_db.execute(
                sql(
                    """
                    INSERT INTO rubric_criteria
                        (concept_id, slug, prompt, key_points, weight, min_words)
                    VALUES (:cid, 'covers-it', 'Does the learner explain it?',
                            '[]'::jsonb, 2, 20)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"cid": concept.id},
            )
    runtime_db.commit()

    result = publish.publish_subject(runtime_db, subject_slug=seeded.subject.slug)
    runtime_db.commit()

    assert result.published, result.render()

    row = runtime_db.execute(
        sql(
            """
            SELECT s.status, s.published_at,
                   (SELECT status FROM sources WHERE id = :source_id) AS source_status
              FROM subjects s WHERE s.id = :id
            """
        ),
        {"id": seeded.subject.id, "source_id": source_id},
    ).one()
    assert row.status == "active"
    assert row.published_at is not None
    assert row.source_status == "active"


# --- the job worker --------------------------------------------------------


def test_the_worker_drains_the_pipeline(runtime_db, seeded, storage, tmp_path) -> None:
    """§6.1: one job at a time, each stage enqueueing the next.

    The production shape, as opposed to ``run_pipeline``'s synchronous drive.
    """
    path = pdfs.simple(tmp_path / "simple.pdf", pages=2)
    source_id = _upload(runtime_db, seeded.subject.id, path)

    async def drain() -> list[str]:
        done: list[str] = []
        for _ in range(10):
            result = await pipeline.run_worker_once()
            if result is None:
                break
            done.append(result.kind)
        return done

    stages = asyncio.run(drain())
    assert stages[:3] == ["extract_text", "normalize", "chunk"]

    jobs = runtime_db.execute(
        sql(
            "SELECT kind, status FROM ingestion_jobs WHERE source_id = :id "
            "ORDER BY created_at"
        ),
        {"id": source_id},
    ).all()
    assert all(job.status == "done" for job in jobs), [
        (j.kind, j.status) for j in jobs
    ]


def test_claim_marks_the_job_running(runtime_db, seeded, storage, tmp_path) -> None:
    path = pdfs.simple(tmp_path / "simple.pdf", pages=1)
    source_id = _upload(runtime_db, seeded.subject.id, path)

    job = pipeline.claim(runtime_db, kind="extract_text")
    assert job is not None
    assert job["source_id"] == source_id
    assert job["attempt"] == 1

    status = runtime_db.execute(
        sql("SELECT status FROM ingestion_jobs WHERE id = :id"), {"id": job["id"]}
    ).scalar_one()
    assert status == "running"


def test_claiming_an_empty_queue_returns_none(runtime_db) -> None:
    assert pipeline.claim(runtime_db, kind="extract_text") is None

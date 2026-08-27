"""Ingestion Tier 3: the full pipeline with real embeddings (ingestion §16).

Two checks, and the second is the one that matters.

The first runs a real PDF through every stage and ends with real vectors in
``source_chunk_embeddings``. The second then asks retrieval for a passage and
checks the newly ingested chunks come back.

That second check is the point of this file. Tier 1 verifies the chunker is
deterministic. Tier 2 verifies chunks reach the database in the right shape.
Neither can tell you whether the text ingestion produced is text retrieval can
find -- both subsystems can pass every test they own and be wrong about each
other, which is what an ingestion pipeline writing chunks nobody can retrieve
looks like from the inside. It costs real money to answer, and it is the only
question here that a fixture cannot.

Billable and opt-in twice: a Voyage key must be present *and*
``STUDIUM_RUN_PAID_TESTS`` set.

    pip install -e ".[dev,retrieval,ingestion]"
    VOYAGE_API_KEY=... STUDIUM_RUN_PAID_TESTS=1 \\
      python -m pytest tests/online/test_ingestion_paid.py -m anthropic
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text as sql

from studium.ingestion import pipeline
from studium.models.identity import SYSTEM_USER_ID
from tests.fixtures import lambda_calculus, pdfs

pytestmark = [pytest.mark.postgres, pytest.mark.anthropic]

PAID_ENV = "STUDIUM_RUN_PAID_TESTS"


@pytest.fixture(scope="session")
def voyage_enabled() -> bool:
    if not os.environ.get("VOYAGE_API_KEY"):
        pytest.skip("no VOYAGE_API_KEY in the environment")
    if os.environ.get(PAID_ENV, "").lower() not in {"1", "true", "yes"}:
        pytest.skip(f"{PAID_ENV} is not set; these tests make billable calls")
    pytest.importorskip("voyageai", reason="pip install -e '.[retrieval]'")
    return True


@pytest.fixture
def storage(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(
        "studium.config.settings.source_storage_root", str(tmp_path / "sources")
    )
    return tmp_path


@pytest.fixture
def seeded(runtime_db):
    fixture = lambda_calculus.build(
        runtime_db, email=f"paid-{uuid.uuid4().hex[:8]}@example.com"
    )
    runtime_db.flush()
    runtime_db.commit()
    return fixture


def test_the_pipeline_ends_with_real_embeddings(
    voyage_enabled, runtime_db, seeded, storage, tmp_path
) -> None:
    """§16 Tier 3: upload, extract, normalise, chunk, embed -- for real."""
    from studium.retrieval.providers import VoyageEmbeddings

    path = pdfs.simple(tmp_path / "paid.pdf", pages=4)
    source_id = pipeline.upload(
        runtime_db,
        data=path.read_bytes(),
        filename=path.name,
        subject_id=seeded.subject.id,
        uploaded_by=SYSTEM_USER_ID,
    )
    runtime_db.commit()

    results = asyncio.run(pipeline.run_pipeline(source_id, embed=True))
    assert all(r.ok for r in results), [(r.kind, r.detail) for r in results]

    row = runtime_db.execute(
        sql(
            """
            SELECT count(*) AS chunks,
                   count(e.chunk_id) AS embedded,
                   count(DISTINCT e.model_version) AS models,
                   min(e.model_version) AS model
              FROM source_chunks sc
              LEFT JOIN source_chunk_embeddings e ON e.chunk_id = sc.id
             WHERE sc.source_id = :id AND sc.superseded_at IS NULL
            """
        ),
        {"id": source_id},
    ).one()

    assert row.chunks > 0
    assert row.embedded == row.chunks, (
        f"{row.chunks - row.embedded} chunk(s) have no vector and are "
        f"invisible to vector search"
    )
    # A stub provider would also fill the column. The model version is what
    # distinguishes a real embedding run from a test double that reported one.
    assert row.models == 1
    assert row.model == VoyageEmbeddings().model_version


def test_newly_ingested_chunks_are_retrievable(
    voyage_enabled, runtime_db, seeded, storage, tmp_path
) -> None:
    """The seam Tier 1 and Tier 2 cannot reach.

    Ingestion and retrieval can each pass every test they own while disagreeing
    about each other -- a chunk written with the wrong ``chunk_type``, a
    ``superseded_at`` filter that excludes everything, an embedding written
    against a model whose dimensionality does not match the column. All of them
    look like healthy ingestion and empty search results.

    Asserting the ingested source appears rather than that it ranks first: the
    fixture corpus already grounds these concepts, and demanding the new
    material outrank it would be a test of the reranker's opinion rather than
    of the pipeline.
    """
    from studium.retrieval import default_retriever

    path = pdfs.simple(tmp_path / "paid.pdf", pages=4)
    source_id = pipeline.upload(
        runtime_db,
        data=path.read_bytes(),
        filename=path.name,
        subject_id=seeded.subject.id,
        uploaded_by=SYSTEM_USER_ID,
    )
    runtime_db.commit()

    results = asyncio.run(pipeline.run_pipeline(source_id, embed=True))
    assert all(r.ok for r in results)

    # The source has to be active for retrieval to consider it -- exactly the
    # draft-until-published gate §3 describes, and a thing that silently
    # returns nothing if forgotten.
    runtime_db.execute(
        sql("UPDATE sources SET status = 'active' WHERE id = :id"), {"id": source_id}
    )
    runtime_db.commit()

    retriever = default_retriever()
    result = asyncio.run(
        retriever.retrieve_passages(
            seeded.concept_id("beta-reduction"),
            k=20,
            query_text="beta-reduction substitutes the argument into the body",
        )
    )

    assert result.passages, "retrieval returned nothing for a freshly ingested source"
    returned_sources = {p.source_id for p in result.passages}
    assert source_id in returned_sources, (
        f"the newly ingested source did not appear among "
        f"{len(result.passages)} passages; ingestion wrote chunks that "
        f"retrieval cannot find. Query used: {result.query_used!r}"
    )

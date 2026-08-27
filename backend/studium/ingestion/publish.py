"""Publishing a subject (ingestion §9.3).

The moment authored work becomes learner-visible. §3: "the reviewer's
transition of a subject from draft to active is the moment when authorial work
becomes learner-visible; nothing else counts." Nothing in the pipeline and
nothing in the authoring import does this -- a source can be ingested, chunked,
embedded, graphed and rubric'd, and still teach no one until someone runs this.

Publishing is a *gate*, not a formality. Every check here answers the question
"can the runtime actually teach this?", and each one corresponds to a way the
runtime fails at speed when the answer is no:

* A concept with no ``concept_sources`` row has no curated grounding, so the
  Lecturer falls back to subject-wide search and grounds a lecture in whatever
  is nearest in vector space.
* A load-bearing concept with no rubric criteria cannot be assessed, so a
  learner can complete it without anything checking they understood it.
* A cycle in the prerequisite graph means the Curator's sequencing walk does
  not terminate.
* An unclassified source means the corpus is being served on a rights basis
  nobody has established.

Each of those surfaces as a confusing runtime failure hours into a session.
Here they surface as a list of things to fix, before anyone has been taught
anything wrong.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.graph import GraphCycleError, assert_acyclic

from .licensing import HONEST_DEFAULT
from .provenance import ingestion_writer
from .queue import flag


@dataclass(frozen=True, slots=True)
class PublishCheck:
    name: str
    passed: bool
    detail: str = ""
    #: Which flag_source to queue when this check fails, if any. Validation
    #: failures that a reviewer must act on get a row; the license check does
    #: not, because §6.2 already queued a license_pending row per source and a
    #: second one would be the same task twice in the same queue.
    flag_source: str | None = None


@dataclass(frozen=True, slots=True)
class PublishResult:
    subject_slug: str
    published: bool
    checks: tuple[PublishCheck, ...] = ()
    flagged: tuple[uuid.UUID, ...] = ()

    @property
    def failures(self) -> tuple[PublishCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def render(self) -> str:
        lines = [
            f"  {'ok  ' if check.passed else 'FAIL'} {check.name}"
            + (f" -- {check.detail}" if check.detail else "")
            for check in self.checks
        ]
        return "\n".join(lines)


def validate(session: Session, subject_slug: str) -> tuple[uuid.UUID, list[PublishCheck]]:
    """Run every publish check. Returns the subject id and the results.

    All checks run even after one fails. A reviewer fixing a subject wants the
    full list -- publishing is a batch of authoring work, and finding the four
    problems one run at a time is four round trips through a domain expert's
    afternoon.
    """
    row = session.execute(
        sql(
            "SELECT id, status FROM subjects "
            "WHERE slug = :slug AND deleted_at IS NULL"
        ),
        {"slug": subject_slug},
    ).one_or_none()
    if row is None:
        raise LookupError(f"no subject {subject_slug!r}")

    subject_id = row.id
    checks = [
        _check_has_concepts(session, subject_id),
        _check_acyclic(session, subject_id),
        _check_concept_sources(session, subject_id),
        _check_rubric_coverage(session, subject_id),
        _check_licenses(session, subject_id),
    ]
    return subject_id, checks


@ingestion_writer
def publish_subject(session: Session, *, subject_slug: str) -> PublishResult:
    """Publish if every check passes; otherwise report why not (§9.3)."""
    subject_id, checks = validate(session, subject_slug)
    failures = [check for check in checks if not check.passed]

    if failures:
        flagged = tuple(
            flag(
                session,
                flag_source=check.flag_source,
                subject_id=subject_id,
                reason=f"publish blocked: {check.name} -- {check.detail}",
                payload={"check": check.name},
            )
            for check in failures
            if check.flag_source
        )
        return PublishResult(
            subject_slug=subject_slug,
            published=False,
            checks=tuple(checks),
            flagged=flagged,
        )

    session.execute(
        sql(
            """
            UPDATE subjects
               SET status = 'active', published_at = NOW()
             WHERE id = :id
            """
        ),
        {"id": subject_id},
    )
    # The sources a published subject serves become active with it. A subject
    # whose sources are still draft is published and teaches nothing -- the
    # retrieval layer filters on source status, so every lecture would ground
    # in an empty result set.
    session.execute(
        sql(
            """
            UPDATE sources
               SET status = 'active'
             WHERE subject_id = :id
               AND status = 'draft'
               AND deleted_at IS NULL
               AND ingested_at IS NOT NULL
            """
        ),
        {"id": subject_id},
    )
    session.execute(
        sql("SELECT refresh_subject_metadata(:id)"), {"id": subject_id}
    )

    return PublishResult(
        subject_slug=subject_slug, published=True, checks=tuple(checks)
    )


def _check_has_concepts(session: Session, subject_id: uuid.UUID) -> PublishCheck:
    count = session.execute(
        sql("SELECT COUNT(*) FROM concepts WHERE subject_id = :id"),
        {"id": subject_id},
    ).scalar_one()
    return PublishCheck(
        name="subject has concepts",
        passed=bool(count),
        detail="" if count else "the graph is empty; import it first",
        flag_source="graph_validation_error" if not count else None,
    )


def _check_acyclic(session: Session, subject_id: uuid.UUID) -> PublishCheck:
    """§9.3 step 1. Re-run at publish even though import already checked.

    Import validates the file; this validates the database. They differ when a
    graph is assembled from several imports, which is the normal way a subject
    grows -- each file is acyclic on its own and the union is not.
    """
    try:
        assert_acyclic(session, subject_id)
    except GraphCycleError as exc:
        return PublishCheck(
            name="prerequisite graph is acyclic",
            passed=False,
            detail=f"cycle: {exc}",
            flag_source="graph_validation_error",
        )
    return PublishCheck(name="prerequisite graph is acyclic", passed=True)


def _check_concept_sources(session: Session, subject_id: uuid.UUID) -> PublishCheck:
    """§9.3 step 2: every concept needs at least one curated pointer."""
    rows = session.execute(
        sql(
            """
            SELECT c.slug
              FROM concepts c
             WHERE c.subject_id = :id
               AND NOT EXISTS (
                     SELECT 1 FROM concept_sources cs
                      WHERE cs.concept_id = c.id
                        AND cardinality(cs.chunk_ids) > 0
               )
             ORDER BY c.position
            """
        ),
        {"id": subject_id},
    ).all()
    missing = [row.slug for row in rows]
    return PublishCheck(
        name="every concept has curated sources",
        passed=not missing,
        detail=(
            f"{len(missing)} concept(s) with no concept_sources: "
            f"{', '.join(missing[:8])}" + (" ..." if len(missing) > 8 else "")
            if missing
            else ""
        ),
        flag_source="concept_source_conflict" if missing else None,
    )


def _check_rubric_coverage(session: Session, subject_id: uuid.UUID) -> PublishCheck:
    """§9.3 step 3: load-bearing concepts need rubric criteria.

    Load-bearing only. Requiring a rubric for every concept would make a
    60-concept subject unpublishable until 60 rubrics exist, which is not how
    the work proceeds -- and §6.2's ``is_load_bearing`` flag exists precisely to
    name the concepts where the standard is higher.
    """
    rows = session.execute(
        sql(
            """
            SELECT c.slug
              FROM concepts c
             WHERE c.subject_id = :id
               AND c.is_load_bearing
               AND NOT EXISTS (
                     SELECT 1 FROM rubric_criteria r
                      WHERE r.concept_id = c.id
                        AND r.retired_at IS NULL
               )
             ORDER BY c.position
            """
        ),
        {"id": subject_id},
    ).all()
    missing = [row.slug for row in rows]
    return PublishCheck(
        name="load-bearing concepts have rubrics",
        passed=not missing,
        detail=(
            f"{len(missing)} load-bearing concept(s) with no rubric criteria: "
            f"{', '.join(missing[:8])}" + (" ..." if len(missing) > 8 else "")
            if missing
            else ""
        ),
        flag_source="rubric_validation_error" if missing else None,
    )


def _check_licenses(session: Session, subject_id: uuid.UUID) -> PublishCheck:
    """§14: publish is blocked while any source is unclassified.

    "Unclassified" is the honest default with no note -- see
    ``licensing.LicenseState.is_classified``. A source a reviewer looked at and
    left at ``permission_granted`` *with* a note has been classified; one still
    carrying the value the upload set has not.
    """
    rows = session.execute(
        sql(
            """
            SELECT title FROM sources
             WHERE subject_id = :id
               AND deleted_at IS NULL
               AND license = CAST(:default_license AS license_kind)
               AND COALESCE(BTRIM(license_notes), '') = ''
             ORDER BY created_at
            """
        ),
        {"id": subject_id, "default_license": HONEST_DEFAULT},
    ).all()
    titles = [row.title for row in rows]
    return PublishCheck(
        name="every source has a license determination",
        passed=not titles,
        detail=(
            f"{len(titles)} unclassified source(s): "
            f"{', '.join(repr(t) for t in titles[:5])}"
            + (" ..." if len(titles) > 5 else "")
            if titles
            else ""
        ),
        # No queue row: §6.2 already wrote a license_pending row for each of
        # these at upload, and a second row per publish attempt would grow the
        # queue by one every time a reviewer checked whether they were done.
        flag_source=None,
    )

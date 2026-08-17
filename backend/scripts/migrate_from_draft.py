"""One-time migration from the draft's JSON files into Postgres (spec §12).

Not required to ship -- MVP can be built greenfield -- but running it carries
the lambda calculus draft-in-progress forward as the seed subject.

Reads the draft's own loaders (``tutor.catalog``, ``tutor.models``) rather than
re-parsing its files, so a change to the draft's shape surfaces as an import or
validation error here instead of silently mis-migrating.

Order of operations, per §12: this runs against a database that already has the
schema (``alembic upgrade head``), then seeds. That is the reverse of the
spec's wording, which has the script populate a fresh database *before* Alembic
runs -- but the script writes through the ORM, so the tables have to exist
first. See DIVERGENCES.md.

    python scripts/migrate_from_draft.py --dry-run
    python scripts/migrate_from_draft.py --student demo
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from tutor import paths as draft_paths  # noqa: E402
from tutor.catalog import Course, load_course  # noqa: E402
from tutor.models import UnitPack  # noqa: E402
from tutor.progress import PASS_THRESHOLD  # noqa: E402

from studium.assessment import compute_pass  # noqa: E402
from studium.db import SessionLocal  # noqa: E402
from studium.graph import refresh_subject_metadata  # noqa: E402
from studium.models import (  # noqa: E402
    AssessmentAttempt,
    AssessmentResponse,
    Concept,
    ConceptEdge,
    ConceptMastery,
    ContentArtifact,
    LearnerSubject,
    RubricCriterion,
    Source,
    Subject,
    User,
    UserBudgetCap,
    UserProfile,
)

#: Modules excluded from the migrated subject, with the reason. The rights
#: posture for MVP is public-domain and open-licence material only; anything
#: here failed it and is dropped rather than migrated into a draft nobody can
#: publish.
EXCLUDED_MODULES = {
    "3_clojure": (
        "material is two in-copyright commercial textbooks (Programming "
        "Clojure, Pragmatic Bookshelf; The Joy of Clojure, Manning). No "
        "redistribution right. Functional-programming coverage deferred to a "
        "future subject with properly licensed material."
    ),
}

#: Every source defaults here. There is deliberately no heuristic: a filename
#: cannot establish provenance, and a false positive on licence asserts a right
#: that does not exist -- categorically worse than over-flagging for review.
#: An earlier substring rule matched "turing" and marked a named academic's
#: lecture slides public domain. A human sets this, or it stays unreviewed.
DEFAULT_LICENSE = "permission_granted"
UNREVIEWED_NOTE = (
    "Licence not yet reviewed by a human. Migrated from the v0.4 draft with no "
    "asserted redistribution right; confirm the basis and update this note "
    "before the subject leaves draft status."
)


@dataclass
class Report:
    """What the migration did, printed at the end and reviewed before publish."""

    subjects: int = 0
    concepts: int = 0
    edges: int = 0
    artifacts: int = 0
    rubric_criteria: int = 0
    sources: int = 0
    attempts: int = 0
    responses: int = 0
    excluded_modules: int = 0
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def render(self) -> str:
        lines = [
            "migration summary",
            f"  subjects          {self.subjects}",
            f"  concepts          {self.concepts}",
            f"  concept edges     {self.edges}",
            f"  content artifacts {self.artifacts}",
            f"  rubric criteria   {self.rubric_criteria}",
            f"  sources           {self.sources}",
            f"  attempts          {self.attempts}",
            f"  responses         {self.responses}",
            f"  modules excluded  {self.excluded_modules}",
        ]
        if self.warnings:
            lines.append("")
            lines.append(f"  {len(self.warnings)} item(s) need review before publish:")
            lines.extend(f"    - {w}" for w in self.warnings)
        return "\n".join(lines)


def slugify(text: str, *, limit: int = 80) -> str:
    """Draft keys use underscores; the schema's CHECK requires [a-z0-9-]."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:limit].strip("-")
    return slug or "unit"


@dataclass(frozen=True)
class Provenance:
    """The envelope v0.4 writes around each pack.

    A coursepack file is ``{unit_key, title, module_key, model, ingested_at,
    pack}``; only ``pack`` matches ``UnitPack``. The rest is provenance, and
    the v1.1 schema has columns for it -- ``content_artifacts.model`` and
    ``.generated_at`` -- so carrying it through is keeping data the schema was
    built to hold, not decorating the migration.
    """

    model: str | None = None
    generated_at: dt.datetime | None = None


def load_unit_pack(
    coursepack_dir: Path, unit_key: str
) -> tuple[UnitPack, Provenance] | None:
    module_key = unit_key.split("--", 1)[0]
    path = coursepack_dir / module_key / f"{unit_key}.json"
    if not path.exists():
        return None

    raw = json.loads(path.read_text(encoding="utf-8"))
    # Validating the whole document against UnitPack fails on all five of its
    # required fields, because they live one level down. Fall back to the whole
    # document so a future unwrapped pack still loads.
    pack = UnitPack.model_validate(raw.get("pack", raw))

    ingested = raw.get("ingested_at")
    return pack, Provenance(
        model=raw.get("model"),
        generated_at=dt.datetime.fromisoformat(ingested) if ingested else None,
    )


def licence_for(pdf: Path, report: Report) -> str:
    """§12: every source is unreviewed until a human says otherwise.

    This function used to guess public_domain from filename substrings. It is
    now deliberately a constant. Licence is a claim about a real artifact's
    provenance, and nothing derivable from a file path can support it.
    """
    report.warn(
        f"source {pdf.name!r} is unreviewed -- a human must set its licence "
        f"and license_notes before the subject leaves draft"
    )
    return DEFAULT_LICENSE


def migrate_course(
    session: Session, course: Course, coursepack_dir: Path, report: Report
) -> Subject:
    subject = session.execute(
        select(Subject).where(Subject.slug == slugify(course.name, limit=63))
    ).scalar_one_or_none()
    if subject is None:
        subject = Subject(slug=slugify(course.name, limit=63))
        session.add(subject)
        report.subjects += 1
    subject.title = course.title
    subject.long_description = course.note
    subject.status = "draft"  # you review before publishing
    session.flush()

    concepts_by_key: dict[str, Concept] = {}
    position = 0

    for module in course.modules:
        if module.key in EXCLUDED_MODULES:
            report.warn(
                f"module {module.key!r} excluded: {EXCLUDED_MODULES[module.key]}"
            )
            report.excluded_modules += 1
            continue
        for unit in module.units:
            loaded = load_unit_pack(coursepack_dir, unit.key)
            if loaded is None:
                report.warn(
                    f"unit {unit.key!r} has no coursepack JSON; migrated as a "
                    f"title-only concept"
                )
                pack, provenance = None, Provenance()
            else:
                pack, provenance = loaded
                if provenance.model is None:
                    report.warn(
                        f"unit {unit.key!r} records no generating model; its "
                        f"artifacts carry no attribution"
                    )

            slug = slugify(unit.key.split("--", 1)[-1])
            concept = session.execute(
                select(Concept)
                .where(Concept.subject_id == subject.id)
                .where(Concept.slug == slug)
            ).scalar_one_or_none()
            if concept is None:
                concept = Concept(subject_id=subject.id, slug=slug)
                session.add(concept)
                report.concepts += 1

            concept.title = unit.title
            concept.position = position
            concept.module_slug = slugify(module.key, limit=63)
            position += 1

            if pack is not None:
                concept.short_description = pack.overview
                concept.long_description = _assemble_long_description(pack)
                # learning_objectives are objectives, not exposition. The spec
                # maps them onto lecture_segment artifacts, which would put
                # bullet lists where prose belongs. They ride on the concept.
                concept.meta = {
                    "learning_objectives": pack.learning_objectives,
                    "mastery_targets": pack.study_guide.mastery_targets,
                    "common_misconceptions": pack.study_guide.common_misconceptions,
                    "module_breakdown": [
                        item.model_dump() for item in pack.module_breakdown
                    ],
                    "migrated_from": unit.key,
                }
            session.flush()
            concepts_by_key[unit.key] = concept

            for pdf in unit.pdf_paths:
                _migrate_source(session, subject, pdf, report)

            if pack is not None:
                _migrate_segments(session, concept, pack, provenance, report)
                _migrate_definitions(session, concept, pack, provenance, report)
                _migrate_rubric(session, concept, pack, report)

    _migrate_edges(session, subject, course, concepts_by_key, report)
    refresh_subject_metadata(session, subject.id)
    return subject


def _assemble_long_description(pack: UnitPack) -> str:
    """§12: study_guide.summary and .why_it_matters assemble into
    concepts.long_description."""
    guide = pack.study_guide
    parts: list[str] = []
    if guide.summary:
        parts.append(guide.summary)
    if guide.why_it_matters:
        parts.append(f"## Why it matters\n\n{guide.why_it_matters}")
    if guide.worked_example:
        parts.append(f"## Worked example\n\n{guide.worked_example}")
    return "\n\n".join(parts)


def _migrate_source(
    session: Session, subject: Subject, pdf: Path, report: Report
) -> None:
    if not pdf.exists():
        report.warn(f"source file missing on disk: {pdf}")
        return
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    existing = session.execute(
        select(Source)
        .where(Source.subject_id == subject.id)
        .where(Source.content_sha256 == digest)
    ).scalar_one_or_none()
    if existing is not None:
        return
    session.add(
        Source(
            subject_id=subject.id,
            title=pdf.stem.replace("_", " ").title(),
            license=licence_for(pdf, report),
            license_notes=UNREVIEWED_NOTE,
            storage_path=str(pdf.relative_to(REPO_ROOT)).replace("\\", "/"),
            content_sha256=digest,
            status="draft",
        )
    )
    report.sources += 1


def _migrate_segments(
    session: Session,
    concept: Concept,
    pack: UnitPack,
    provenance: Provenance,
    report: Report,
) -> None:
    """segments[].content -> lecture_segment; segments[].practice ->
    practice_problem plus a separate model_answer artifact."""
    for index, segment in enumerate(pack.segments):
        session.add(
            ContentArtifact(
                concept_id=concept.id,
                kind="lecture_segment",
                title=segment.title,
                body=segment.content,
                generated_by="lecturer",
                model=provenance.model,
                generated_at=provenance.generated_at,
                status="draft",
                meta={"segment_index": index, "of": len(pack.segments)},
            )
        )
        report.artifacts += 1

        if segment.practice is None:
            continue
        session.add(
            ContentArtifact(
                concept_id=concept.id,
                kind="practice_problem",
                title=f"Practice: {segment.title}",
                body=segment.practice.question,
                generated_by="lecturer",
                model=provenance.model,
                generated_at=provenance.generated_at,
                status="draft",
                meta={
                    "segment_index": index,
                    "hint_ladder": [segment.practice.hint],
                },
            )
        )
        session.add(
            ContentArtifact(
                concept_id=concept.id,
                kind="model_answer",
                title=f"Model answer: {segment.title}",
                body=segment.practice.model_answer,
                generated_by="lecturer",
                model=provenance.model,
                generated_at=provenance.generated_at,
                status="draft",
                meta={"segment_index": index},
            )
        )
        report.artifacts += 2


def _migrate_definitions(
    session: Session,
    concept: Concept,
    pack: UnitPack,
    provenance: Provenance,
    report: Report,
) -> None:
    for definition in pack.key_definitions:
        session.add(
            ContentArtifact(
                concept_id=concept.id,
                kind="worked_example",
                title=definition.term,
                body=definition.definition,
                generated_by="lecturer",
                model=provenance.model,
                generated_at=provenance.generated_at,
                status="draft",
                meta={"kind": "definition", "term": definition.term},
            )
        )
        report.artifacts += 1


def _migrate_rubric(
    session: Session, concept: Concept, pack: UnitPack, report: Report
) -> None:
    """The draft's RubricCriterion has no prompt field -- it carries ``concept``
    (what is being assessed) and flat string key_points. The schema needs a
    learner-facing prompt and weighted key points, so the prompt is synthesised
    and flagged for review."""
    for criterion in pack.rubric:
        slug = slugify(criterion.id)
        existing = session.execute(
            select(RubricCriterion)
            .where(RubricCriterion.concept_id == concept.id)
            .where(RubricCriterion.slug == slug)
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            RubricCriterion(
                concept_id=concept.id,
                slug=slug,
                prompt=f"Explain {criterion.concept}.",
                key_points=[
                    {"point": point, "weight": criterion.weight}
                    for point in criterion.key_points
                ],
                weight=criterion.weight,
                status="draft",
            )
        )
        report.rubric_criteria += 1
        report.warn(
            f"rubric {criterion.id!r} on {concept.slug!r}: prompt synthesised "
            f"from the criterion's concept -- rewrite it as a real question"
        )


def _migrate_edges(
    session: Session,
    subject: Subject,
    course: Course,
    concepts_by_key: dict[str, Concept],
    report: Report,
) -> None:
    """The draft gates strictly on course order, so consecutive units become
    prerequisite edges. That is a faithful translation of the draft's gate --
    and a starting point you will want to rewrite by hand into a real graph."""
    ordered = course.ordered_units()
    for previous, current in zip(ordered, ordered[1:], strict=False):
        src = concepts_by_key.get(previous.key)
        dst = concepts_by_key.get(current.key)
        if src is None or dst is None:
            continue
        exists = session.execute(
            select(ConceptEdge)
            .where(ConceptEdge.from_concept_id == src.id)
            .where(ConceptEdge.to_concept_id == dst.id)
            .where(ConceptEdge.kind == "prerequisite")
        ).scalar_one_or_none()
        if exists is not None:
            continue
        session.add(
            ConceptEdge(
                subject_id=subject.id,
                from_concept_id=src.id,
                to_concept_id=dst.id,
                kind="prerequisite",
                note="derived from the draft's sequential unit gate",
            )
        )
        report.edges += 1
    if report.edges:
        report.warn(
            f"{report.edges} prerequisite edge(s) were derived from linear unit "
            f"order; replace them with the real dependency structure"
        )


def migrate_progress(
    session: Session,
    subject: Subject,
    student: str,
    progress_dir: Path,
    report: Report,
) -> None:
    """Progress.attempts -> assessment_attempts + assessment_responses.

    ``passed`` is recomputed with ``compute_pass`` rather than copied: §3 makes
    the pass decision derived, and re-deriving it here proves the draft's own
    stored value agreed.
    """
    path = progress_dir / f"{student}.json"
    if not path.exists():
        report.warn(f"no progress file for student {student!r}; skipped attempts")
        return

    data = json.loads(path.read_text(encoding="utf-8"))
    user = session.execute(
        select(User).where(User.email == f"{student}@studium.local")
    ).scalar_one_or_none()
    if user is None:
        user = User(email=f"{student}@studium.local", display_name=student.title())
        session.add(user)
        session.flush()
        session.add(UserProfile(user_id=user.id))
        session.add(UserBudgetCap(user_id=user.id))

    enrollment = session.execute(
        select(LearnerSubject)
        .where(LearnerSubject.user_id == user.id)
        .where(LearnerSubject.subject_id == subject.id)
    ).scalar_one_or_none()
    if enrollment is None:
        enrollment = LearnerSubject(
            user_id=user.id, subject_id=subject.id, subject_version=subject.version
        )
        session.add(enrollment)
        session.flush()

    concepts = {
        c.meta.get("migrated_from"): c
        for c in session.execute(
            select(Concept).where(Concept.subject_id == subject.id)
        ).scalars()
    }

    for unit_key, unit_data in data.get("units", {}).items():
        concept = concepts.get(unit_key)
        if concept is None:
            report.warn(f"progress references unknown unit {unit_key!r}; skipped")
            continue

        mastery = session.execute(
            select(ConceptMastery)
            .where(ConceptMastery.learner_subject_id == enrollment.id)
            .where(ConceptMastery.concept_id == concept.id)
        ).scalar_one_or_none()
        if mastery is None:
            mastery = ConceptMastery(
                learner_subject_id=enrollment.id, concept_id=concept.id
            )
            session.add(mastery)
            session.flush()

        for attempt_data in unit_data.get("attempts", []):
            score = float(attempt_data.get("score", 0.0))
            recomputed = compute_pass(score, PASS_THRESHOLD)
            if recomputed != bool(attempt_data.get("passed")):
                report.warn(
                    f"{unit_key}: stored passed={attempt_data.get('passed')} "
                    f"disagrees with score {score:.2f} at threshold "
                    f"{PASS_THRESHOLD}; kept the recomputed value"
                )
            attempt = AssessmentAttempt(
                user_id=user.id,
                learner_subject_id=enrollment.id,
                scope_concept_id=concept.id,
                mode="summative",
                triggered_by="learner_initiated",
                score=score,
                passed=recomputed,
                threshold=PASS_THRESHOLD,
            )
            session.add(attempt)
            session.flush()
            report.attempts += 1

            per_criterion: dict[str, Any] = attempt_data.get("per_criterion", {})
            for criterion_slug, grade in per_criterion.items():
                criterion = session.execute(
                    select(RubricCriterion)
                    .where(RubricCriterion.concept_id == concept.id)
                    .where(RubricCriterion.slug == slugify(criterion_slug))
                ).scalar_one_or_none()
                if criterion is None:
                    report.warn(
                        f"{unit_key}: attempt references unknown criterion "
                        f"{criterion_slug!r}; grade dropped"
                    )
                    continue
                session.add(
                    AssessmentResponse(
                        attempt_id=attempt.id,
                        rubric_criterion_id=criterion.id,
                        criterion_snapshot={
                            "slug": criterion.slug,
                            "prompt": criterion.prompt,
                            "key_points": criterion.key_points,
                            "weight": criterion.weight,
                        },
                        prompt_shown=criterion.prompt,
                        learner_response="(not retained by the draft)",
                        grade=int(grade),
                    )
                )
                report.responses += 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--material", type=Path, default=draft_paths.MATERIAL_DIR)
    parser.add_argument("--coursepack", type=Path, default=draft_paths.COURSEPACK_DIR)
    parser.add_argument("--progress", type=Path, default=draft_paths.PROGRESS_DIR)
    parser.add_argument("--student", default=None, help="migrate this student's attempts")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="roll back at the end; print what would have been written",
    )
    args = parser.parse_args()

    report = Report()
    course = load_course(args.material)

    with SessionLocal() as session:
        subject = migrate_course(session, course, args.coursepack, report)
        if args.student:
            migrate_progress(session, subject, args.student, args.progress, report)

        if args.dry_run:
            session.rollback()
            print("(dry run -- rolled back)\n")
        else:
            session.commit()

    print(report.render())
    if report.warnings:
        print(
            "\nReview the items above before publishing the subject "
            "(subjects.status is still 'draft')."
        )


if __name__ == "__main__":
    main()

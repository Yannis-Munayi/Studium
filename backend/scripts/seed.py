"""Development seed (spec §13, "Local development").

"make db-reset drops and recreates the dev database, then runs migrations plus
a seed script that populates a demo user, the lambda calculus subject, and 10
sample concepts."

Idempotent: re-running updates the existing rows rather than duplicating them,
so it is safe to point at a database that already has data.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from studium.db import SessionLocal  # noqa: E402
from studium.graph import assert_acyclic, refresh_subject_metadata  # noqa: E402
from studium.models import (  # noqa: E402
    Concept,
    ConceptEdge,
    ConceptMastery,
    LearnerSubject,
    RubricCriterion,
    Source,
    Subject,
    User,
    UserBudgetCap,
    UserProfile,
)

DEMO_EMAIL = "demo@studium.local"
REVIEWER_EMAIL = "reviewer@studium.local"
SUBJECT_SLUG = "lambda-calculus"

#: slug, title, depth, load-bearing, minutes, short description
CONCEPTS: tuple[tuple[str, str, int, bool, int, str], ...] = (
    ("syntax", "Lambda terms: variables, abstraction, application", 1, True, 40,
     "The three term formers and how they nest."),
    ("alpha-equivalence", "Alpha-equivalence and variable capture", 2, True, 30,
     "When two terms are the same up to renaming bound variables."),
    ("beta-reduction", "Beta-reduction", 2, True, 45,
     "The computation step: substituting an argument for a bound variable."),
    ("eta-conversion", "Eta-conversion and extensionality", 3, False, 25,
     "When a function is the same as its point-free form."),
    ("normal-forms", "Normal forms and reduction strategies", 3, True, 45,
     "Normal order, applicative order, and what terminates."),
    ("church-rosser", "The Church-Rosser theorem", 4, True, 60,
     "Confluence: reduction order does not change the answer."),
    ("church-encoding", "Church encodings of data", 3, False, 50,
     "Numerals, booleans, and pairs as pure functions."),
    ("y-combinator", "Fixed-point combinators", 4, False, 45,
     "Recursion without naming yourself."),
    ("simply-typed", "The simply typed lambda calculus", 4, False, 55,
     "Adding types, and what they rule out."),
    ("normalization", "Strong normalisation", 5, False, 50,
     "Why every simply typed term terminates."),
)

EDGES: tuple[tuple[str, str, str], ...] = (
    ("syntax", "alpha-equivalence", "prerequisite"),
    ("syntax", "beta-reduction", "prerequisite"),
    ("alpha-equivalence", "beta-reduction", "prerequisite"),
    ("beta-reduction", "eta-conversion", "prerequisite"),
    ("beta-reduction", "normal-forms", "prerequisite"),
    ("normal-forms", "church-rosser", "prerequisite"),
    ("beta-reduction", "church-encoding", "prerequisite"),
    ("church-encoding", "y-combinator", "prerequisite"),
    ("normal-forms", "y-combinator", "dependency"),
    ("syntax", "simply-typed", "prerequisite"),
    ("simply-typed", "normalization", "prerequisite"),
    ("church-rosser", "normalization", "related"),
    ("simply-typed", "y-combinator", "related"),
)


def upsert_user(session: Session, email: str, name: str, role: str) -> User:
    user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        user = User(email=email, display_name=name, role=role)
        session.add(user)
        session.flush()
        session.add(UserProfile(user_id=user.id))
        session.add(UserBudgetCap(user_id=user.id))
    else:
        user.display_name = name
        user.role = role
    return user


def seed(session: Session) -> None:
    demo = upsert_user(session, DEMO_EMAIL, "Demo Learner", "learner")
    reviewer = upsert_user(session, REVIEWER_EMAIL, "Reviewer", "reviewer")

    subject = session.execute(
        select(Subject).where(Subject.slug == SUBJECT_SLUG)
    ).scalar_one_or_none()
    if subject is None:
        subject = Subject(slug=SUBJECT_SLUG, title="Lambda Calculus", version=1)
        session.add(subject)
    subject.short_description = "Syntax, reduction, encodings, and types."
    subject.long_description = (
        "A first course in the lambda calculus, from raw syntax through "
        "confluence and the simply typed fragment."
    )
    subject.status = "active"
    subject.authored_by = reviewer.id
    session.flush()

    concepts: dict[str, Concept] = {
        c.slug: c
        for c in session.execute(
            select(Concept).where(Concept.subject_id == subject.id)
        ).scalars()
    }

    for position, (slug, title, depth, load_bearing, minutes, blurb) in enumerate(
        CONCEPTS
    ):
        concept = concepts.get(slug)
        if concept is None:
            concept = Concept(subject_id=subject.id, slug=slug)
            session.add(concept)
            concepts[slug] = concept
        concept.title = title
        concept.depth = depth
        concept.is_load_bearing = load_bearing
        concept.estimated_minutes = minutes
        concept.position = position
        concept.short_description = blurb
        concept.meta = {"bkt": {"p_slip": 0.1, "p_guess": 0.2}}
    session.flush()

    existing_edges = {
        (e.from_concept_id, e.to_concept_id, e.kind)
        for e in session.execute(
            select(ConceptEdge).where(ConceptEdge.subject_id == subject.id)
        ).scalars()
    }
    for src, dst, kind in EDGES:
        key = (concepts[src].id, concepts[dst].id, kind)
        if key not in existing_edges:
            session.add(
                ConceptEdge(
                    subject_id=subject.id,
                    from_concept_id=key[0],
                    to_concept_id=key[1],
                    kind=kind,
                )
            )
    session.flush()

    # Publishing validates the graph. A cycle here is a seed bug, and it should
    # stop the seed rather than land in the database.
    assert_acyclic(session, subject.id)

    body = b"Church 1936, An Unsolvable Problem of Elementary Number Theory"
    digest = hashlib.sha256(body).hexdigest()
    source = session.execute(
        select(Source)
        .where(Source.subject_id == subject.id)
        .where(Source.content_sha256 == digest)
    ).scalar_one_or_none()
    if source is None:
        session.add(
            Source(
                subject_id=subject.id,
                title="An Unsolvable Problem of Elementary Number Theory",
                authors=["Alonzo Church"],
                publication_year=1936,
                license="public_domain",
                storage_path="material/2_lambda_calculus/church1936.pdf",
                content_sha256=digest,
                status="active",
            )
        )

    beta = concepts["beta-reduction"]
    has_rubric = session.execute(
        select(RubricCriterion).where(RubricCriterion.concept_id == beta.id)
    ).scalar_one_or_none()
    if has_rubric is None:
        session.add(
            RubricCriterion(
                concept_id=beta.id,
                slug="substitution",
                prompt="What does (M N) reduce to when M is an abstraction, and "
                "what has to be true about the variables involved?",
                key_points=[
                    {
                        "point": "(M N) reduces to M'[x := N] when M = (lambda x. M')",
                        "weight": 3,
                    },
                    {
                        "point": "Substitution must not capture free variables of N",
                        "weight": 3,
                    },
                    {"point": "A redex is an application whose left side is an "
                              "abstraction", "weight": 2},
                ],
                weight=3,
                status="active",
                reviewed_by=reviewer.id,
            )
        )

    enrollment = session.execute(
        select(LearnerSubject)
        .where(LearnerSubject.user_id == demo.id)
        .where(LearnerSubject.subject_id == subject.id)
    ).scalar_one_or_none()
    if enrollment is None:
        enrollment = LearnerSubject(
            user_id=demo.id, subject_id=subject.id, subject_version=subject.version
        )
        session.add(enrollment)
        session.flush()
    enrollment.syllabus_plan = [str(concepts[slug].id) for slug, *_ in CONCEPTS]
    enrollment.current_focus_concept_id = concepts["syntax"].id

    tracked = {
        m.concept_id
        for m in session.execute(
            select(ConceptMastery).where(
                ConceptMastery.learner_subject_id == enrollment.id
            )
        ).scalars()
    }
    for concept in concepts.values():
        if concept.id not in tracked:
            session.add(
                ConceptMastery(
                    learner_subject_id=enrollment.id,
                    concept_id=concept.id,
                    bkt_params=concept.meta.get("bkt", {}),
                )
            )

    session.flush()
    refresh_subject_metadata(session, subject.id)
    session.commit()

    print(f"seeded subject {subject.slug!r} with {len(concepts)} concepts")
    print(f"  demo learner: {DEMO_EMAIL}")
    print(f"  reviewer:     {REVIEWER_EMAIL}")


def main() -> None:
    with SessionLocal() as session:
        seed(session)


if __name__ == "__main__":
    main()

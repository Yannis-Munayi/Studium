"""The shared test fixture (spec §14, "Fixture data").

"backend/tests/fixtures/lambda.py builds a minimal lambda calculus subject with
6 concepts, 8 edges, 1 source, 20 chunks. Reused by all downstream tests."

Module name is ``lambda_calculus`` because ``lambda`` is a Python keyword and
``import lambda`` is a syntax error.

The concepts and their depth / load-bearing / minutes values are the example
rows from §6.2.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from studium.models import (
    Concept,
    ConceptEdge,
    ConceptMastery,
    LearnerSubject,
    RubricCriterion,
    Source,
    SourceChunk,
    Subject,
    User,
    UserBudgetCap,
    UserProfile,
)

SUBJECT_SLUG = "lambda-calculus"

#: slug, title, depth, load-bearing, estimated minutes
CONCEPTS: tuple[tuple[str, str, int, bool, int], ...] = (
    ("syntax", "Lambda terms: variables, abstraction, application", 1, True, 40),
    ("alpha-equivalence", "Alpha-equivalence and variable capture", 2, True, 30),
    ("beta-reduction", "Beta-reduction", 2, True, 45),
    ("church-rosser", "The Church-Rosser theorem", 4, True, 60),
    ("church-encoding", "Church encodings of data", 3, False, 50),
    ("y-combinator", "Fixed-point combinators", 4, False, 45),
)

#: from, to, kind. Six prerequisites give the graph a real gating spine; the
#: last two exercise the distinction the spec draws between a prerequisite and
#: a mere dependency or association.
EDGES: tuple[tuple[str, str, str], ...] = (
    ("syntax", "alpha-equivalence", "prerequisite"),
    ("syntax", "beta-reduction", "prerequisite"),
    ("alpha-equivalence", "beta-reduction", "prerequisite"),
    ("beta-reduction", "church-rosser", "prerequisite"),
    ("beta-reduction", "church-encoding", "prerequisite"),
    ("beta-reduction", "y-combinator", "prerequisite"),
    ("church-encoding", "y-combinator", "dependency"),
    ("church-rosser", "y-combinator", "related"),
)

CHUNK_COUNT = 20


@dataclass
class Fixture:
    """Handles onto everything the fixture created."""

    user: User
    subject: Subject
    concepts: dict[str, Concept]
    source: Source
    chunks: list[SourceChunk]
    enrollment: LearnerSubject
    mastery: dict[str, ConceptMastery] = field(default_factory=dict)

    def concept_id(self, slug: str) -> uuid.UUID:
        return self.concepts[slug].id


def build(session: Session, *, email: str = "learner@example.com") -> Fixture:
    """Create the fixture in the caller's transaction.

    Nothing is committed: the test harness wraps each test in a transaction it
    rolls back, so the fixture is rebuilt per test and never leaks.
    """
    user = User(email=email, display_name="Test Learner", role="learner")
    session.add(user)
    session.flush()

    session.add(UserProfile(user_id=user.id, stated_goals="Understand normalisation"))
    session.add(UserBudgetCap(user_id=user.id))

    subject = Subject(
        slug=SUBJECT_SLUG,
        title="Lambda Calculus",
        short_description="Syntax, reduction, and encodings.",
        status="active",
        version=1,
    )
    session.add(subject)
    session.flush()

    concepts: dict[str, Concept] = {}
    for position, (slug, title, depth, load_bearing, minutes) in enumerate(CONCEPTS):
        concept = Concept(
            subject_id=subject.id,
            slug=slug,
            title=title,
            depth=depth,
            is_load_bearing=load_bearing,
            estimated_minutes=minutes,
            position=position,
            meta={"bkt": {"p_slip": 0.1, "p_guess": 0.2}},
        )
        session.add(concept)
        concepts[slug] = concept
    session.flush()

    for src, dst, kind in EDGES:
        session.add(
            ConceptEdge(
                subject_id=subject.id,
                from_concept_id=concepts[src].id,
                to_concept_id=concepts[dst].id,
                kind=kind,
            )
        )

    body = b"Church, A. (1936). An Unsolvable Problem of Elementary Number Theory."
    source = Source(
        subject_id=subject.id,
        title="An Unsolvable Problem of Elementary Number Theory",
        authors=["Alonzo Church"],
        publication_year=1936,
        license="public_domain",
        storage_path="material/2_lambda_calculus/church1936.pdf",
        content_sha256=hashlib.sha256(body).hexdigest(),
        status="active",
    )
    session.add(source)
    session.flush()

    chunks = []
    for index in range(CHUNK_COUNT):
        chunk = SourceChunk(
            source_id=source.id,
            chunk_index=index,
            text_=f"Passage {index}: a lambda term is a variable, an abstraction, "
            f"or an application.",
            token_count=16,
            page_start=index // 4 + 1,
            page_end=index // 4 + 1,
            section_path=["Ch 1", f"1.{index // 4 + 1}"],
        )
        session.add(chunk)
        chunks.append(chunk)
    session.flush()

    session.add(
        RubricCriterion(
            concept_id=concepts["beta-reduction"].id,
            slug="substitution",
            prompt="Explain what (M N) reduces to when M is an abstraction.",
            key_points=[
                {
                    "point": "The application (M N) reduces to M'[x := N] "
                    "when M = (lambda x. M')",
                    "weight": 3,
                },
                {"point": "Reduction must avoid capturing free variables of N", "weight": 2},
            ],
            weight=3,
            status="active",
        )
    )

    enrollment = LearnerSubject(
        user_id=user.id,
        subject_id=subject.id,
        subject_version=subject.version,
        syllabus_plan=[str(concepts[slug].id) for slug, *_ in CONCEPTS],
    )
    session.add(enrollment)
    session.flush()

    fixture = Fixture(
        user=user,
        subject=subject,
        concepts=concepts,
        source=source,
        chunks=chunks,
        enrollment=enrollment,
    )

    for slug, concept in concepts.items():
        row = ConceptMastery(
            learner_subject_id=enrollment.id,
            concept_id=concept.id,
            bkt_params={"p_init": 0.1, "p_transit": 0.15, "p_slip": 0.1, "p_guess": 0.2},
        )
        session.add(row)
        fixture.mastery[slug] = row
    session.flush()

    return fixture

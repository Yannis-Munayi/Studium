"""Concept-graph invariants and the unlock test (spec §3, §6.2, §8).

Two things live here that the database deliberately does not enforce:

* **Acyclicity.** Postgres cannot cheaply reject a cycle without a recursive
  CTE on every insert, so the check runs at subject-publish time instead.
* **Unlocking.** A concept is unlocked when every prerequisite is mastered.
  §3 calls this "a graph reachability test"; the spec's own query in §8 checks
  only immediate parents. Both are implemented, ``transitive=False`` by
  default -- see DIVERGENCES.md (C2) for why that is the sane default.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .mastery import MASTERY_THRESHOLD
from .models import ConceptEdge


class GraphCycleError(ValueError):
    """A prerequisite cycle was found at publish time."""

    def __init__(self, cycle: list[uuid.UUID]) -> None:
        self.cycle = cycle
        super().__init__(" -> ".join(str(c) for c in cycle))


@dataclass(frozen=True, slots=True)
class UnlockStatus:
    concept_id: uuid.UUID
    required: int
    satisfied: int

    @property
    def unlocked(self) -> bool:
        return self.required == self.satisfied

    @property
    def missing(self) -> int:
        return self.required - self.satisfied


def assert_acyclic(session: Session, subject_id: uuid.UUID) -> None:
    """Reject prerequisite cycles. Called by the publish path and by the
    integrity tests (§14: "cycle detection catches A -> B -> A")."""
    edges = session.execute(
        select(ConceptEdge.from_concept_id, ConceptEdge.to_concept_id)
        .where(ConceptEdge.subject_id == subject_id)
        .where(ConceptEdge.kind == "prerequisite")
    ).all()

    adjacency: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for src, dst in edges:
        adjacency[src].append(dst)

    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[uuid.UUID, int] = defaultdict(int)
    stack: list[uuid.UUID] = []

    def visit(node: uuid.UUID) -> None:
        colour[node] = GREY
        stack.append(node)
        for nxt in adjacency.get(node, ()):
            if colour[nxt] == GREY:
                start = stack.index(nxt)
                raise GraphCycleError([*stack[start:], nxt])
            if colour[nxt] == WHITE:
                visit(nxt)
        stack.pop()
        colour[node] = BLACK

    for node in list(adjacency):
        if colour[node] == WHITE:
            visit(node)


def prerequisites(
    session: Session, concept_id: uuid.UUID, *, transitive: bool = False
) -> set[uuid.UUID]:
    """Concepts required before ``concept_id``.

    Direct parents by default. ``transitive=True`` walks the full ancestor set,
    which matters only when the authored graph omits transitive edges.
    """
    if not transitive:
        rows = session.execute(
            select(ConceptEdge.from_concept_id)
            .where(ConceptEdge.to_concept_id == concept_id)
            .where(ConceptEdge.kind == "prerequisite")
        ).scalars()
        return set(rows)

    sql = text(
        """
        WITH RECURSIVE ancestors(concept_id) AS (
            SELECT from_concept_id
              FROM concept_edges
             WHERE to_concept_id = :concept_id AND kind = 'prerequisite'
            UNION
            SELECT e.from_concept_id
              FROM concept_edges e
              JOIN ancestors a ON e.to_concept_id = a.concept_id
             WHERE e.kind = 'prerequisite'
        )
        SELECT concept_id FROM ancestors
        """
    )
    return set(session.execute(sql, {"concept_id": concept_id}).scalars())


def unlock_status(
    session: Session,
    *,
    learner_subject_id: uuid.UUID,
    concept_id: uuid.UUID,
    transitive: bool = False,
    threshold: float = MASTERY_THRESHOLD,
) -> UnlockStatus:
    """Whether a concept can be attempted.

    Decay is computed in SQL from ``p_known`` and ``last_evidence_at`` rather
    than read from the ``p_known_decayed`` column, which is only as fresh as
    the last decay job. Gating on a day-stale value would let a learner through
    on evidence that has already faded. See DIVERGENCES.md (C1).
    """
    required = prerequisites(session, concept_id, transitive=transitive)
    if not required:
        return UnlockStatus(concept_id=concept_id, required=0, satisfied=0)

    sql = text(
        """
        SELECT count(*) FILTER (
            WHERE cm.p_known
                  * pow(0.5, EXTRACT(EPOCH FROM (NOW() - cm.last_evidence_at))
                             / :half_life_seconds)
                  >= :threshold
        ) AS satisfied
          FROM unnest(CAST(:required AS uuid[])) AS req(concept_id)
          LEFT JOIN concept_mastery cm
                 ON cm.concept_id = req.concept_id
                AND cm.learner_subject_id = :learner_subject_id
        """
    )
    from .mastery import DECAY_HALF_LIFE

    satisfied = session.execute(
        sql,
        {
            "required": list(required),
            "learner_subject_id": learner_subject_id,
            "threshold": threshold,
            "half_life_seconds": DECAY_HALF_LIFE.total_seconds(),
        },
    ).scalar_one()

    return UnlockStatus(
        concept_id=concept_id, required=len(required), satisfied=int(satisfied)
    )


def refresh_subject_metadata(session: Session, subject_id: uuid.UUID) -> None:
    """Recompute the graph statistics for one subject.

    Thin wrapper over the stored procedure so callers do not hand-write SQL;
    invoked after graph mutations rather than from a row trigger.
    """
    session.execute(
        text("SELECT refresh_subject_metadata(:subject_id)"), {"subject_id": subject_id}
    )

"""Bayesian Knowledge Tracing: the mastery model (spec §3, §6.5).

No table stores a value computed by asking a model "did the learner pass?" or
"is this concept mastered?". Mastery is a number derived here from the
evidence log; passing is a comparison against a threshold; unlocking is a
graph test. This module owns the first of those.

Every ``apply_evidence`` call writes a ``mastery_events`` row and updates
``concept_mastery`` in one transaction, so the current estimate is always
reconstructible from its history -- and re-derivable under corrected BKT
parameters without losing evidence.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ConceptMastery, MasteryEvent

#: Gating threshold for prerequisite checks. Defined here, not in the database,
#: so it can be tuned without a migration (§6.5).
MASTERY_THRESHOLD = 0.85

#: Exponential forgetting curve half-life. A concept at p_known = 1.0 with no
#: further evidence decays to 0.5 after this long. The Reviewer's FSRS
#: scheduling is the real retention model; this is the cheap read-time proxy
#: the Curator uses for gating.
DECAY_HALF_LIFE = dt.timedelta(days=30)

#: Evidence kinds that count as a correct observation for the BKT update.
CORRECT_KINDS = frozenset(
    {
        "lecture_check_correct",
        "tutorial_turn_success",
        "tutorial_turn_recovery",
        "practice_correct",
        "review_correct",
    }
)

#: Kinds that count as incorrect.
INCORRECT_KINDS = frozenset(
    {
        "lecture_check_incorrect",
        "tutorial_turn_stuck",
        "practice_incorrect",
        "review_incorrect",
    }
)

#: Kinds carrying a partial or numeric result; the caller supplies ``correct``.
GRADED_KINDS = frozenset({"practice_partial", "assessment_scored"})

#: Kinds that bypass the BKT update entirely.
ADMINISTRATIVE_KINDS = frozenset({"manual_adjustment", "decay_refresh"})


#: The spec's illustrative BKT defaults (§6.5). Tuning them is the Agent
#: Runtime spec's job. Kept as module constants rather than read back off the
#: dataclass: with slots=True, ``BKTParams.p_init`` is a member descriptor, not
#: the default value.
DEFAULT_BKT = {
    "p_init": 0.1,
    "p_transit": 0.15,
    "p_slip": 0.1,
    "p_guess": 0.2,
}


@dataclass(frozen=True, slots=True)
class BKTParams:
    """Per-concept BKT parameters.

    The schema holds these per learner row, so per-learner tuning is a later
    refinement rather than a migration.
    """

    p_init: float = DEFAULT_BKT["p_init"]
    p_transit: float = DEFAULT_BKT["p_transit"]
    p_slip: float = DEFAULT_BKT["p_slip"]
    p_guess: float = DEFAULT_BKT["p_guess"]

    @classmethod
    def from_jsonb(cls, blob: dict[str, Any] | None) -> BKTParams:
        """Build from ``concept_mastery.bkt_params``, filling any missing key.

        The column defaults to ``'{}'``, so the empty case is the common one.
        """
        blob = blob or {}
        return cls(
            p_init=float(blob.get("p_init", DEFAULT_BKT["p_init"])),
            p_transit=float(blob.get("p_transit", DEFAULT_BKT["p_transit"])),
            p_slip=float(blob.get("p_slip", DEFAULT_BKT["p_slip"])),
            p_guess=float(blob.get("p_guess", DEFAULT_BKT["p_guess"])),
        )

    def as_jsonb(self) -> dict[str, float]:
        return {
            "p_init": self.p_init,
            "p_transit": self.p_transit,
            "p_slip": self.p_slip,
            "p_guess": self.p_guess,
        }


def posterior(p_known: float, correct: bool, params: BKTParams) -> float:
    """One BKT update step.

    Standard two-step form: condition on the observation, then apply the
    learning transition.
    """
    if correct:
        num = p_known * (1.0 - params.p_slip)
        den = num + (1.0 - p_known) * params.p_guess
    else:
        num = p_known * params.p_slip
        den = num + (1.0 - p_known) * (1.0 - params.p_guess)

    conditioned = num / den if den > 0 else p_known
    updated = conditioned + (1.0 - conditioned) * params.p_transit
    return _clamp(updated)


def decayed(
    p_known: float,
    last_evidence_at: dt.datetime | None,
    *,
    now: dt.datetime | None = None,
    half_life: dt.timedelta = DECAY_HALF_LIFE,
) -> float:
    """Apply the forgetting curve to a raw posterior.

    ``concept_mastery.p_known_decayed`` is only as fresh as the last decay job,
    so anything that gates on mastery calls this rather than reading the
    column. The column stays as a materialised value for sorting and reporting.
    """
    if last_evidence_at is None:
        return _clamp(p_known)
    now = now or dt.datetime.now(dt.UTC)
    if last_evidence_at.tzinfo is None:
        last_evidence_at = last_evidence_at.replace(tzinfo=dt.UTC)
    age = (now - last_evidence_at).total_seconds()
    if age <= 0:
        return _clamp(p_known)
    return _clamp(p_known * math.pow(0.5, age / half_life.total_seconds()))


def is_mastered(
    p_known: float,
    last_evidence_at: dt.datetime | None,
    *,
    now: dt.datetime | None = None,
) -> bool:
    return decayed(p_known, last_evidence_at, now=now) >= MASTERY_THRESHOLD


def apply_evidence(
    session: Session,
    *,
    concept_mastery_id: uuid.UUID,
    kind: str,
    session_id: uuid.UUID | None = None,
    correct: bool | None = None,
    evidence: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> ConceptMastery:
    """Record one piece of evidence and update the mastery estimate.

    Writes the event and the new estimate in the caller's transaction. Run it
    through ``db.with_serialization_retry`` at REPEATABLE READ so concurrent
    evidence for the same concept cannot lose an update.
    """
    now = now or dt.datetime.now(dt.UTC)

    row = session.execute(
        select(ConceptMastery).where(ConceptMastery.id == concept_mastery_id)
    ).scalar_one()

    before = float(row.p_known)

    if kind in ADMINISTRATIVE_KINDS:
        after = before if correct is None else _clamp(float(correct))
    else:
        observed = _resolve_correct(kind, correct)
        after = posterior(before, observed, BKTParams.from_jsonb(row.bkt_params))

    session.add(
        MasteryEvent(
            concept_mastery_id=concept_mastery_id,
            session_id=session_id,
            kind=kind,
            p_known_before=before,
            p_known_after=after,
            evidence=evidence or {},
        )
    )

    row.p_known = after
    row.p_known_decayed = after  # fresh as of now; the decay job re-derives later
    row.evidence_count = row.evidence_count + 1
    row.last_evidence_at = now
    if row.first_reached_mastery_at is None and after >= MASTERY_THRESHOLD:
        row.first_reached_mastery_at = now

    return row


def _resolve_correct(kind: str, correct: bool | None) -> bool:
    if kind in CORRECT_KINDS:
        return True
    if kind in INCORRECT_KINDS:
        return False
    if kind in GRADED_KINDS:
        if correct is None:
            raise ValueError(f"{kind!r} requires an explicit correct= value")
        return correct
    raise ValueError(f"unknown mastery event kind: {kind!r}")


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))

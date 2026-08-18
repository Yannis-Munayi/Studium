"""Applying ``ToolEffect``s to the data layer (agent runtime §8).

Agents decide that something should be written; this applies it. Keeping the
decision and the write separate is what makes a retried model call safe -- the
first attempt wrote nothing, so the second cannot double-apply.

**All effects for a turn go in one transaction.** §8: "If any effect fails, all
effects for the turn are rolled back and an error is logged; the turn itself
(the ``session_turns`` row) still writes, so the trace remains complete." That
ordering holds here because traces are written by the client wrapper at the
point of the call, before any effect is applied -- so a failed effect batch
leaves a complete cost record and an incomplete state change, which is the
recoverable direction.

The module is a dispatch table rather than a chain of ``if`` branches so an
unrecognised effect kind fails loudly at the boundary instead of being skipped.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.agents.base import ToolEffect
from studium.asyncdb import run_db
from studium.mastery import DEFAULT_BKT, apply_evidence
from studium.models import (
    ConceptMastery,
    ContentArtifact,
    ContentCitation,
    ContentReviewQueueItem,
    JournalEntry,
    JournalEvent,
    PortfolioItem,
    ReviewCard,
    ReviewEvent,
)
from studium.queries import open_journal_entry

log = logging.getLogger(__name__)


class EffectApplicationError(RuntimeError):
    """The effect batch failed and was rolled back."""

    def __init__(self, kind: str, cause: BaseException) -> None:
        self.kind = kind
        super().__init__(f"applying {kind!r} failed: {cause}")
        self.__cause__ = cause


async def apply_effects(
    effects: Sequence[ToolEffect], *, session_turn_id: uuid.UUID | None = None
) -> list[str]:
    """Apply a turn's effects atomically. Returns the kinds applied."""
    if not effects:
        return []
    return await run_db(lambda s: _apply_all(s, effects, session_turn_id))


def _apply_all(
    session: Session,
    effects: Sequence[ToolEffect],
    session_turn_id: uuid.UUID | None,
) -> list[str]:
    applied: list[str] = []
    for effect in effects:
        handler = HANDLERS.get(effect.kind)
        if handler is None:  # pragma: no cover -- ToolEffect validates on construction
            raise EffectApplicationError(effect.kind, KeyError(effect.kind))
        try:
            handler(session, effect.payload, session_turn_id)
        except Exception as exc:  # noqa: BLE001 -- wrapped and re-raised
            log.exception("effect %s failed; rolling back the batch", effect.kind)
            raise EffectApplicationError(effect.kind, exc) from exc
        applied.append(effect.kind)
    return applied


# --- handlers --------------------------------------------------------------


def _mastery_row(
    session: Session,
    *,
    learner_subject_id: uuid.UUID,
    concept_id: uuid.UUID,
) -> ConceptMastery:
    """Get or create the ``concept_mastery`` row for this pair.

    First evidence on a concept has no row yet. BKT parameters are copied from
    ``concepts.metadata`` at creation (data layer §6.5), falling back to the
    spec's defaults where an author has not tuned them.
    """
    row = session.execute(
        select(ConceptMastery)
        .where(ConceptMastery.learner_subject_id == learner_subject_id)
        .where(ConceptMastery.concept_id == concept_id)
    ).scalar_one_or_none()

    if row is not None:
        return row

    authored = session.execute(
        sql("SELECT metadata -> 'bkt_params' FROM concepts WHERE id = :cid"),
        {"cid": concept_id},
    ).scalar()

    row = ConceptMastery(
        learner_subject_id=learner_subject_id,
        concept_id=concept_id,
        bkt_params=authored or dict(DEFAULT_BKT),
    )
    session.add(row)
    session.flush()
    return row


def _record_mastery_evidence(
    session: Session, payload: dict[str, Any], turn_id: uuid.UUID | None
) -> None:
    row = _mastery_row(
        session,
        learner_subject_id=uuid.UUID(payload["learner_subject_id"]),
        concept_id=uuid.UUID(payload["concept_id"]),
    )
    apply_evidence(
        session,
        concept_mastery_id=row.id,
        kind=payload["kind"],
        session_id=_maybe_uuid(payload.get("session_id")),
        correct=payload.get("correct"),
        evidence=payload.get("evidence") or {},
    )


def _update_journal(
    session: Session, payload: dict[str, Any], turn_id: uuid.UUID | None
) -> None:
    """Create, revise, or resolve a journal entry, with its event (§6.7)."""
    action = payload["action"]
    now = dt.datetime.now(dt.UTC)

    if action == "create_entry":
        entry = open_journal_entry(
            session,
            user_id=uuid.UUID(payload["user_id"]),
            learner_subject_id=uuid.UUID(payload["learner_subject_id"]),
            summary=payload["summary"],
            hypothesis=payload.get("hypothesis") or "",
            concept_id=_maybe_uuid(payload.get("concept_id")),
            session_id=_maybe_uuid(payload.get("session_id")),
            origin=payload.get("origin") or "tracker_inferred",
            note=payload.get("reasoning") or "",
        )
        log.debug("journal entry %s opened", entry.id)
        return

    entry_id = uuid.UUID(payload["entry_id"])
    entry = session.get(JournalEntry, entry_id)
    if entry is None:
        # The entry was archived or erased between the Tracker's decision and
        # this write. Dropping is correct: there is nothing to revise.
        log.info("journal entry %s no longer exists; skipping %s", entry_id, action)
        return

    if action == "revise_entry":
        entry.hypothesis = payload.get("hypothesis") or entry.hypothesis
        entry.last_touched_at = now
        kind = "hypothesis_updated"
    else:  # flag_resolved
        entry.status = "resolved"
        entry.resolved_at = now
        entry.last_touched_at = now
        kind = "resolved"

    session.add(
        JournalEvent(
            entry_id=entry.id,
            session_id=_maybe_uuid(payload.get("session_id")),
            kind=kind,
            note=payload.get("reasoning") or "",
        )
    )


def _record_content_artifact(
    session: Session, payload: dict[str, Any], turn_id: uuid.UUID | None
) -> None:
    """Persist a generated segment and its citations (§10).

    Written as ``draft``: §6.4's lifecycle is draft -> reviewed -> active, and
    promoting straight to active would make an unreviewed segment the cached
    artifact the next learner reads.
    """
    artifact = ContentArtifact(
        concept_id=uuid.UUID(payload["concept_id"]),
        kind=payload["kind"],
        stance=payload.get("stance") or "default",
        title=payload.get("title") or "",
        body=payload["body"],
        meta=payload.get("metadata") or {},
        generated_by=payload.get("generated_by") or "lecturer",
        model=payload.get("model"),
        prompt_hash=payload.get("prompt_hash"),
        cost_usd=payload.get("cost_usd"),
        status="draft",
        generated_for_session_id=_maybe_uuid(payload.get("session_id")),
    )
    session.add(artifact)
    session.flush()

    seen: set[uuid.UUID] = set()
    for citation in payload.get("citations") or []:
        chunk_id = uuid.UUID(citation["source_chunk_id"])
        if chunk_id in seen:
            continue  # uq_content_citations_pair
        seen.add(chunk_id)
        session.add(
            ContentCitation(
                artifact_id=artifact.id,
                source_chunk_id=chunk_id,
                quoted_span=citation.get("quoted_span"),
                note=citation.get("note"),
            )
        )


def _schedule_review(
    session: Session, payload: dict[str, Any], turn_id: uuid.UUID | None
) -> None:
    """Write the FSRS result to the card and its event log (§6.9)."""
    card = session.get(ReviewCard, uuid.UUID(payload["card_id"]))
    if card is None:
        log.info("review card %s no longer exists; skipping", payload["card_id"])
        return

    session.add(
        ReviewEvent(
            card_id=card.id,
            session_id=uuid.UUID(payload["session_id"]),
            artifact_id=_maybe_uuid(payload.get("artifact_id")),
            rating=int(payload["rating"]),
            response_text=payload.get("response_text") or "",
            elapsed_seconds=payload.get("elapsed_seconds"),
            stability_before=float(payload["stability_before"]),
            stability_after=float(payload["stability_after"]),
            difficulty_before=float(payload["difficulty_before"]),
            difficulty_after=float(payload["difficulty_after"]),
        )
    )

    card.stability = float(payload["stability_after"])
    card.difficulty = float(payload["difficulty_after"])
    card.retrievability = float(payload["retrievability"])
    card.state = payload["state"]
    card.reps = int(payload["reps"])
    card.lapses = int(payload["lapses"])
    if payload.get("due_at"):
        card.due_at = dt.datetime.fromisoformat(payload["due_at"])
    if payload.get("last_reviewed_at"):
        card.last_reviewed_at = dt.datetime.fromisoformat(payload["last_reviewed_at"])


def _record_portfolio_item(
    session: Session, payload: dict[str, Any], turn_id: uuid.UUID | None
) -> None:
    """Append to the learner's hash-chained portfolio (§6.10).

    ``chain_index`` and ``prev_hash`` are derived here rather than supplied:
    the chain is what makes "did the learner actually do this work" verifiable,
    and letting a caller pass its own index would let a gap or a fork through.
    """
    import hashlib

    learner_subject_id = uuid.UUID(payload["learner_subject_id"])
    body = payload["body"]

    tail = session.execute(
        sql(
            """
            SELECT chain_index, content_sha256
              FROM portfolio_items
             WHERE learner_subject_id = :lsid
             ORDER BY chain_index DESC
             LIMIT 1
             FOR UPDATE
            """
        ),
        {"lsid": learner_subject_id},
    ).first()

    next_index = (tail.chain_index + 1) if tail else 0
    prev_hash = tail.content_sha256 if tail else ""
    digest = hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()

    session.add(
        PortfolioItem(
            user_id=uuid.UUID(payload["user_id"]),
            learner_subject_id=learner_subject_id,
            chain_index=next_index,
            concept_id=_maybe_uuid(payload.get("concept_id")),
            session_id=_maybe_uuid(payload.get("session_id")),
            kind=payload.get("kind") or "prose",
            title=payload.get("title") or "",
            body=body,
            language=payload.get("language"),
            content_sha256=digest,
        )
    )


def _flag_for_review(
    session: Session, payload: dict[str, Any], turn_id: uuid.UUID | None
) -> None:
    """Enqueue a review item (§21 "Escalation to reviewer").

    ``content_review_queue`` CHECKs that at least one of artifact_id or
    session_turn_id is set, so the turn is attached when the caller did not
    name an artifact -- otherwise a flag with no subject is rejected at insert
    and takes the whole effect batch down with it.
    """
    artifact_id = _maybe_uuid(payload.get("artifact_id"))
    linked_turn = _maybe_uuid(payload.get("session_turn_id")) or turn_id

    if artifact_id is None and linked_turn is None:
        log.warning("dropping review flag with no target: %s", payload.get("reason"))
        return

    session.add(
        ContentReviewQueueItem(
            artifact_id=artifact_id,
            session_turn_id=linked_turn,
            source=payload.get("source") or "system_confidence",
            reason=payload["reason"],
            severity=int(payload.get("severity", 2)),
        )
    )


HANDLERS = {
    "record_mastery_evidence": _record_mastery_evidence,
    "update_journal": _update_journal,
    "record_content_artifact": _record_content_artifact,
    "schedule_review": _schedule_review,
    "record_portfolio_item": _record_portfolio_item,
    "flag_for_review": _flag_for_review,
}


def _maybe_uuid(value: Any) -> uuid.UUID | None:
    if value in (None, "", "None"):
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

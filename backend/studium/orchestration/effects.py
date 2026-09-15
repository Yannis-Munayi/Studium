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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.agents.base import ToolEffect
from studium.asyncdb import run_db
from studium.mastery import DEFAULT_BKT, apply_evidence
from studium.models import (
    CREDENTIAL_KINDS,
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


@dataclass(slots=True)
class AppliedEffects:
    """What a turn's effect batch produced.

    This used to be a ``list[str]`` of kinds, which was enough while nothing
    downstream cared *which rows* a batch wrote. The ``end`` chunk now has to
    carry the id of the artifact a lecture segment became, so the client can
    resolve its ``[Pn]`` markers -- and that id does not exist until this batch
    commits (SPEC_DEBT SD5). Returning it is the whole reason for the type.
    """

    kinds: list[str] = field(default_factory=list)
    artifact_ids: list[uuid.UUID] = field(default_factory=list)

    # v1.0.1 §4.3's remaining four. Scalars rather than lists because unlike
    # artifacts these have exactly one meaningful value per turn: a turn
    # revises one journal entry, appends one portfolio item, schedules one
    # card. First-wins if that ever stops being true, which the id-collection
    # below makes visible rather than silently overwriting.
    journal_entry_id: uuid.UUID | None = None
    portfolio_item_id: uuid.UUID | None = None
    review_card_id: uuid.UUID | None = None
    queue_item_id: uuid.UUID | None = None

    @property
    def artifact_id(self) -> uuid.UUID | None:
        """The first artifact this batch wrote, if any.

        First rather than only: a turn writes at most one ``lecture_segment``
        today, and *that* is the one a segment's citations belong to. If a turn
        ever writes two, the ``end`` chunk naming the first is wrong in a way a
        list on the chunk would not fix -- the client would still not know which
        artifact the prose it just rendered came from. Kept as a list so that
        case is visible here rather than lost at the call site.
        """
        return self.artifact_ids[0] if self.artifact_ids else None

    def end_chunk_ids(self) -> dict[str, str]:
        """The id fields for the ``end`` chunk, omitting the absent ones.

        §4.2 makes every id optional because not every turn produces every
        effect kind. Emitting the nulls would make a client's "did this turn
        produce an artifact" check a truthiness test on a key that is always
        present -- easy to get subtly wrong. Absent means absent.
        """
        produced = {
            "artifact_id": self.artifact_id,
            "journal_entry_id": self.journal_entry_id,
            "portfolio_item_id": self.portfolio_item_id,
            "review_card_id": self.review_card_id,
            "queue_item_id": self.queue_item_id,
        }
        return {name: str(value) for name, value in produced.items() if value is not None}


#: Which :class:`AppliedEffects` field an effect kind's produced id lands in
#: (§4.3). An effect whose kind is absent here produces no client-visible id.
ID_FIELD_FOR_KIND: dict[str, str] = {
    "record_content_artifact": "artifact_id",
    "update_journal": "journal_entry_id",
    "record_portfolio_item": "portfolio_item_id",
    "schedule_review": "review_card_id",
    "flag_for_review": "queue_item_id",
}


#: An effect handler. Returns the id of the row it wrote, so the ``end`` chunk
#: can name it (§4.2), or ``None`` when it wrote nothing addressable --
#: ``record_mastery_evidence`` (the id a client would want is the concept's,
#: which it already has), and any handler that skipped because its target row
#: had been erased between the agent's decision and this write.
EffectHandler = Callable[[Session, dict[str, Any], uuid.UUID | None], uuid.UUID | None]


async def apply_effects(
    effects: Sequence[ToolEffect], *, session_turn_id: uuid.UUID | None = None
) -> AppliedEffects:
    """Apply a turn's effects atomically."""
    if not effects:
        return AppliedEffects()
    return await run_db(lambda s: _apply_all(s, effects, session_turn_id))


def _apply_all(
    session: Session,
    effects: Sequence[ToolEffect],
    session_turn_id: uuid.UUID | None,
) -> AppliedEffects:
    result = AppliedEffects()
    for effect in effects:
        handler = HANDLERS.get(effect.kind)
        if handler is None:  # pragma: no cover -- ToolEffect validates on construction
            raise EffectApplicationError(effect.kind, KeyError(effect.kind))
        try:
            written = handler(session, effect.payload, session_turn_id)
        except Exception as exc:  # noqa: BLE001 -- wrapped and re-raised
            log.exception("effect %s failed; rolling back the batch", effect.kind)
            raise EffectApplicationError(effect.kind, exc) from exc
        result.kinds.append(effect.kind)
        if written is not None:
            _record_id(result, effect.kind, written)
    return result


def _record_id(result: AppliedEffects, kind: str, written: uuid.UUID) -> None:
    """File a produced id under the field §4.3 assigns to its effect kind."""
    field_name = ID_FIELD_FOR_KIND.get(kind)
    if field_name is None:  # pragma: no cover -- handler returned an unclaimed id
        log.debug("effect %s produced id %s with nowhere to file it", kind, written)
        return
    if field_name == "artifact_id":
        result.artifact_ids.append(written)
        return
    if getattr(result, field_name) is None:
        setattr(result, field_name, written)
    else:
        # Two of the same kind in one batch. Keeping the first matches the
        # artifact rule; logging it means the assumption is checkable rather
        # than merely stated.
        log.info(
            "turn produced a second %s (%s); the end chunk names the first",
            kind, written,
        )


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
) -> uuid.UUID | None:
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
) -> uuid.UUID | None:
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
        return entry.id

    entry_id = uuid.UUID(payload["entry_id"])
    entry = session.get(JournalEntry, entry_id)
    if entry is None:
        # The entry was archived or erased between the Tracker's decision and
        # this write. Dropping is correct: there is nothing to revise -- and
        # returning no id is right too, since the end chunk must not name a row
        # that is not there.
        log.info("journal entry %s no longer exists; skipping %s", entry_id, action)
        return None

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
    return entry.id


def _record_content_artifact(
    session: Session, payload: dict[str, Any], turn_id: uuid.UUID | None
) -> uuid.UUID | None:
    """Persist a generated segment and its citations (§10).

    Written as ``draft``: §6.4's lifecycle is draft -> reviewed -> active, and
    promoting straight to active would make an unreviewed segment the cached
    artifact the next learner reads.

    **Returns the new artifact's id**, which is the only handler that does. Two
    consumers need it and neither could have it before: the ``end`` chunk, so
    the client can resolve the segment's ``[Pn]`` markers (SD5), and
    ``session_turns.artifact_id``, which the data layer declares (§6.6) and
    which nothing had ever written. The second matters beyond tidiness -- the
    chunk is delivered once and a reloaded page has no way back to it, so the
    row is where "which artifact was this turn" survives a refresh.
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

    # ``artifact_id IS NULL`` rather than an unconditional set: a turn that
    # somehow produced two artifacts keeps its link to the first, matching what
    # ``AppliedEffects.artifact_id`` reports to the client. Two writers
    # disagreeing about which artifact a turn is would be worse than one
    # being incomplete.
    if turn_id is not None:
        session.execute(
            sql(
                """
                UPDATE session_turns
                   SET artifact_id = :artifact_id
                 WHERE id = :turn_id AND artifact_id IS NULL
                """
            ),
            {"artifact_id": artifact.id, "turn_id": turn_id},
        )

    return artifact.id


def _schedule_review(
    session: Session, payload: dict[str, Any], turn_id: uuid.UUID | None
) -> uuid.UUID | None:
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
    return card.id


def _record_portfolio_item(
    session: Session, payload: dict[str, Any], turn_id: uuid.UUID | None
) -> uuid.UUID | None:
    """Append to the learner's hash-chained portfolio (§6.10).

    ``chain_index`` and ``prev_hash`` are derived here rather than supplied:
    the chain is what makes "did the learner actually do this work" verifiable,
    and letting a caller pass its own index would let a gap or a fork through.

    **The signature manifest.** ``portfolio_items.signature`` is ``NOT NULL``
    with no default and this handler did not build one, so every
    ``record_portfolio_item`` effect raised ``NotNullViolation`` at insert --
    and since :func:`_apply_all` rolls the whole batch back when a handler
    raises, the turn lost its mastery evidence and its journal update as well.
    Nothing covered the path. Fixed here by building the manifest data layer
    §6.10 specifies; see DIVERGENCES-EVALUATION (E9).

    A work item is written **unsigned** when no key is configured, rather than
    failing. The chain digest is its tamper-evidence and the signature is
    additional, so refusing to record a learner's proof because a development
    box has no ``STUDIUM_SIGNING_KEY`` would trade something real for something
    marginal. Credentials take the opposite path -- see
    ``studium.eval.credentials.issue_credential``, where signing is the point
    and its absence is §16's error.
    """
    import hashlib

    from studium.eval.credentials import (
        SigningKeyUnavailable,
        build_manifest,
        envelope,
        load_identity,
    )

    learner_subject_id = uuid.UUID(payload["learner_subject_id"])
    user_id = uuid.UUID(payload["user_id"])
    body = payload["body"]

    tail = session.execute(
        sql(
            """
            SELECT id, chain_index, content_sha256
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

    session_id = _maybe_uuid(payload.get("session_id"))
    try:
        identity = load_identity()
    except SigningKeyUnavailable as exc:
        log.warning("portfolio item written unsigned: %s", exc)
        identity = None

    kind = payload.get("kind") or "prose"
    item = PortfolioItem(
        user_id=user_id,
        learner_subject_id=learner_subject_id,
        chain_index=next_index,
        concept_id=_maybe_uuid(payload.get("concept_id")),
        session_id=session_id,
        kind=kind,
        # Amendment v1.2.1 §3.1: what erasure reads to decide whether this row
        # outlives the learner. Derived from ``kind`` rather than accepted from
        # the payload -- an agent that could set it would be an agent that
        # could exempt a learner's essay from their own erasure request. The
        # CHECK constraint from migration 0013 rejects the insert if this
        # disagrees with ``kind``.
        is_credential=kind in CREDENTIAL_KINDS,
        title=payload.get("title") or "",
        body=body,
        language=payload.get("language"),
        content_sha256=digest,
        # Deliberately not set from ``tail``: ``parent_item_id`` is the
        # *revision* link ("a draft essay revised three times is four rows"),
        # not the chain link. Chain order is ``chain_index``. Pointing it at
        # the previous chain entry would make every item read as a revision of
        # an unrelated one.
        parent_item_id=_maybe_uuid(payload.get("parent_item_id")),
        signature=envelope(
            build_manifest(
                previous_sha256=prev_hash,
                content_sha256=digest,
                user_id=user_id,
                session_id=session_id,
            ),
            identity,
        ),
    )
    session.add(item)
    session.flush()  # assign the id the end chunk reports
    return item.id


def _flag_for_review(
    session: Session, payload: dict[str, Any], turn_id: uuid.UUID | None
) -> uuid.UUID | None:
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
        return None

    item = ContentReviewQueueItem(
        artifact_id=artifact_id,
        session_turn_id=linked_turn,
        source=payload.get("source") or "system_confidence",
        reason=payload["reason"],
        severity=int(payload.get("severity", 2)),
    )
    session.add(item)
    session.flush()
    return item.id


HANDLERS: dict[str, EffectHandler] = {
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

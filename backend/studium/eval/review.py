"""The content review surface (evaluation §10).

The reviewer's queue for what the *runtime generated*: thin grounding, grading
anomalies, structured-output parse failures, content filter trips (§10.1).
Distinct from ``studium.ingestion.queue``, which holds what *came in*. Same
reviewer, different queue, different actions (§10).

The three actions §10.2 names map onto ``review_status`` as follows, and the
mapping is the part worth being careful about:

* ``approve`` -> ``dismissed``. The item was flagged and turned out to be fine.
  Nothing was wrong, so nothing was fixed.
* ``reject`` -> ``resolved``. The item is a defect. The row is closed because
  the reviewer has acted on it, and the note records what they concluded.
* ``flag --to-dataset`` -> ``resolved``, plus a golden dataset stub.

``approve`` mapping to ``dismissed`` rather than ``resolved`` reads backwards
at first and is right: ``resolved`` means "there was a problem and it has been
dealt with", which is precisely what an approved item is not. Getting this the
other way round would make "how many real defects did the queue catch"
unanswerable, and that number is what §14.3 uses to decide whether the queue's
severity thresholds are tuned.

**The stub is not an authored entry.** §3 refuses generated datasets, so
``flag --to-dataset`` writes the *fixture input* that produced the defect and
leaves ``expected`` empty. ``datasets.Entry.is_stub`` detects that and the
runner refuses to score it -- otherwise the entry would grade whatever the
agent did against nothing and report a pass for the very defect it was created
from.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

#: §10.2's actions and the ``review_status`` each writes. See the module
#: docstring on why ``approve`` is a dismissal.
ACTION_STATUS = {
    "approve": "dismissed",
    "reject": "resolved",
    "flag": "resolved",
}


@dataclass(frozen=True, slots=True)
class QueueItem:
    """One ``content_review_queue`` row, flattened for the CLI."""

    id: uuid.UUID
    source: str
    reason: str
    severity: int
    status: str
    artifact_id: uuid.UUID | None = None
    session_turn_id: uuid.UUID | None = None
    resolution_note: str | None = None
    #: Joined from the artifact or the turn, so `review list --agent lecturer`
    #: can filter without a second query.
    agent: str | None = None
    concept_title: str | None = None
    trace_id: uuid.UUID | None = None

    @property
    def target(self) -> str:
        if self.artifact_id:
            return f"artifact:{self.artifact_id}"
        if self.session_turn_id:
            return f"turn:{self.session_turn_id}"
        return "none"  # pragma: no cover -- the has_target CHECK forbids it

    def render_line(self) -> str:
        agent = f"  [{self.agent}]" if self.agent else ""
        return f"[{self.severity}] {self.id}  {self.source:24}{agent} {self.target}"


#: The queue joined to whatever produced it. ``content_artifacts`` carries
#: ``generated_by`` (the agent) and a concept; ``session_turns`` carries the
#: actor. LEFT JOINs throughout because a row has one target or the other, and
#: an INNER JOIN on either would drop half the queue.
_SELECT = """
    SELECT q.id, q.source::text AS source, q.reason, q.severity,
           q.status::text AS status, q.artifact_id, q.session_turn_id,
           q.resolution_note,
           COALESCE(a.generated_by::text, t.actor::text) AS agent,
           c.title AS concept_title,
           t.id AS trace_id
      FROM content_review_queue q
      LEFT JOIN content_artifacts a ON a.id = q.artifact_id
      LEFT JOIN session_turns t     ON t.id = q.session_turn_id
      LEFT JOIN concepts c          ON c.id = COALESCE(a.concept_id, t.concept_id)
"""


def pending(
    session: Session,
    *,
    agent: str | None = None,
    severity: int | None = None,
    limit: int = 50,
) -> list[QueueItem]:
    """Pending items, most severe first (§10.2 ``studium review list``).

    Ordered to match ``idx_review_queue_pending`` so the reviewer's default
    view is served by the partial index without a sort.
    """
    rows = session.execute(
        sql(
            _SELECT
            + """
             WHERE q.status = 'pending'
               AND (:no_severity OR q.severity = :severity)
               AND (:no_agent OR COALESCE(a.generated_by::text, t.actor::text) = :agent)
             ORDER BY q.severity DESC, q.created_at
             LIMIT :limit
            """
        ),
        {
            "no_severity": severity is None,
            "severity": severity,
            "no_agent": agent is None,
            "agent": agent,
            "limit": limit,
        },
    ).all()
    return [_as_item(row) for row in rows]


def get(session: Session, item_id: uuid.UUID) -> QueueItem | None:
    row = session.execute(
        sql(_SELECT + " WHERE q.id = :id"), {"id": item_id}
    ).one_or_none()
    return _as_item(row) if row else None


def detail(session: Session, item_id: uuid.UUID) -> dict[str, Any] | None:
    """Everything ``studium review show`` prints, including the trace link.

    §5: "a reviewer can drill from a failed evaluation to the full trace
    (prompt, response, cost, latency)". The same is true from a queue item, and
    it is the difference between "the Lecturer under-grounded here" and being
    able to see which passages it was actually given.
    """
    item = get(session, item_id)
    if item is None:
        return None

    # Joined on session_turn_id, not on id. ``content_review_queue`` points at
    # a ``session_turns`` row; ``agent_traces`` is a separate table that
    # *references* one. ``traces.write`` returns a ``session_turns.id`` despite
    # the local name ``turn_id``, and treating that as a trace id silently
    # matches nothing -- the reviewer would see "no trace" on every item.
    #
    # Skipped entirely when the item has no turn. A queue row may target an
    # artifact instead (the ``has_target`` CHECK requires one or the other),
    # and passing NULL here made Postgres refuse the statement outright --
    # "could not determine data type of parameter" -- rather than returning no
    # rows. That took down every ``review show`` on an artifact-targeted item,
    # which is the common case for a thin-grounding flag.
    trace = None
    if item.session_turn_id is not None:
        trace = session.execute(
            sql(
                """
                SELECT tr.id, tr.agent::text AS agent, tr.model, tr.kind,
                       tr.system_prompt_hash, tr.cost_usd, tr.latency_ms,
                       tr.completion
                  FROM agent_traces tr
                 WHERE tr.session_turn_id = :turn_id
                 ORDER BY tr.created_at DESC
                 LIMIT 1
                """
            ),
            {"turn_id": item.session_turn_id},
        ).one_or_none()

    artifact = None
    if item.artifact_id:
        artifact = session.execute(
            sql(
                """
                SELECT id, kind::text AS kind, stance::text AS stance,
                       generated_by::text AS generated_by, model, body
                  FROM content_artifacts WHERE id = :id
                """
            ),
            {"id": item.artifact_id},
        ).one_or_none()

    return {
        "item": item,
        "trace": dict(trace._mapping) if trace else None,
        "artifact": dict(artifact._mapping) if artifact else None,
    }


def depth(session: Session) -> dict[str, int]:
    """Pending count per flag source (§14.3's queue-depth dashboard).

    §16's last row: when the reviewer is unavailable "items accumulate; the
    queue-depth dashboard reflects this". This is the number that dashboard
    reads, and the number subsystem 7's alert will threshold on.
    """
    rows = session.execute(
        sql(
            """
            SELECT source::text AS source, COUNT(*) AS n
              FROM content_review_queue WHERE status = 'pending'
             GROUP BY source ORDER BY source
            """
        )
    ).all()
    return {row.source: int(row.n) for row in rows}


def close(
    session: Session,
    item_id: uuid.UUID,
    *,
    action: str,
    note: str,
    actor_id: uuid.UUID | None = None,
) -> bool:
    """Apply an ``approve`` / ``reject`` decision (§10.2).

    The note is required. §10.2's commands all take one, and a closed queue row
    with no reason is indistinguishable a year later from one that was closed
    by accident. Writes an ``audit_log`` row alongside, because closing a
    review item is a privileged action taken on content a learner saw.
    """
    status = ACTION_STATUS.get(action)
    if status is None:
        raise ValueError(f"unknown action {action!r}; expected {sorted(ACTION_STATUS)}")
    if not note.strip():
        raise ValueError("a note is required; it is the audit record")

    before = get(session, item_id)
    if before is None:
        raise LookupError(f"no content review item {item_id}")
    if before.status != "pending":
        return False

    session.execute(
        sql(
            """
            UPDATE content_review_queue
               SET status = CAST(:status AS review_status),
                   resolution_note = :note,
                   resolved_at = NOW(),
                   assigned_to = COALESCE(assigned_to, :actor)
             WHERE id = :id AND status = 'pending'
            """
        ),
        {"id": item_id, "status": status, "note": note.strip(), "actor": actor_id},
    )
    _audit(
        session,
        actor_id=actor_id,
        action=f"content_review.{action}",
        target_id=item_id,
        before={"status": before.status},
        after={"status": status},
        reason=note.strip(),
    )
    return True


def escalate(session: Session, item_id: uuid.UUID, *, severity: int) -> bool:
    """Raise an item's severity. Only upward.

    Matching ``ingestion.queue.escalate``: lowering would let a triage pass
    quietly bury something, and dismissing with a note is the honest way to do
    that.
    """
    if not 1 <= severity <= 3:
        raise ValueError(f"severity must be 1-3, not {severity}")
    changed = session.execute(
        sql(
            """
            UPDATE content_review_queue SET severity = :severity
             WHERE id = :id AND severity < :severity
            """
        ),
        {"id": item_id, "severity": severity},
    )
    return bool(changed.rowcount)


# --- §10.2's flag --to-dataset ---------------------------------------------


@dataclass(frozen=True, slots=True)
class Stub:
    """A dataset entry captured from a production defect, awaiting authoring."""

    dataset_slug: str
    entry_key: str
    path: Path
    yaml: str


def flag_to_dataset(
    session: Session,
    item_id: uuid.UUID,
    *,
    dataset_slug: str,
    note: str,
    content_root: Path,
    actor_id: uuid.UUID | None = None,
) -> Stub:
    """§10.2: turn a real production defect into a golden dataset stub.

    "The reviewer sees a Lecturer segment that under-grounded; they flag it; a
    stub dataset entry gets created capturing the fixture input that produced
    the defect; the reviewer edits the stub to specify what the correct output
    should have been."

    Written to the YAML tree, not to the database. §4 makes the repo the source
    of truth, and a stub that appeared only in the database would be invisible
    to the diff the reviewer commits alongside the prompt fix -- and would be
    overwritten by the next ``studium eval sync``.

    ``expected`` is left empty and marked ``stub: true``. That is what makes it
    refuse to run: an entry whose expectation is blank scores whatever the
    agent produced against nothing, and would report a pass for the defect it
    was created from.
    """
    context = _defect_context(session, item_id)
    if context is None:
        raise LookupError(f"no content review item {item_id}")

    entry_key = f"defect_{str(item_id)[:8]}"
    directory = content_root / dataset_slug
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{entry_key}.yaml"

    document = _stub_yaml(
        dataset_slug=dataset_slug,
        entry_key=entry_key,
        item_id=item_id,
        context=context,
        note=note.strip(),
    )
    path.write_text(document, encoding="utf-8")

    close(session, item_id, action="flag", note=note, actor_id=actor_id)
    log.info("wrote dataset stub %s", path)
    return Stub(
        dataset_slug=dataset_slug, entry_key=entry_key, path=path, yaml=document
    )


def _defect_context(session: Session, item_id: uuid.UUID) -> dict[str, Any] | None:
    """Reconstruct the fixture input that produced a flagged output.

    Best effort by design. The passages a segment was grounded in are recorded
    on ``content_citations``, so a thin-grounding flag reconstructs exactly;
    a content-filter trip on a Tutor turn has no citations and reconstructs
    only the concept and the utterance. The stub says which it got rather than
    inventing the difference -- an author filling in a stub needs to know
    whether the input in front of them is the real one.
    """
    found = detail(session, item_id)
    if found is None:
        return None

    item: QueueItem = found["item"]
    artifact = found["artifact"] or {}

    passages = []
    if item.artifact_id:
        passages = [
            {
                # The chunk's own text, falling back to the quoted span. The
                # span is what the artifact cited; the chunk is what it was
                # given. An author filling in this stub needs the second --
                # a fixture built from quoted spans would supply the agent
                # only the parts it already chose to use.
                "id": str(row.source_chunk_id),
                "text": row.chunk_text or row.quoted_span or "",
                "section_path": list(row.section_path or []),
            }
            for row in session.execute(
                sql(
                    """
                    SELECT cc.source_chunk_id, cc.quoted_span,
                           sc.text AS chunk_text, sc.section_path
                      FROM content_citations cc
                      LEFT JOIN source_chunks sc ON sc.id = cc.source_chunk_id
                     WHERE cc.artifact_id = :id
                     ORDER BY sc.id
                    """
                ),
                {"id": item.artifact_id},
            ).all()
        ]

    return {
        "reason": item.reason,
        "severity": item.severity,
        "source": item.source,
        "agent": item.agent or artifact.get("generated_by"),
        "concept_title": item.concept_title,
        "stance": artifact.get("stance"),
        "actual_output": (artifact.get("body") or "")[:4000],
        "retrieved_passages": passages,
        "reconstructed": "full" if passages else "partial",
    }


def _stub_yaml(
    *,
    dataset_slug: str,
    entry_key: str,
    item_id: uuid.UUID,
    context: dict[str, Any],
    note: str,
) -> str:
    """Render the stub. Hand-written rather than ``yaml.dump``ed.

    ``yaml.dump`` produces a valid file with no comments, and the comments are
    the whole point: this file exists to be edited by a human who needs to know
    which parts are captured evidence and which parts they have to supply.
    """
    import yaml

    input_block = yaml.safe_dump(
        {
            "concept": {"title": context.get("concept_title") or "TODO"},
            "stance": context.get("stance") or "default",
            "retrieved_passages": context.get("retrieved_passages") or [],
        },
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    indented = "\n".join(f"      {line}" for line in input_block.rstrip().splitlines())

    return f"""# STUB -- not runnable until `expected:` is filled in.
#
# Captured from content_review_queue item {item_id}
#   flagged as: {context["source"]} (severity {context["severity"]})
#   reason:     {context["reason"]}
#   reviewer:   {note}
#
# Input reconstruction: {context["reconstructed"]}
{"#   Only the concept was recoverable; the passages this output was" if context["reconstructed"] == "partial" else "#   Passages recovered from content_citations; this is the real input."}
{"#   grounded in were not recorded. Fill them in from the trace." if context["reconstructed"] == "partial" else "#"}
#
# What the agent actually produced is at the bottom, commented out, so you can
# see the defect while writing what it should have done instead. Evaluation §3
# is categorical that datasets are authored, not generated: do not paste the
# actual output into `expected`.

dataset:
  slug: {dataset_slug}
  kind: agent_output
  agent: {context.get("agent") or "TODO"}
  version: 1
  description: >
    TODO: what evaluation question does this dataset answer?

entries:
  - id: {entry_key}
    input:
{indented}

    expected:
      stub: true
      # TODO: replace the line above with the properties this entry asserts.
      # properties:
      #   - name: cites_provided_passages
      #     check: at_least_n_citations
      #     n: 2

    notes: >
      Captured from production defect {item_id}. {note}

# --- what the agent actually produced -------------------------------------
{_comment_block(context.get("actual_output") or "(no artifact body recorded)")}
"""


def _comment_block(text: str) -> str:
    return "\n".join(f"# {line}" for line in text.splitlines() or [""])


# --- §10.4 escalation to the ingestion queue -------------------------------


def escalate_upstream(
    session: Session,
    item_id: uuid.UUID,
    *,
    reason: str,
    concept_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """§10.4: a systemic issue whose fix is upstream, not downstream.

    "If every Lecturer segment on a specific concept is thin grounding,
    suggesting concept_sources authoring is missing, the reviewer flags the
    underlying issue rather than just the individual item. This creates an
    ingestion review queue entry rather than staying in the content review
    queue."

    Writes to ``ingestion_review_queue`` through ingestion's own ``flag``, so
    the provenance invariant that module enforces (ingestion §13) applies to
    rows this subsystem creates as much as to rows ingestion creates itself.
    """
    from studium.ingestion.queue import flag

    if concept_id is None and subject_id is None:
        raise ValueError(
            "an upstream escalation needs a concept or a subject: the fix is to "
            "the authoring for some specific thing, and a queue row with no "
            "target cannot be actioned"
        )

    queue_id = flag(
        session,
        flag_source="concept_source_conflict",
        reason=f"escalated from content review {item_id}: {reason}",
        concept_id=concept_id,
        subject_id=subject_id,
        severity=3,
        payload={"content_review_queue_id": str(item_id)},
    )
    close(
        session,
        item_id,
        action="reject",
        note=f"escalated upstream to ingestion review {queue_id} (§10.4): {reason}",
        actor_id=actor_id,
    )
    return queue_id


# --- helpers ---------------------------------------------------------------


def _as_item(row: Any) -> QueueItem:
    return QueueItem(
        id=row.id,
        source=row.source,
        reason=row.reason,
        severity=int(row.severity),
        status=row.status,
        artifact_id=row.artifact_id,
        session_turn_id=row.session_turn_id,
        resolution_note=row.resolution_note,
        agent=row.agent,
        concept_title=row.concept_title,
        trace_id=getattr(row, "trace_id", None),
    )


def _audit(
    session: Session,
    *,
    actor_id: uuid.UUID | None,
    action: str,
    target_id: uuid.UUID,
    before: dict[str, Any],
    after: dict[str, Any],
    reason: str,
) -> None:
    session.execute(
        sql(
            """
            INSERT INTO audit_log
                (actor_user_id, action, target_type, target_id, before, after, reason)
            VALUES
                (:actor, :action, 'content_review_queue', :target,
                 CAST(:before AS jsonb), CAST(:after AS jsonb), :reason)
            """
        ),
        {
            "actor": actor_id,
            "action": action,
            "target": target_id,
            "before": json.dumps(before),
            "after": json.dumps(after),
            "reason": reason,
        },
    )


def sources(items: Sequence[QueueItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.source] = counts.get(item.source, 0) + 1
    return counts

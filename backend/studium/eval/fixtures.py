"""Seeded fixture contexts for evaluation runs (evaluation §3).

§3's last principle: "Evaluation runs pin the prompt (by hash), the model (by
identifier), and the runtime context (by seeded fixture), so results are
comparable across runs."

The first two are columns on ``evaluation_runs``. The third is this module. An
agent under test needs a :class:`~studium.session.context.SessionContext` --
that is the interface every agent has -- but a regression run has no learner,
no session and no live database. What it has is a dataset entry's ``input``
block, and this turns one into the other.

**Every id here is derived, not random.** ``uuid5`` over the dataset slug and
entry key, so re-running the same entry produces the same session id, the same
learner id and therefore the same prompt bytes. A ``uuid4`` would change the
prefix on every run, defeating the prompt cache and -- much worse -- making two
runs of the same entry incomparable at the byte level, which is precisely what
§3 says pinning the context is for.

**The context's ids are synthetic; the session behind them must not be.**

Every billable call writes an ``agent_traces`` row, and ``traces._write_sync``
writes a ``session_turns`` row first -- which takes ``next_turn_index``, which
does ``SELECT id FROM learning_sessions WHERE id = :session_id FOR UPDATE`` and
requires exactly one row. A purely synthetic ``session_id`` therefore makes
every real agent call raise ``NoResultFound`` after the model has already been
paid for.

Nothing below Tier 3 sees this: Tier 1 uses fake agents that write no traces,
and Tier 2 persists hand-built reports. It surfaced on the first run against a
real agent, which is what that tier is for.

:func:`ensure_eval_session` is the answer, and skipping the trace instead would
have been the wrong one -- §15.1 attributes evaluation cost *through* traces and
§5's ``agent_trace_id`` links each result back to one, so traces are
load-bearing here rather than incidental. One real ``learning_sessions`` row
per run, under the system account, also makes "which calls belonged to this
run" a join rather than a guess.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.session.context import Passage, SessionContext

log = logging.getLogger(__name__)

#: Namespace for every id an evaluation fixture mints. Fixed so the derivation
#: is reproducible across machines and releases.
EVAL_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://studium.app/evaluation")

#: The fixture session's start time. Frozen rather than ``now()`` because
#: ``SessionContext.minutes_remaining`` reads it, the Curator's pacing reads
#: that, and a wall clock in a fixture makes the Curator's prompt different in
#: the afternoon than in the morning.
FIXTURE_STARTED_AT = dt.datetime(2026, 1, 1, 9, 0, tzinfo=dt.UTC)


#: The subject every evaluation session is enrolled in. A real row, deliberately
#: named so it is obvious in any query that it is not a subject anyone learns.
#: It is never published (``status`` stays ``draft``), so it cannot appear on a
#: learner's desk.
EVAL_SUBJECT_SLUG = "evaluation-harness"


def derived_id(*parts: str) -> uuid.UUID:
    return uuid.uuid5(EVAL_NAMESPACE, "/".join(parts))


def ensure_eval_session(
    session: Session,
    *,
    dataset_slug: str,
    triggered_by: uuid.UUID | None = None,
) -> uuid.UUID:
    """Create (or reuse) the real ``learning_sessions`` row a run's traces need.

    Returns the session id to put on every :class:`SessionContext` in the run.

    **Why a real row rather than a synthetic id.** ``traces._write_sync`` writes
    a ``session_turns`` row per billable call, and ``next_turn_index`` locks the
    parent ``learning_sessions`` row and requires it to exist. Without one,
    every agent call raises after the model has been paid for.

    **Why one per run rather than one per entry.** The turns of a run belong
    together: §15.1 attributes the run's whole cost to one actor, and the
    reviewer drilling from a failed result (§5) wants the neighbouring calls,
    not an island. It also keeps ``learning_sessions`` growing by one row per
    run rather than by twenty.

    **Owned by the system account, not the reviewer.** ``triggered_by`` records
    who asked, and it lands on ``evaluation_runs.triggered_by`` where §15.1
    puts it. The *session* belongs to the system user so an evaluation run
    never appears in a real person's session history, their desk, or their
    mastery -- a reviewer's "recent sessions" filling up with harness runs
    would be a reporting bug that looks like a data bug.

    Idempotent per (dataset, day): re-running a dataset on the same day reuses
    the session, so a day of gate runs is one session with many turns rather
    than a session per attempt.
    """
    from studium.models.identity import SYSTEM_USER_ID

    subject_id = session.execute(
        sql(
            """
            INSERT INTO subjects (slug, title, short_description, long_description)
            VALUES (:slug, 'Evaluation harness',
                    'Owns evaluation-run sessions.',
                    'Synthetic subject owning the learning_sessions rows that '
                    'evaluation runs write their traces against. Never '
                    'published, so it cannot reach a learner''s desk.')
            ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug
            RETURNING id
            """
        ),
        {"slug": EVAL_SUBJECT_SLUG},
    ).scalar_one()

    enrollment_id = session.execute(
        sql(
            """
            INSERT INTO learner_subjects (user_id, subject_id, subject_version)
            VALUES (:user_id, :subject_id, 1)
            ON CONFLICT (user_id, subject_id)
              DO UPDATE SET subject_version = learner_subjects.subject_version
            RETURNING id
            """
        ),
        {"user_id": SYSTEM_USER_ID, "subject_id": subject_id},
    ).scalar_one()

    # Deterministic per (dataset, day) so the reuse above is a lookup rather
    # than a scan, and so a crashed run's session is picked up by the retry
    # instead of orphaned.
    session_id = derived_id(
        dataset_slug, dt.datetime.now(dt.UTC).date().isoformat(), "eval-session"
    )
    session.execute(
        sql(
            """
            INSERT INTO learning_sessions
                (id, user_id, learner_subject_id, mode, target_duration_minutes)
            VALUES (:id, :user_id, :lsid, 'tutorial', 0)
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {"id": session_id, "user_id": SYSTEM_USER_ID, "lsid": enrollment_id},
    )
    if triggered_by is not None:
        log.debug("evaluation session %s triggered by %s", session_id, triggered_by)
    return session_id


def ensure_eval_concept(
    session: Session, *, dataset_slug: str, entry_key: str, title: str
) -> uuid.UUID:
    """The real ``concepts`` row an entry's turns reference.

    ``session_turns.concept_id`` is a foreign key and every agent's
    :meth:`~studium.agents.base.Agent.call_spec` copies
    ``ctx.focus_concept_id`` onto the trace. A derived id therefore fails the
    constraint on the first real call -- the same failure as the missing
    session, one table along, and found the same way.

    **One row per entry, not one per dataset.** The concept id is part of every
    agent prefix's cache key (``prompts._key(concept["id"], ...)``), so sharing
    one row across entries would give two entries with different concepts the
    same cache key while their prefix text differed. Rows are cheap; a cache
    key that means two things is not.

    The row is a *pointer*, not the content: title and descriptions still come
    from the YAML into the context dict, because that is what the author wrote
    and what the prompt should read. This exists so the foreign keys resolve.

    Idempotent per (subject, slug), so re-running a dataset reuses its rows.
    """
    subject_id = session.execute(
        sql("SELECT id FROM subjects WHERE slug = :slug"),
        {"slug": EVAL_SUBJECT_SLUG},
    ).scalar_one()

    # The data layer CHECKs slug format: lowercase alphanumerics and hyphens,
    # 2-81 characters. Dataset slugs and entry keys use underscores, so they
    # are translated rather than passed through -- an IntegrityError naming a
    # regex is a poor way to learn that.
    slug = f"{dataset_slug}-{entry_key}".replace("_", "-").lower()[:81]

    return session.execute(
        sql(
            """
            INSERT INTO concepts
                (subject_id, slug, title, short_description, long_description, depth)
            VALUES (:subject_id, :slug, :title, :short, :long, 1)
            ON CONFLICT (subject_id, slug) DO UPDATE SET title = EXCLUDED.title
            RETURNING id
            """
        ),
        {
            "subject_id": subject_id,
            "slug": slug,
            "title": title[:200],
            "short": f"Evaluation fixture for {dataset_slug}/{entry_key}.",
            "long": (
                f"Placeholder concept owning the session_turns rows written "
                f"while evaluating {dataset_slug}/{entry_key}. The concept the "
                f"entry actually describes is in its YAML."
            ),
        },
    ).scalar_one()


def build_context(
    entry_input: Mapping[str, Any],
    *,
    dataset_slug: str,
    entry_key: str,
    mode: str = "tutorial",
    session_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    concept_id: uuid.UUID | None = None,
) -> SessionContext:
    """Assemble the context an agent under test receives.

    ``entry_input`` is the dataset entry's ``input:`` block. Recognised keys
    follow §7.2's and §8's dataset shapes: ``concept``, ``stance``,
    ``retrieved_passages``, ``mastery_snapshot``, ``recent_turns``,
    ``prior_session_summary``, ``open_journal_entries``, ``concepts_seen``,
    ``subject``. Anything else is left for the agent's own payload -- the
    context carries the world, the payload carries the request.

    ``session_id`` and ``user_id`` should be the real rows from
    :func:`ensure_eval_session` whenever the agents are real. They default to
    derived values so a fake-agent run needs no database at all -- but a real
    agent writes a trace, and a trace needs a session that exists. See the
    module docstring.
    """
    session_id = session_id or derived_id(dataset_slug, entry_key, "session")
    user_id = user_id or derived_id(dataset_slug, entry_key, "user")
    learner_subject_id = derived_id(dataset_slug, entry_key, "enrollment")

    subject = _subject(entry_input, dataset_slug)
    concept = _concept(
        entry_input, dataset_slug, entry_key, subject["id"], concept_id=concept_id
    )

    mastery = {
        str(k): float(v)
        for k, v in (entry_input.get("mastery_snapshot") or {}).items()
    }
    if concept and str(concept["id"]) not in mastery:
        # An agent that reads focus_mastery gets 0.0 by default, which is the
        # honest reading of "this entry did not say" -- a fresh learner.
        mastery[str(concept["id"])] = float(entry_input.get("focus_mastery", 0.0))

    return SessionContext(
        session={
            "id": session_id,
            "mode": mode,
            "started_at": FIXTURE_STARTED_AT,
            "target_duration_minutes": int(
                entry_input.get("target_duration_minutes", 90)
            ),
            "learner_subject_id": learner_subject_id,
        },
        learner={
            "id": user_id,
            "display_name": "Evaluation fixture",
            "role": "learner",
        },
        learner_subject={
            "id": learner_subject_id,
            "user_id": user_id,
            "subject_id": subject["id"],
        },
        subject=subject,
        focus_concept=concept,
        focus_neighborhood=list(entry_input.get("focus_neighborhood") or []),
        subject_concepts=list(entry_input.get("subject_concepts") or []),
        recent_turns=list(entry_input.get("recent_turns") or []),
        mastery_snapshot=mastery,
        open_journal_entries=list(entry_input.get("open_journal_entries") or []),
        prior_session_summary=entry_input.get("prior_session_summary"),
        passages=_passages(entry_input, dataset_slug, entry_key),
        concepts_seen=list(entry_input.get("concepts_seen") or []),
        # Fixed, not derived from the passages: the real value is a hash of
        # subject, concept and chunk timestamps, none of which a fixture has.
        # A constant keeps the prefix byte-stable, which is the property that
        # matters here.
        grounding_version=f"eval:{dataset_slug}",
        exchange_index=int(entry_input.get("exchange_index", 1)),
    )


def _subject(entry_input: Mapping[str, Any], dataset_slug: str) -> dict[str, Any]:
    raw = entry_input.get("subject")
    if isinstance(raw, Mapping):
        subject = dict(raw)
    else:
        subject = {"slug": str(raw or "evaluation-fixture"), "title": str(raw or "Evaluation fixture")}
    subject.setdefault("id", derived_id(dataset_slug, "subject", str(subject["slug"])))
    subject.setdefault("title", str(subject["slug"]).replace("-", " ").title())
    subject.setdefault("assessment_threshold", 0.75)
    return subject


def _concept(
    entry_input: Mapping[str, Any],
    dataset_slug: str,
    entry_key: str,
    subject_id: Any,
    *,
    concept_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    raw = entry_input.get("concept")
    if raw is None:
        # The Orchestrator's intent classifier has no focus concept (§8.7:
        # "Utterance + state -> intent"), so a context without one is valid
        # rather than an error.
        return None
    if not isinstance(raw, Mapping):
        raw = {"title": str(raw)}

    concept = dict(raw)
    # The real row's id when the run is live, so session_turns.concept_id
    # resolves; a derived one otherwise.
    concept["id"] = concept_id or concept.get("id") or derived_id(
        dataset_slug, entry_key, "concept"
    )
    concept.setdefault("subject_id", subject_id)
    title = str(concept.get("title", "Concept"))
    concept.setdefault("slug", title.lower().replace(" ", "-"))
    concept.setdefault("short_description", concept.get("long_description", title))
    concept.setdefault("long_description", concept.get("short_description", title))
    concept.setdefault("depth", 2)
    concept.setdefault("load_bearing", False)
    concept.setdefault("metadata", {})
    return concept


def _passages(
    entry_input: Mapping[str, Any], dataset_slug: str, entry_key: str
) -> list[Passage]:
    """§7.2's ``retrieved_passages``, in the citation order already decided.

    **List order is authoritative and is not re-sorted here.**
    ``datasets.order_passages`` fixed it at parse time, and
    ``checks._passage_ids`` reads the same list the same way. Sorting again on
    the derived ``chunk_id`` would reorder against that -- uuid5 output is
    unrelated to the slug it came from, so ``[P1]`` in the agent's context
    would point at one chunk while the citation check resolved it to another,
    and the check would pass anyway because only the counts are compared.
    """
    raw = entry_input.get("retrieved_passages") or []
    passages: list[Passage] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        chunk_id = item.get("id") or item.get("chunk_id")
        passages.append(
            Passage(
                # Authored ids are slugs ("chunk_beta_001"), not UUIDs. Derived
                # so the fixture matches the real shape while staying stable;
                # the authored slug survives on the checks' side because
                # ``checks._passage_ids`` reads the YAML, not this.
                chunk_id=_chunk_uuid(chunk_id, dataset_slug, entry_key),
                text=str(item.get("text", "")),
                source_title=item.get("source_title"),
                section_path=list(item.get("section_path") or []),
                page_start=item.get("page_start"),
                page_end=item.get("page_end"),
                chunk_type=str(item.get("chunk_type", "body")),
                relevance_score=item.get("relevance_score"),
            )
        )
    return passages


class FixtureRetriever:
    """A :class:`~studium.retrieval.types.PassageRetriever` that returns the
    entry's own passages.

    §8.1's Lecturer dataset shape is "Concept + stance + retrieved passages ->
    segment": the passages are an *input* the author controls, chosen to
    include the adversarial cases §8.1 asks for (thin retrieval, misleading
    passages). But ``Lecturer.handle_streaming`` always calls
    ``self.retriever.retrieve_passages``, so without this the run would ground
    against whatever is in the live corpus and measure retrieval quality and
    corpus coverage instead of the prompt -- while reporting the number as a
    Lecturer grounding score.

    §3 names the fix without naming this class: "Evaluation runs pin the
    prompt (by hash), the model (by identifier), and the runtime context (by
    seeded fixture)". The retriever is part of the runtime context, so it is
    pinned too.

    Returns the passages verbatim, in the order
    ``datasets.order_passages`` fixed. It does not re-sort, re-rank, or apply
    ``k`` -- the author already decided what this entry supplies, and silently
    truncating to ``k`` would make a deliberately-thin entry look well-grounded
    or vice versa.
    """

    def __init__(self, passages: list[Passage]) -> None:
        self.passages = passages
        self.calls: list[tuple[uuid.UUID, str, int]] = []

    async def retrieve_passages(
        self,
        concept_id: uuid.UUID,
        *,
        stance: str = "default",
        k: int = 6,
        query_text: str | None = None,
        session_turn_id: uuid.UUID | None = None,
    ) -> Any:
        from studium.retrieval.types import RetrievalResult, detect_thin_grounding

        self.calls.append((concept_id, stance, k))
        thin, reason = detect_thin_grounding(self.passages)
        return RetrievalResult(
            passages=list(self.passages),
            query_used=query_text or "",
            thin_grounding=thin,
            # No review_queue_id: a fixture's thin grounding is the author's
            # choice, and writing a content_review_queue row for it would fill
            # the reviewer's queue with their own test cases.
            thin_grounding_reason=reason,
        )


def _chunk_uuid(raw: Any, dataset_slug: str, entry_key: str) -> uuid.UUID:
    if isinstance(raw, uuid.UUID):
        return raw
    text = str(raw or "")
    try:
        return uuid.UUID(text)
    except ValueError:
        return derived_id(dataset_slug, entry_key, "chunk", text)

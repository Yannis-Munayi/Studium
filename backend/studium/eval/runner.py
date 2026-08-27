"""Executing an evaluation run (evaluation §4, §6.2, §16).

One run = one dataset, one pinned prompt hash, one model. The run creates an
``evaluation_runs`` row, walks the entries, invokes whatever is under test,
grades, writes one ``evaluation_results`` row per entry, and completes.

**Entry failures do not end the run** (§16 rows 1 and 2). An agent call that
fails past its retries marks that entry failed with a specific note and the run
continues -- because a run that aborted on entry 3 of 20 has spent money and
produced no baseline, which is the worst of both. Only an *invalid entry*
(§16 row 3) fails the run outright, and that is caught by
``studium eval validate`` before a run starts.

**Persistence is optional.** ``persist=False`` runs everything and writes
nothing, which is what the offline tier and ``--dry-run`` use. The run's
correctness does not depend on a database being reachable, so a Postgres
outage cannot silently change what a gate decides.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.agents.base import AgentInput
from studium.eval import metrics as metrics_mod
from studium.eval import retrieval_eval
from studium.eval.datasets import Dataset, Entry
from studium.eval.fixtures import FixtureRetriever, build_context
from studium.eval.grading import EntryGrade, Grader, aggregate_score, property_scores
from studium.eval.regression import EntryOutcome, GateResult, RunOutcome, should_block

log = logging.getLogger(__name__)

#: Which invocation kind an ``agent_output`` dataset calls when its entries do
#: not name one. Each is the agent's principal method -- the one §8's "dataset
#: shape" line describes.
DEFAULT_KIND: dict[str, str] = {
    "lecturer": "deliver_segment",
    "tutor": "answer",
    "evaluator": "grade_practice",
    "curator": "next_topic",
    "confusion_tracker": "evaluate_turn",
    "reviewer": "generate_prompt",
    "orchestrator": "classify_intent",
}

#: Kinds that stream rather than return (agent runtime §20). The runner
#: consumes the stream and concatenates the text, which is what a learner would
#: have read and therefore what §8.1's checks measure.
STREAMING_KINDS = frozenset(
    {"deliver_segment", "re_explain", "worked_example", "answer", "open_tutorial",
     "interruption_response", "office_hours_response", "remediation"}
)

#: The session mode each agent's fixture context runs in. Matters because
#: prefix builders read ``ctx.mode`` and a Curator asked to open a session in
#: "tutorial" mode gets a different prompt than one in "lecture".
FIXTURE_MODE: dict[str, str] = {
    "lecturer": "lecture",
    "tutor": "tutorial",
    "evaluator": "lab",
    "curator": "orientation",
    "confusion_tracker": "tutorial",
    "reviewer": "review",
    "orchestrator": "tutorial",
}


class RunAborted(RuntimeError):
    """§16 row 3: the run could not start or could not continue at all."""


@dataclass(frozen=True, slots=True)
class EntryResult:
    """One entry's full outcome, ready to persist."""

    entry: Entry
    grade: EntryGrade
    actual_output: dict[str, Any]
    latency_ms: int
    cost_usd: float = 0.0
    trace_id: uuid.UUID | None = None
    #: Populated for ``retrieval_quality`` datasets only.
    retrieval: retrieval_eval.EntryMetrics | None = None


@dataclass
class RunReport:
    """What a run produced. Rendered by the CLI, persisted by :func:`persist`."""

    dataset: Dataset
    prompt_hash: str
    model: str
    results: list[EntryResult] = field(default_factory=list)
    run_id: uuid.UUID | None = None
    started_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    completed_at: dt.datetime | None = None

    @property
    def grades(self) -> list[EntryGrade]:
        return [r.grade for r in self.results]

    @property
    def aggregate_score(self) -> float:
        return aggregate_score(self.grades)

    @property
    def entries_passed(self) -> int:
        return sum(1 for g in self.grades if g.passed)

    @property
    def entries_failed(self) -> int:
        return len(self.grades) - self.entries_passed

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd + r.grade.cost_usd for r in self.results)

    def outcome(self) -> RunOutcome:
        return RunOutcome(
            dataset_slug=self.dataset.slug,
            aggregate_score=self.aggregate_score,
            entries=tuple(
                EntryOutcome(key=g.entry_key, passed=g.passed, score=g.score)
                for g in self.grades
            ),
        )

    def metric_values(self) -> list[metrics_mod.MetricValue]:
        if not self.dataset.agent:
            return []
        return metrics_mod.compute(self.dataset.agent, property_scores(self.grades))

    def retrieval_aggregate(self) -> retrieval_eval.Aggregate | None:
        entries = [r.retrieval for r in self.results if r.retrieval is not None]
        return retrieval_eval.aggregate(entries) if entries else None

    def threshold_violations(self) -> list[str]:
        return metrics_mod.violations(self.metric_values())

    def render(self, gate: GateResult | None = None) -> str:
        lines = [
            f"{self.dataset.slug}  ({self.dataset.kind})",
            f"  prompt {self.prompt_hash[:12]}  model {self.model}",
            f"  {self.entries_passed}/{len(self.results)} entries passed, "
            f"aggregate {self.aggregate_score:.4f}, cost ${self.cost_usd:.4f}",
        ]
        for result in self.results:
            mark = "pass" if result.grade.passed else "FAIL"
            lines.append(f"  [{mark}] {result.entry.key}: {result.grade.notes}")

        retrieval = self.retrieval_aggregate()
        if retrieval is not None:
            lines.append("")
            lines.append(retrieval.render())

        values = self.metric_values()
        if values:
            lines.append("")
            lines.append("  §8 metrics:")
            lines.extend(f"  {v.render()}" for v in values)

        violations = self.threshold_violations()
        if violations:
            lines.append("")
            lines.extend(f"  BLOCKING: {v}" for v in violations)

        if gate is not None:
            lines.append("")
            lines.append("  §13.2 regression gate:")
            lines.append(gate.render())
        return "\n".join(lines)


async def run_dataset(
    dataset: Dataset,
    *,
    agent: Any = None,
    retriever: Any = None,
    grader: Grader | None = None,
    model: str = "",
    prompt_hash: str = "",
    rerank: bool = True,
    session_id: uuid.UUID | None = None,
    live: bool = True,
) -> RunReport:
    """Execute one dataset. Never raises for an entry's failure (§16).

    ``agent`` is the agent under test for ``agent_output`` and
    ``grading_calibration`` datasets; ``retriever`` is the retriever under test
    for ``retrieval_quality``. Injected rather than constructed here so a Tier
    1 run can pass fakes and a Tier 3 run can pass the real thing, with no
    branch between them that only one of the two exercises.

    ``live=True`` (the default) provisions the real ``learning_sessions`` row
    that a real agent's trace write needs -- see
    :func:`studium.eval.fixtures.ensure_eval_session`. Set it False for a run
    against fakes with no database reachable, which is what Tier 1 does; a real
    agent then fails on the first call rather than silently writing no trace,
    which is the right way round because a billable call with no trace is
    unaccounted cost (agent runtime §19).
    """
    grader = grader or Grader()
    report = RunReport(dataset=dataset, prompt_hash=prompt_hash, model=model)

    if live and session_id is None:
        session_id = await _provision_session(dataset)

    for entry in dataset.entries:
        started = time.monotonic()
        try:
            if dataset.kind == "retrieval_quality":
                result = await _run_retrieval_entry(
                    entry, dataset=dataset, retriever=retriever, rerank=rerank
                )
            else:
                result = await _run_agent_entry(
                    entry,
                    dataset=dataset,
                    agent=agent,
                    grader=grader,
                    session_id=session_id,
                )
        except Exception as exc:  # noqa: BLE001 -- §16 row 1
            log.exception("entry %s failed", entry.key)
            result = EntryResult(
                entry=entry,
                grade=EntryGrade(
                    entry.key, error=f"agent call failed after retries: {exc}"
                ),
                actual_output={"error": str(exc)},
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        report.results.append(result)

    report.completed_at = dt.datetime.now(dt.UTC)
    if not report.prompt_hash:
        report.prompt_hash = _resolve_prompt_hash(report)
    if not report.model:
        report.model = _resolve_model(dataset, rerank=rerank)
    return report


async def _provision_session(dataset: Dataset) -> uuid.UUID | None:
    """The real session row a live run's traces need.

    Failure is logged and swallowed rather than raised: a ``--no-persist``
    run against fakes on a machine with no database is a legitimate thing to
    do, and the agents that need the session are the ones that will fail
    loudly without it. Raising here would make the harness require Postgres to
    run the tier that exists so it does not have to.
    """
    from studium.asyncdb import run_db
    from studium.eval.fixtures import ensure_eval_session

    try:
        return await run_db(
            lambda s: ensure_eval_session(s, dataset_slug=dataset.slug)
        )
    except Exception:
        log.warning(
            "could not provision an evaluation session for %s; a run against "
            "real agents will fail on its first trace write",
            dataset.slug,
            exc_info=True,
        )
        return None


async def _provision_concept(dataset: Dataset, entry: Entry) -> uuid.UUID | None:
    """The real concept row this entry's turns reference. See
    :func:`studium.eval.fixtures.ensure_eval_concept`."""
    from studium.asyncdb import run_db
    from studium.eval.fixtures import ensure_eval_concept

    raw = entry.input.get("concept")
    title = raw.get("title", entry.key) if isinstance(raw, Mapping) else str(raw)

    try:
        return await run_db(
            lambda s: ensure_eval_concept(
                s,
                dataset_slug=dataset.slug,
                entry_key=entry.key,
                title=str(title),
            )
        )
    except Exception:
        log.warning(
            "could not provision an evaluation concept for %s/%s",
            dataset.slug,
            entry.key,
            exc_info=True,
        )
        return None


async def _run_agent_entry(
    entry: Entry,
    *,
    dataset: Dataset,
    agent: Any,
    grader: Grader,
    session_id: uuid.UUID | None = None,
) -> EntryResult:
    """Invoke the agent under test and grade what it produced."""
    if entry.is_stub:
        return EntryResult(
            entry=entry,
            grade=await grader.grade_entry(entry, output=""),
            actual_output={"skipped": "stub entry"},
            latency_ms=0,
        )
    if agent is None:
        raise RunAborted(
            f"{dataset.slug} is an {dataset.kind} dataset and needs an agent "
            f"under test; none was supplied"
        )

    agent_name = dataset.agent or ""
    kind = str(entry.input.get("kind") or DEFAULT_KIND.get(agent_name, ""))
    if not kind:
        raise RunAborted(f"no invocation kind for agent {agent_name!r}")

    concept_id = None
    if session_id is not None and entry.input.get("concept"):
        concept_id = await _provision_concept(dataset, entry)

    context = build_context(
        entry.input,
        dataset_slug=dataset.slug,
        entry_key=entry.key,
        mode=FIXTURE_MODE.get(agent_name, "tutorial"),
        # The real session and concept, so the trace write finds rows to lock
        # and reference. Both fall back to derived ids when the run is not
        # live, which is what keeps Tier 1 database-free.
        session_id=session_id,
        user_id=_system_user() if session_id else None,
        concept_id=concept_id,
    )
    # The agent's own retriever is replaced by the entry's passages. See
    # FixtureRetriever: without this a Lecturer run grounds against the live
    # corpus and reports the result as a prompt score.
    if hasattr(agent, "retriever"):
        agent.retriever = FixtureRetriever(list(context.passages))

    payload = dict(entry.input.get("payload") or {})
    payload.setdefault("stance", entry.input.get("stance", "default"))
    for key in ("answer", "question", "expected_key_points", "rubric", "responses",
                "utterance", "attempt", "card", "prior_prompt", "turns"):
        if key in entry.input:
            payload[key] = entry.input[key]

    agent_input = AgentInput(session_context=context, kind=kind, payload=payload)

    started = time.monotonic()
    if kind in STREAMING_KINDS and hasattr(agent, "handle_streaming"):
        text, trace_id, cost = await _consume_stream(agent, agent_input)
        structured: Any = None
    else:
        output = await agent.handle(agent_input)
        text = output.text
        structured = output.structured
        trace_id = output.turn_id
        cost = float(getattr(output.trace, "cost_usd", 0.0) or 0.0)
    latency_ms = int((time.monotonic() - started) * 1000)

    graded_text = _grading_target(text, structured)
    grade = await grader.grade_entry(
        entry,
        output=graded_text,
        context=context,
        agent_under_test=agent_name,
    )

    return EntryResult(
        entry=entry,
        grade=grade,
        actual_output={
            "kind": kind,
            "text": text,
            "structured": _dump(structured),
            # What the checks actually read. Recorded so a reviewer looking at
            # a failed deterministic check can see the exact string it scored,
            # rather than re-deriving which of text/structured was used.
            "graded_text": graded_text,
        },
        latency_ms=latency_ms,
        cost_usd=cost,
        trace_id=trace_id,
    )


async def _consume_stream(agent: Any, agent_input: AgentInput) -> tuple[str, Any, float]:
    """Concatenate a streaming agent's text. Effects are read, not applied.

    An evaluation run must not write ``content_artifacts``, move mastery, or
    enqueue review items: it is not a learner's turn, and applying its effects
    would fill the corpus with fixture output and the reviewer's queue with the
    harness's own test cases. The effects are still *observed* -- the trace id
    comes off the ``end`` chunk -- which is what §5's ``agent_trace_id`` needs.

    **Cost is read back from the trace, not off a chunk.** The first version of
    this summed ``cost_usd`` from ``trace``-kind chunks, and streaming agents do
    not emit any: agent runtime §20's stream carries ``text``, ``tool_effect``,
    ``end`` and ``degraded``, and the cost is known only after the stream
    closes, when the client writes the trace. So every Lecturer and Tutor entry
    recorded $0 -- a full regression would have booked to
    ``evaluation_runs.cost_usd = 0`` and told §15.1's attribution that a
    fifteen-dollar run was free. Nothing below Tier 3 could see it, because a
    fake agent's cost is legitimately zero.

    The trace row is also the *authoritative* number rather than a second
    opinion: it is what the cost roll-up bills from.
    """
    parts: list[str] = []
    trace_id: Any = None
    async for chunk in agent.handle_streaming(agent_input):
        if chunk.kind in ("text", "degraded"):
            # §21's degraded path produces learner-visible copy, not a segment.
            # Kept so the grade reflects what actually happened rather than
            # scoring an apology as though it were a lecture.
            parts.append(str(chunk.payload.get("text", "")))
        elif chunk.kind == "end":
            trace_id = chunk.payload.get("turn_id") or trace_id
        elif chunk.kind == "trace":
            # Not emitted by any agent today. Honoured anyway so a future
            # streaming agent that does emit one is not silently free.
            trace_id = chunk.payload.get("session_turn_id") or trace_id

    turn_id = _maybe_uuid(trace_id)
    return "".join(parts), turn_id, await _trace_cost(turn_id)


async def _trace_cost(turn_id: uuid.UUID | None) -> float:
    """The cost the client recorded for this turn.

    Zero when there is no trace -- a fake agent, or a run with no database.
    That is the honest answer in both cases: no billable call was made, or none
    that we can account for. It is distinguishable from a real zero because a
    real call always writes a trace.
    """
    if turn_id is None:
        return 0.0
    from studium.asyncdb import read_db

    try:
        return float(
            await read_db(
                lambda s: s.execute(
                    sql(
                        "SELECT COALESCE(SUM(cost_usd), 0) FROM agent_traces"
                        " WHERE session_turn_id = :turn_id"
                    ),
                    {"turn_id": turn_id},
                ).scalar_one()
            )
        )
    except Exception:
        log.warning("could not read the trace cost for turn %s", turn_id, exc_info=True)
        return 0.0


async def _run_retrieval_entry(
    entry: Entry, *, dataset: Dataset, retriever: Any, rerank: bool
) -> EntryResult:
    """§9: retrieve, then score against the entry's labelled sets."""
    if retriever is None:
        raise RunAborted(
            f"{dataset.slug} is a retrieval_quality dataset and needs a "
            f"retriever under test; none was supplied"
        )
    if entry.is_stub:
        return EntryResult(
            entry=entry,
            grade=EntryGrade(entry.key, error="stub entry"),
            actual_output={"skipped": "stub entry"},
            latency_ms=0,
        )

    from studium.eval.checks import CheckOutcome
    from studium.eval.grading import PropertyGrade

    concept_id = _maybe_uuid(entry.input.get("concept_id")) or uuid.uuid5(
        uuid.NAMESPACE_URL, f"studium/eval/{entry.input.get('concept_id')}"
    )
    k = int(entry.input.get("k") or retrieval_eval.DEFAULT_K)

    started = time.monotonic()
    result = await retriever.retrieve_passages(
        concept_id,
        stance=str(entry.input.get("stance", "default")),
        k=k,
        query_text=entry.input.get("query_text"),
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    judgment = retrieval_eval.Judgment.from_expected(entry.expected)
    # Re-sorted by relevance: RetrievalResult.passages is in chunk_id order
    # (retrieval §12's citation contract), and scoring that order would grade
    # the reranker as though it had never run -- which is the one thing §9.3
    # exists to measure.
    ranked = retrieval_eval.ranked_ids(result.passages)
    measured = retrieval_eval.evaluate_entry(ranked, judgment, k=k)

    grade = EntryGrade(
        entry.key,
        properties=(
            PropertyGrade(
                name="retrieval_quality",
                check="retrieval_metrics",
                outcome=CheckOutcome(
                    # §9 states no per-entry pass line, so this one is stated
                    # rather than borrowed: an entry passes when it found at
                    # least one judged-relevant chunk and returned nothing
                    # judged misleading. Recall of zero means the query missed
                    # entirely; any misleading hit is the failure mode §9.2
                    # added the metric for. The graded numbers travel in the
                    # note either way, and the aggregate is what §13.2 gates.
                    passed=measured.recall > 0.0 and measured.misleading_rate == 0.0,
                    score=measured.score,
                    note=measured.render(),
                ),
            ),
        ),
    )

    return EntryResult(
        entry=entry,
        grade=grade,
        actual_output={
            "retrieved": ranked,
            "query_used": getattr(result, "query_used", ""),
            "thin_grounding": bool(getattr(result, "thin_grounding", False)),
            "degraded": bool(getattr(result, "degraded", False)),
            "rerank": rerank,
            "metrics": {
                "recall": measured.recall,
                "precision": measured.precision,
                "misleading_rate": measured.misleading_rate,
                "curated_preference": measured.curated_preference,
            },
        },
        latency_ms=latency_ms,
        retrieval=measured,
    )


# --- persistence -----------------------------------------------------------


def persist(
    session: Session,
    report: RunReport,
    *,
    trigger_kind: str = "manual",
    triggered_by: uuid.UUID | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> uuid.UUID:
    """Write the run and its results (§5 additions 3 and 4).

    One transaction. A half-written run whose ``entries_run`` disagreed with
    its result rows would fail the table's own
    ``entry_counts_agree`` CHECK, and the caller commits.
    """
    dataset_id = session.execute(
        sql("SELECT id FROM golden_datasets WHERE slug = :slug"),
        {"slug": report.dataset.slug},
    ).scalar_one_or_none()
    if dataset_id is None:
        raise RunAborted(
            f"dataset {report.dataset.slug!r} is not in the database; "
            f"run `studium eval sync` first"
        )

    entry_ids = dict(
        session.execute(
            sql(
                """
                SELECT entry_key, id FROM golden_dataset_entries
                 WHERE dataset_id = :dataset_id
                """
            ),
            {"dataset_id": dataset_id},
        ).all()
    )

    run_id = session.execute(
        sql(
            """
            INSERT INTO evaluation_runs
                (dataset_id, prompt_hash, model, triggered_by, trigger_kind,
                 status, aggregate_score, entries_run, entries_passed,
                 entries_failed, cost_usd, started_at, completed_at, metadata)
            VALUES
                (:dataset_id, :prompt_hash, :model, :triggered_by, :trigger_kind,
                 'complete', :aggregate, :run, :passed, :failed, :cost,
                 :started_at, :completed_at, CAST(:metadata AS jsonb))
            RETURNING id
            """
        ),
        {
            "dataset_id": dataset_id,
            "prompt_hash": report.prompt_hash,
            "model": report.model,
            "triggered_by": triggered_by,
            "trigger_kind": trigger_kind,
            "aggregate": report.aggregate_score,
            "run": len(report.results),
            "passed": report.entries_passed,
            "failed": report.entries_failed,
            "cost": report.cost_usd,
            "started_at": report.started_at,
            "completed_at": report.completed_at or dt.datetime.now(dt.UTC),
            "metadata": json.dumps(dict(metadata or {})),
        },
    ).scalar_one()

    for result in report.results:
        entry_id = entry_ids.get(result.entry.key)
        if entry_id is None:
            # The dataset in the database is behind the one on disk. Skipping
            # loses a result; the alternative is failing the whole run after
            # paying for it. Logged loudly, and `studium eval sync` is the fix.
            log.error(
                "entry %s is not synced for dataset %s; result not persisted",
                result.entry.key,
                report.dataset.slug,
            )
            continue
        session.execute(
            sql(
                """
                INSERT INTO evaluation_results
                    (run_id, entry_id, actual_output, score, passed,
                     grading_notes, cost_usd, latency_ms, agent_trace_id)
                VALUES
                    (:run_id, :entry_id, CAST(:actual AS jsonb), :score, :passed,
                     :notes, :cost, :latency, :trace_id)
                """
            ),
            {
                "run_id": run_id,
                "entry_id": entry_id,
                "actual": json.dumps(result.actual_output, default=str),
                "score": result.grade.score,
                "passed": result.grade.passed,
                "notes": result.grade.notes[:8000],
                "cost": result.cost_usd + result.grade.cost_usd,
                "latency": result.latency_ms,
                # Only a trace that exists: the fixture ids are synthetic, and
                # a foreign key to a trace that was never written would take
                # the whole run's persistence down.
                "trace_id": _existing_trace(session, result.trace_id),
            },
        )

    report.run_id = run_id
    return run_id


def baseline(session: Session, dataset_slug: str) -> RunOutcome | None:
    """The last complete run for a dataset (§13.1 step 4).

    Reads ``idx_eval_runs_baseline``. Returns ``None`` when there is none,
    which :func:`studium.eval.regression.should_block` treats as "cannot
    regress".
    """
    run = session.execute(
        sql(
            """
            SELECT r.id, r.aggregate_score
              FROM evaluation_runs r
              JOIN golden_datasets d ON d.id = r.dataset_id
             WHERE d.slug = :slug AND r.status = 'complete'
             ORDER BY r.completed_at DESC
             LIMIT 1
            """
        ),
        {"slug": dataset_slug},
    ).one_or_none()
    if run is None:
        return None

    rows = session.execute(
        sql(
            """
            SELECT e.entry_key, res.passed, res.score
              FROM evaluation_results res
              JOIN golden_dataset_entries e ON e.id = res.entry_id
             WHERE res.run_id = :run_id
            """
        ),
        {"run_id": run.id},
    ).all()
    return RunOutcome(
        dataset_slug=dataset_slug,
        aggregate_score=float(run.aggregate_score or 0.0),
        entries=tuple(
            EntryOutcome(key=r.entry_key, passed=bool(r.passed), score=float(r.score))
            for r in rows
        ),
    )


def gate(session: Session | None, report: RunReport) -> GateResult:
    """§13.2's gate for this run against its own dataset's last passing run."""
    previous = baseline(session, report.dataset.slug) if session is not None else None
    return should_block(report.outcome(), previous, report.dataset.regression_tolerance)


# --- helpers ---------------------------------------------------------------


def _grading_target(text: str, structured: Any) -> str:
    """What the checks score.

    Prose wins when there is prose: for a Lecturer segment the text *is* the
    product, and its JSON envelope is not what a learner reads. For a
    structured-only agent (the Orchestrator's classifier, the Curator's
    ``next_topic``) the canonical JSON is the only output there is, and it is
    what ``structured_output_matches_schema`` and the ``contains_*`` checks
    need to see.
    """
    if text.strip():
        return text
    return _canonical(structured) if structured is not None else ""


def _canonical(structured: Any) -> str:
    return json.dumps(_dump(structured), sort_keys=True, separators=(",", ":"), default=str)


def _dump(structured: Any) -> Any:
    if structured is None:
        return None
    if hasattr(structured, "model_dump"):
        return json.loads(structured.model_dump_json())
    return structured


def _system_user() -> uuid.UUID:
    """The account a live run's session and traces belong to.

    ``agent_traces.user_id`` is a foreign key, so a derived id fails the
    constraint after the call has been paid for -- the same failure the session
    row fixes, one table along.
    """
    from studium.models.identity import SYSTEM_USER_ID

    return SYSTEM_USER_ID


def budget_preflight(session: Session, *, estimated_usd: float) -> None:
    """Refuse a run that would cross the harness account's spend cap.

    **Closes SPEC_DEBT SD9, and the resolution is smaller than the entry
    expected because half of it was already true.**

    SD9 reported that "a $5-15 regression run draws on the reviewer's $8 daily
    hard cap", so a scheduled run would lock the reviewer out of their own
    learning sessions. That was read from evaluation §15.3, which reverted the
    ``daily_evaluation_usd_max`` column. Measured against the code, the spend
    never went there: :func:`_system_user` books every trace of a live run to
    the system account, which migration 0008 seeded with $1,000 daily and
    $20,000 monthly caps. SD9's option 2 -- "attribute scheduled and CI runs to
    the system user rather than to the triggering human" -- was already the
    implementation, arrived at while fixing E14 rather than as an answer to
    this question.

    **What was genuinely missing is enforcement.** Those caps were a number
    nobody read. ``budget_gate.pre_flight_check`` is called only from the
    Orchestrator's session path, and an evaluation run invokes agents directly,
    so no cap of any kind constrained a run. A human typing yes to an estimate
    was the only limit -- which §13.3's CI regression job removes, because it
    spends with nobody present. That is why the trigger SD9 names is exactly
    the thing subsystem 7 wired.

    So: same account, and now a gate in front of it. Raises
    :class:`BudgetExceededError` before anything is spent.
    """
    from studium.cost import budget_status
    from studium.session.budget_gate import BudgetExceededError

    user_id = _system_user()
    try:
        status = budget_status(session, user_id)
    except LookupError:
        # No caps row for the system account. Refusing rather than proceeding:
        # migration 0008 seeds one, so its absence means a database this run
        # should not be spending against in the first place.
        raise BudgetExceededError(
            scope="evaluation-harness",
            limit_usd=0.0,
            spent_usd=0.0,
            reset_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=1),
        ) from None

    projected = status.today_usd + estimated_usd
    if projected <= status.daily_hard_usd:
        return

    tomorrow = dt.datetime.combine(
        dt.date.today() + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC
    )
    raise BudgetExceededError(
        scope="evaluation-harness daily",
        limit_usd=status.daily_hard_usd,
        spent_usd=status.today_usd,
        reset_at=tomorrow,
    )


def _maybe_uuid(value: Any) -> uuid.UUID | None:
    if value in (None, "", "None"):
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _existing_trace(session: Session, turn_id: uuid.UUID | None) -> uuid.UUID | None:
    """Resolve a ``session_turns.id`` to the ``agent_traces.id`` behind it.

    ``AgentOutput.turn_id`` and ``traces.write`` both name this "turn_id" and
    both return a ``session_turns.id``; ``evaluation_results.agent_trace_id``
    references ``agent_traces.id``, which is a different table with different
    ids. Storing the turn id directly would violate the foreign key -- or, if
    a trace happened to share the value, point at an unrelated call.

    Returns ``None`` when nothing matches rather than raising: a run whose
    agent was a fake wrote no trace at all, and §5 makes the column nullable
    for exactly that reason.
    """
    if turn_id is None:
        return None
    return session.execute(
        sql(
            """
            SELECT id FROM agent_traces
             WHERE session_turn_id = :turn_id
             ORDER BY created_at DESC LIMIT 1
            """
        ),
        {"turn_id": turn_id},
    ).scalar_one_or_none()


def _resolve_prompt_hash(report: RunReport) -> str:
    """The prompt pinned by this run (§3, §5).

    For an agent dataset the hash comes off the calls that were made. For a
    retrieval dataset there is no prompt, and the column is ``NOT NULL`` with a
    64-character CHECK -- so the pinned thing is the retrieval configuration
    instead, hashed the same way. That is honest: §3's point is that a result
    is meaningless unless what produced it is pinned, and for retrieval what
    produces it is the provider and the rerank setting.
    """
    descriptor = json.dumps(
        {
            "dataset": report.dataset.slug,
            "kind": report.dataset.kind,
            "version": report.dataset.version,
            "rerank": any(
                r.actual_output.get("rerank") for r in report.results if r.actual_output
            ),
        },
        sort_keys=True,
    )
    return hashlib.sha256(descriptor.encode("utf-8")).hexdigest()


def _resolve_model(dataset: Dataset, *, rerank: bool) -> str:
    if dataset.kind == "retrieval_quality":
        return f"retrieval:rerank={'on' if rerank else 'off'}"
    from studium.llm.client import route

    agent = dataset.agent or "lecturer"
    return route(agent, DEFAULT_KIND.get(agent))


async def compare_reranker(
    dataset: Dataset, *, retriever_with: Any, retriever_without: Any
) -> retrieval_eval.ABResult:
    """§9.3's A/B, run over one retrieval dataset.

    Two retrievers rather than one with a flag: the rerank setting lives on the
    retriever's construction (``HybridRetriever(reranker=...)``), and mutating
    a shared instance between arms would leave the result cache holding arm A's
    answers when arm B ran.
    """
    with_rerank = await run_dataset(dataset, retriever=retriever_with, rerank=True)
    without_rerank = await run_dataset(dataset, retriever=retriever_without, rerank=False)

    def measured(report: RunReport) -> list[retrieval_eval.EntryMetrics]:
        return [r.retrieval for r in report.results if r.retrieval is not None]

    return retrieval_eval.compare_reranker(measured(with_rerank), measured(without_rerank))


def entry_keys(entries: Sequence[Entry]) -> list[str]:
    return [e.key for e in entries]

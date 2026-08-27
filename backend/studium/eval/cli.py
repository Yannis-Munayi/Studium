"""The reviewer's evaluation command line (evaluation §7.3, §10.2, §13.1).

    studium eval validate <dir>                     # §7.3 step 3
    studium eval sync <dir> [--yes]                 # §7.3 step 5
    studium eval list [--agent <name>] [--inactive]
    studium eval affected --changed <path>...       # §13.4
    studium eval run --datasets <slug>... [--dry-run] [--no-persist]
    studium eval gate --datasets <slug>...          # §13.1 steps 4-7
    studium eval retire <slug> --note "..."         # §7.3
    studium eval ab --dataset <slug>                # §9.3 reranker A/B
    studium eval keys generate | publish | list     # §12.3

    studium review list [--agent <name>] [--severity N]
    studium review show <queue_id>
    studium review approve <queue_id> --note "..."
    studium review reject  <queue_id> --note "..."
    studium review flag <queue_id> --to-dataset <slug> --note "..."
    studium review escalate <queue_id> --severity N
    studium review upstream <queue_id> --concept <id> --note "..."   # §10.4

CLI-primary for MVP (§4, §10.3), for the reason ingestion's is: the reviewer is
already in a terminal and an editor authoring YAML, and a web UI that required
leaving that loop to click through a queue would be slower than the thing it
replaced. §10.3 defers the web surface until queue volume or a second reviewer
makes it worth building.

argparse rather than click, matching ``studium.ingestion.cli`` -- a second CLI
is not a reason to take a dependency the first one did without.

**Every command that spends money says so first.** ``eval run`` prints the
entry count and an estimate and refuses a non-interactive stdin without
``--yes``. §7.4 puts a full regression at $5-15, which is not a number to
discover afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from studium.db import SessionLocal

from . import credentials, regression, review, runner, sync
from .datasets import Dataset, DatasetError, parse_directory
from .metrics import coverage_gaps

#: §4: "YAML files under content/evaluation/".
DEFAULT_CONTENT_ROOT = Path("content/evaluation")

#: §7.4's cost note, per entry, for the estimate `eval run` prints. Derived
#: from "$5-15 per run" across "~150-250 entries", rounded to the pessimistic
#: end. An estimate, and labelled as one -- the real number lands on
#: ``evaluation_runs.cost_usd`` afterwards.
ESTIMATED_COST_PER_ENTRY = 0.06


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 2
    try:
        return int(args.handler(args) or 0)
    except DatasetError as exc:
        # The expected failure: YAML an author has to fix. Printed as the list
        # it is; a stack trace here says nothing about which line is wrong.
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    except (LookupError, ValueError, PermissionError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="studium", description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    _add_eval(subparsers)
    _add_review(subparsers)
    return parser


def _add_eval(subparsers: Any) -> None:
    parser = subparsers.add_parser("eval", help="golden datasets and regression runs")
    kinds = parser.add_subparsers(dest="kind")

    validate = kinds.add_parser("validate", help="§7.3 step 3: parse and check")
    validate.add_argument("directory", type=Path, nargs="?", default=DEFAULT_CONTENT_ROOT)
    validate.set_defaults(handler=_validate)

    materialize = kinds.add_parser("sync", help="§7.3 step 5: YAML -> database")
    materialize.add_argument("directory", type=Path, nargs="?", default=DEFAULT_CONTENT_ROOT)
    materialize.add_argument("--yes", action="store_true", help="skip the confirmation")
    materialize.set_defaults(handler=_sync)

    listing = kinds.add_parser("list", help="datasets in the database")
    listing.add_argument("--agent", default=None)
    listing.add_argument("--inactive", action="store_true", help="include retired")
    listing.set_defaults(handler=_list)

    affected = kinds.add_parser("affected", help="§13.4: which datasets a diff selects")
    affected.add_argument("--changed", nargs="+", required=True, metavar="PATH")
    affected.add_argument("--directory", type=Path, default=DEFAULT_CONTENT_ROOT)
    affected.set_defaults(handler=_affected)

    run = kinds.add_parser("run", help="execute datasets against the current prompts")
    run.add_argument("--datasets", nargs="+", required=True, metavar="SLUG")
    run.add_argument("--directory", type=Path, default=DEFAULT_CONTENT_ROOT)
    run.add_argument("--trigger", default="manual", choices=runner_trigger_kinds())
    run.add_argument("--dry-run", action="store_true", help="estimate cost, run nothing")
    run.add_argument("--no-persist", action="store_true", help="run, write nothing")
    run.add_argument("--yes", action="store_true")
    run.set_defaults(handler=_run)

    gate = kinds.add_parser("gate", help="§13.1: run and compare against the baseline")
    gate.add_argument("--datasets", nargs="+", required=True, metavar="SLUG")
    gate.add_argument("--directory", type=Path, default=DEFAULT_CONTENT_ROOT)
    gate.add_argument("--yes", action="store_true")
    gate.set_defaults(handler=_gate)

    retire = kinds.add_parser("retire", help="§7.3: deactivate, never delete")
    retire.add_argument("slug")
    retire.add_argument("--note", required=True, help="why the property no longer applies")
    retire.set_defaults(handler=_retire)

    ab = kinds.add_parser("ab", help="§9.3: reranker A/B on a retrieval dataset")
    ab.add_argument("--dataset", required=True)
    ab.add_argument("--directory", type=Path, default=DEFAULT_CONTENT_ROOT)
    ab.set_defaults(handler=_ab)

    keys = kinds.add_parser("keys", help="§12.3: issuer signing keys")
    key_kinds = keys.add_subparsers(dest="key_command")
    key_kinds.add_parser("generate").set_defaults(handler=_keys_generate)
    publish = key_kinds.add_parser("publish", help="publish the configured key's public half")
    publish.add_argument("--yes", action="store_true")
    publish.set_defaults(handler=_keys_publish)
    key_kinds.add_parser("list").set_defaults(handler=_keys_list)


def _add_review(subparsers: Any) -> None:
    parser = subparsers.add_parser("review", help="the content review queue (§10)")
    kinds = parser.add_subparsers(dest="kind")

    listing = kinds.add_parser("list")
    listing.add_argument("--agent", default=None, help="filter by originating agent")
    listing.add_argument("--severity", type=int, choices=(1, 2, 3), default=None)
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(handler=_review_list)

    show = kinds.add_parser("show")
    show.add_argument("queue_id", type=uuid.UUID)
    show.set_defaults(handler=_review_show)

    for name in ("approve", "reject"):
        action = kinds.add_parser(name)
        action.add_argument("queue_id", type=uuid.UUID)
        action.add_argument("--note", required=True)
        action.set_defaults(handler=_review_close, action=name)

    flag = kinds.add_parser("flag", help="§10.2: capture a defect as a dataset stub")
    flag.add_argument("queue_id", type=uuid.UUID)
    flag.add_argument("--to-dataset", required=True, dest="dataset")
    flag.add_argument("--note", required=True)
    flag.add_argument("--directory", type=Path, default=DEFAULT_CONTENT_ROOT)
    flag.set_defaults(handler=_review_flag)

    escalate = kinds.add_parser("escalate")
    escalate.add_argument("queue_id", type=uuid.UUID)
    escalate.add_argument("--severity", type=int, required=True, choices=(1, 2, 3))
    escalate.set_defaults(handler=_review_escalate)

    upstream = kinds.add_parser("upstream", help="§10.4: the fix is in the ingestion side")
    upstream.add_argument("queue_id", type=uuid.UUID)
    upstream.add_argument("--concept", type=uuid.UUID, default=None)
    upstream.add_argument("--subject", type=uuid.UUID, default=None)
    upstream.add_argument("--note", required=True)
    upstream.set_defaults(handler=_review_upstream)


def runner_trigger_kinds() -> tuple[str, ...]:
    from studium.models.evaluation import TRIGGER_KINDS

    return TRIGGER_KINDS


# --- eval handlers ---------------------------------------------------------


def _validate(args: argparse.Namespace) -> int:
    datasets = parse_directory(args.directory)

    total = sum(len(d.entries) for d in datasets)
    stubs = sum(d.stub_count for d in datasets)
    print(f"{len(datasets)} dataset(s), {total} entries ({stubs} stub)")

    warnings = 0
    for dataset in datasets:
        names = [
            str(p.get("name", ""))
            for entry in dataset.entries
            for p in entry.properties
        ]
        gaps = coverage_gaps(dataset.agent, names) if dataset.agent else []
        stub_keys = [e.key for e in dataset.entries if e.is_stub]

        print(f"\n  {dataset.slug}  ({dataset.kind}, {len(dataset.entries)} entries)")
        for gap in gaps:
            warnings += 1
            print(f"    ! {gap}")
        for key in stub_keys:
            warnings += 1
            print(f"    ! {key} is a stub; it will be skipped by every run")

    if warnings:
        # Warnings, not errors: a dataset is built up incrementally and
        # blocking `validate` on an unfilled stub would make `review flag
        # --to-dataset` unusable. The CI gate treats a blocking-metric gap as
        # a failure, which is where it needs to bite.
        print(f"\n{warnings} warning(s). Nothing blocks; see the CI gate for what does.")
    else:
        print("\nall datasets valid")
    return 0


def _sync(args: argparse.Namespace) -> int:
    datasets = parse_directory(args.directory)

    with SessionLocal() as session:
        result = sync.sync(session, datasets)
        print(result.render())
        if result.is_empty and not result.orphans:
            print("\nnothing to do")
            return 0
        if not _confirm(args.yes, "apply this sync?"):
            session.rollback()
            return 1
        session.commit()

    print(f"\nsynced {len(datasets)} dataset(s)")
    return 0


def _list(args: argparse.Namespace) -> int:
    from sqlalchemy import text as sql

    with SessionLocal() as session:
        rows = session.execute(
            sql(
                """
                SELECT slug, kind::text AS kind, agent::text AS agent, version,
                       active, entry_count
                  FROM golden_datasets
                 WHERE (:all OR active)
                   AND (:no_agent OR agent::text = :agent)
                 ORDER BY kind, slug
                """
            ),
            {
                "all": args.inactive,
                "no_agent": args.agent is None,
                "agent": args.agent,
            },
        ).all()

    if not rows:
        print("no datasets (run `studium eval sync`)")
        return 0
    for row in rows:
        mark = " " if row.active else "-"
        agent = row.agent or "-"
        print(
            f"{mark} {row.slug:44} {row.kind:20} {agent:18} "
            f"v{row.version}  {row.entry_count} entries"
        )
    return 0


def _affected(args: argparse.Namespace) -> int:
    datasets = parse_directory(args.directory)
    result = regression.affected_datasets(
        args.changed, [(d.slug, d.kind, d.agent) for d in datasets if d.active]
    )

    if result.slugs:
        print("affected datasets:")
        for slug in result.slugs:
            print(f"  {slug}   ({result.because[slug]})")
    else:
        print("no datasets affected")

    if result.unresolved:
        # Never silent. §13.4 makes the reviewer the fallback for complex
        # cases, and a path that selects nothing is exactly where a change
        # ships unevaluated because the tooling shrugged.
        print("\nunresolved -- decide these by hand and pass --datasets:")
        for path in result.unresolved:
            print(f"  {path}")
        return 1
    return 0


def _run(args: argparse.Namespace) -> int:
    datasets = _select(args)
    report_lines: list[str] = []
    blocked = False

    entries = sum(len(d.runnable_entries) for d in datasets)
    estimate = entries * ESTIMATED_COST_PER_ENTRY
    print(
        f"{len(datasets)} dataset(s), {entries} runnable entries, "
        f"estimated ~${estimate:.2f} (§7.4; the real cost lands on the run row)"
    )
    if args.dry_run:
        for dataset in datasets:
            print(f"  {dataset.slug}: {len(dataset.runnable_entries)} entries")
        return 0
    if not _budget_preflight(estimate):
        return 1
    if not _confirm(args.yes, "spend it?"):
        return 1

    agent, retriever, grader = _under_test()

    for dataset in datasets:
        report = asyncio.run(
            runner.run_dataset(
                dataset,
                agent=agent(dataset),
                retriever=retriever,
                grader=grader,
            )
        )
        violations = report.threshold_violations()
        blocked = blocked or bool(violations)
        report_lines.append(report.render())

        if not args.no_persist:
            with SessionLocal() as session:
                runner.persist(session, report, trigger_kind=args.trigger)
                session.commit()

    print("\n" + "\n\n".join(report_lines))
    return 1 if blocked else 0


def _gate(args: argparse.Namespace) -> int:
    """§13.1 steps 3-7: run, compare, and say whether the change may proceed."""
    datasets = _select(args)
    entries = sum(len(d.runnable_entries) for d in datasets)
    estimate = entries * ESTIMATED_COST_PER_ENTRY
    print(
        f"{len(datasets)} dataset(s), {entries} runnable entries, "
        f"estimated ~${estimate:.2f} (§7.4)"
    )
    # Before the confirmation, not after: this is the path
    # .github/workflows/prompt-regression.yml drives with --yes, so the
    # confirmation is not a gate there and the budget check is the only one.
    # See runner.budget_preflight and SPEC_DEBT (SD9).
    if not _budget_preflight(estimate):
        return 1
    if not _confirm(args.yes, f"run {len(datasets)} dataset(s) against the baseline?"):
        return 1

    agent, retriever, grader = _under_test()
    blocked = False

    for dataset in datasets:
        report = asyncio.run(
            runner.run_dataset(
                dataset, agent=agent(dataset), retriever=retriever, grader=grader
            )
        )
        with SessionLocal() as session:
            gate_result = runner.gate(session, report)
            runner.persist(session, report, trigger_kind="ci")
            session.commit()

        print(report.render(gate_result))
        # Both gates block, and they answer different questions: §8's
        # thresholds ask "is this good enough to ship at all", §13.2's
        # tolerance asks "did this change make it worse". A run can pass either
        # and fail the other.
        blocked = blocked or gate_result.blocked or bool(report.threshold_violations())

    if blocked:
        print(
            "\nBLOCKED. §13.1 step 7: a reviewer either approves the "
            "regression with a documented reason, requests changes to the "
            "prompt, or rejects the change."
        )
    return 1 if blocked else 0


def _retire(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        changed = sync.retire(session, args.slug, note=args.note)
        session.commit()
    if not changed:
        print(f"{args.slug} was already inactive (or does not exist)")
        return 1
    print(
        f"{args.slug} retired. Its results stay queryable (§7.3); author a "
        f"successor dataset if the property still needs covering."
    )
    return 0


def _ab(args: argparse.Namespace) -> int:
    """§9.3, answering retrieval §19 open question 3."""
    from studium.retrieval import HybridRetriever
    from studium.retrieval.providers import IdentityReranker

    datasets = {d.slug: d for d in parse_directory(args.directory)}
    dataset = datasets.get(args.dataset)
    if dataset is None:
        raise LookupError(f"no dataset {args.dataset!r}")
    if dataset.kind != "retrieval_quality":
        raise ValueError(
            f"{args.dataset} is a {dataset.kind} dataset; the A/B needs a "
            f"retrieval_quality one"
        )

    result = asyncio.run(
        runner.compare_reranker(
            dataset,
            retriever_with=_production_retriever(),
            # Separate instances, not one with a flag: the rerank setting lives
            # on construction, and a shared cache would serve arm A's answers
            # to arm B.
            retriever_without=HybridRetriever(reranker=IdentityReranker(), use_cache=False),
        )
    )
    print(result.render())
    return 0


def _keys_generate(args: argparse.Namespace) -> int:
    seed = credentials.generate_seed()
    print("A new Ed25519 seed. Put it in secret storage; it is not saved here.\n")
    print(f"  {credentials.SIGNING_KEY_ENV}={seed}")
    print(f"  {credentials.SIGNING_KEY_ID_ENV}=<a stable name, e.g. studium-2026>\n")
    print(
        "Then `studium eval keys publish` to record the public half, which is "
        "what verifiers read (§12.4). Rotation retires the incumbent and keeps "
        "it published so old credentials still verify (§12.3)."
    )
    return 0


def _keys_publish(args: argparse.Namespace) -> int:
    identity = credentials.load_identity()
    print(f"key_id     {identity.key_id}")
    print(f"public key {identity.public_key_b64()}")
    print(f"issuer     {identity.issuer}")
    if not _confirm(args.yes, "publish this key and retire the incumbent?"):
        return 1

    with SessionLocal() as session:
        credentials.register_public_key(session, identity)
        session.commit()
    print("published")
    return 0


def _keys_list(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        keys = credentials.published_keys(session)
    if not keys:
        print("no published keys; credentials cannot be issued or verified")
        return 1
    for key in keys:
        mark = "*" if key["current"] else " "
        retired = key["retired_at"].date() if key["retired_at"] else "-"
        print(f"{mark} {key['key_id']:24} {key['algorithm']:10} retired {retired}")
    print("\n* = current signer. Retired keys stay published (§12.3).")
    return 0


# --- review handlers -------------------------------------------------------


def _review_list(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        items = review.pending(
            session, agent=args.agent, severity=args.severity, limit=args.limit
        )
        counts = review.depth(session)

    if not items:
        print("queue is empty")
        return 0
    for item in items:
        print(item.render_line())
        print(f"      {item.reason}")
    print("\npending by source: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


def _review_show(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        found = review.detail(session, args.queue_id)
    if found is None:
        raise LookupError(f"no content review item {args.queue_id}")

    item = found["item"]
    print(f"{item.source}  severity {item.severity}  {item.status}")
    print(f"  target   {item.target}")
    print(f"  agent    {item.agent or '-'}")
    print(f"  concept  {item.concept_title or '-'}")
    print(f"  reason   {item.reason}")
    if item.resolution_note:
        print(f"  note     {item.resolution_note}")

    trace = found["trace"]
    if trace:
        print("\n  trace (§5: drill from the flag to the call)")
        print(f"    id       {trace['id']}")
        print(f"    {trace['agent']}.{trace['kind']} on {trace['model']}")
        print(f"    prompt   {trace['system_prompt_hash'][:12]}")
        print(f"    cost     ${trace['cost_usd']}   {trace['latency_ms']}ms")
    else:
        print("\n  no trace linked")

    artifact = found["artifact"]
    if artifact:
        print(f"\n  artifact {artifact['kind']} ({artifact['stance']})")
        print("  " + (artifact["body"] or "")[:600].replace("\n", "\n  "))
    return 0


def _review_close(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        changed = review.close(
            session, args.queue_id, action=args.action, note=args.note
        )
        session.commit()
    if not changed:
        print(f"{args.queue_id} was already closed")
        return 1
    status = review.ACTION_STATUS[args.action]
    print(f"{args.queue_id} {args.action}d -> {status}")
    return 0


def _review_flag(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        stub = review.flag_to_dataset(
            session,
            args.queue_id,
            dataset_slug=args.dataset,
            note=args.note,
            content_root=args.directory,
        )
        session.commit()

    print(f"wrote {stub.path}")
    print(
        "\nIt is a STUB and every run will skip it until `expected:` is "
        "filled in. §3 refuses generated datasets: what the agent produced is "
        "at the bottom of the file, commented out, so you can see the defect "
        "while writing what it should have done instead."
    )
    return 0


def _review_escalate(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        changed = review.escalate(session, args.queue_id, severity=args.severity)
        session.commit()
    if not changed:
        print(
            f"{args.queue_id} is already at severity {args.severity} or higher; "
            f"escalation only raises"
        )
        return 1
    print(f"{args.queue_id} escalated to severity {args.severity}")
    return 0


def _review_upstream(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        queue_id = review.escalate_upstream(
            session,
            args.queue_id,
            reason=args.note,
            concept_id=args.concept,
            subject_id=args.subject,
        )
        session.commit()
    print(f"created ingestion review item {queue_id} (§10.4: the fix is upstream)")
    print(f"work it with `studium queue show {queue_id}`")
    return 0


# --- helpers ---------------------------------------------------------------


def _select(args: argparse.Namespace) -> list[Dataset]:
    by_slug = {d.slug: d for d in parse_directory(args.directory)}
    missing = [s for s in args.datasets if s not in by_slug]
    if missing:
        raise LookupError(
            f"no dataset(s) {missing}; known: {', '.join(sorted(by_slug))}"
        )
    selected = [by_slug[s] for s in args.datasets]

    inactive = [d.slug for d in selected if not d.active]
    if inactive:
        raise ValueError(
            f"dataset(s) {inactive} are retired (§7.3). Their results stay "
            f"queryable, but running them would compare against a property "
            f"that no longer applies."
        )
    return selected


def _under_test() -> tuple[Any, Any, Any]:
    """Assemble the agents, retriever and grader a real run uses.

    Imported lazily so `studium eval validate` -- the command an author runs
    twenty times an afternoon -- does not construct an Anthropic client or
    require an API key to tell them a check name is misspelled.
    """
    from studium.agents.confusion_tracker import ConfusionTracker
    from studium.agents.curator import Curator
    from studium.agents.evaluator import Evaluator
    from studium.agents.lecturer import Lecturer
    from studium.agents.reviewer import Reviewer
    from studium.agents.tutor import Tutor
    from studium.llm.client import AnthropicClient

    from .grading import Grader, MetaGrader

    client = AnthropicClient()
    retriever = _production_retriever()

    def agent_for(dataset: Dataset) -> Any:
        builders = {
            "lecturer": lambda: Lecturer(client, retriever),
            "tutor": lambda: Tutor(client, retriever),
            "evaluator": lambda: Evaluator(client),
            "curator": lambda: Curator(client),
            "confusion_tracker": lambda: ConfusionTracker(client),
            "reviewer": lambda: Reviewer(client),
        }
        builder = builders.get(dataset.agent or "")
        return builder() if builder else None

    return agent_for, retriever, Grader(meta_grader=MetaGrader(Evaluator(client)))


def _production_retriever() -> Any:
    from studium.retrieval import default_retriever

    retriever = default_retriever()
    # The result cache would serve arm A's answers to arm B in an A/B, and
    # would make a re-run of the same dataset measure the cache rather than
    # the retriever.
    retriever.use_cache = False
    return retriever


def _budget_preflight(estimated_usd: float) -> bool:
    """Refuse a run that would cross the harness account's daily cap.

    Reports and returns False rather than raising, so the operator sees the
    number and the reset time instead of a traceback. The CI job reads the exit
    code, which is what makes this a gate rather than a warning -- see
    ``runner.budget_preflight`` for why SD9's collision needed enforcement
    rather than re-attribution.

    A database that is unreachable does **not** block the run. The check is a
    spending guard, not a correctness one, and a reviewer unable to run a gate
    because Postgres is down is a worse failure than a run that spent $8
    unmetered. Said out loud rather than left to a bare except.
    """
    from studium.session.budget_gate import BudgetExceededError

    try:
        with SessionLocal() as session:
            from . import runner as runner_mod

            runner_mod.budget_preflight(session, estimated_usd=estimated_usd)
    except BudgetExceededError as exc:
        print(
            f"\nBLOCKED by the evaluation harness budget cap.\n"
            f"  {exc}\n"
            f"  resets {exc.reset_at.isoformat()}\n\n"
            f"  Evaluation runs book to the system account (§15.1), not to "
            f"your own budget, so this does not affect your learning sessions. "
            f"Raise user_budget_caps for the system user if the cap is wrong "
            f"for the suite's real cost.",
            file=sys.stderr,
        )
        return False
    except Exception as exc:  # noqa: BLE001 -- see the docstring
        print(
            f"warning: could not check the harness budget ({type(exc).__name__}: "
            f"{exc}). Proceeding -- the check is a spending guard, not a "
            f"correctness one.",
            file=sys.stderr,
        )
    return True


def _confirm(skip: bool, question: str) -> bool:
    if skip:
        return True
    if not sys.stdin.isatty():
        print(
            f"{question} -- refusing to guess on a non-interactive stdin; "
            f"pass --yes",
            file=sys.stderr,
        )
        return False
    return input(f"\n{question} [y/N] ").strip().lower() in {"y", "yes"}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

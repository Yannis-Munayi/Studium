"""The operator's command line (infrastructure §9-§13).

    studium ops smoke-test [--base-url URL] [--full]        # §10.3
    studium ops verify-restore [--expect-head 0012]         # §9.2, §9.3
    studium ops check-alerts [--json]                       # §8
    studium ops cost-report [--start D] [--end D]           # §13.1
    studium ops secrets check [--environment production]    # §6
    studium ops secrets render [--write]                    # §6.2

    studium ops mint-signing-key                            # §11.1
    studium ops publish-signing-key [--yes]                 # §11.2 step 3
    studium ops rotate-signing-key [--compromised]          # §11.2 (prints)
    studium ops compromise-signing-key <id> --earliest T    # §11.4
    studium ops keys list

    studium ops nightly [--dry-run] [--yes]                 # §12.1, all stages
    studium ops retention run [--dry-run] [--yes]           # the retention stage
    studium ops retention log [--limit N]                   # §12.2
    studium ops set-retention-hold <table> <row_id> <why>   # §12.4
    studium ops release-retention-hold <hold_id>
    studium ops holds list [--all]

    studium ops erase-user <user_id> [--reason ...] [--yes] # §12.3

argparse and ``python -m studium.ops.cli``, matching ``studium.ingestion.cli``
and ``studium.eval.cli``. A third CLI is not a reason to take a dependency the
first two did without.

**Everything destructive confirms, and ``--yes`` is the only way past it.**
``erase-user`` is irreversible after the 30-day window; a retention pass
deletes rows that nothing restores short of §9.2. Both refuse a
non-interactive stdin without the flag, so a script cannot type yes by
omission.

**Exit codes are the interface.** ``check-alerts`` exits non-zero when
something is firing, ``verify-restore`` when the restore is unusable,
``smoke-test`` when a blocking check failed, ``secrets check`` when a required
variable is missing. §10.3's "Failure alerts" and §16's Tier 2 lines are both
"run it and read $?".
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = BACKEND_ROOT / ".env.example"


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 2
    try:
        return int(args.handler(args) or 0)
    except (LookupError, ValueError, PermissionError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="studium", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    ops = sub.add_parser("ops", help="deployment, backups, retention, cost")
    kinds = ops.add_subparsers(dest="kind")

    smoke = kinds.add_parser("smoke-test", help="§10.3: post-deploy verification")
    smoke.add_argument("--base-url", default=None, help="exercise the deployed /health")
    smoke.add_argument(
        "--full", action="store_true", help="add the billable session round trip"
    )
    smoke.set_defaults(handler=_smoke)

    restore = kinds.add_parser("verify-restore", help="§9.2: is this restore usable")
    restore.add_argument("--expect-head", default=None, metavar="REV")
    restore.set_defaults(handler=_verify_restore)

    alerts = kinds.add_parser("check-alerts", help="§8: evaluate the DB-backed conditions")
    alerts.add_argument("--json", action="store_true", dest="as_json")
    alerts.add_argument("--all", action="store_true", help="print non-firing too")
    alerts.set_defaults(handler=_check_alerts)

    cost = kinds.add_parser("cost-report", help="§13.1")
    cost.add_argument("--start", type=_date, default=None, help="default: month start")
    cost.add_argument("--end", type=_date, default=None, help="default: today")
    cost.set_defaults(handler=_cost_report)

    secrets = kinds.add_parser("secrets", help="§6: the secret registry")
    secret_kinds = secrets.add_subparsers(dest="secret_command")
    check = secret_kinds.add_parser("check", help="what is missing here")
    check.add_argument("--environment", default="local", help="production|local|ci")
    check.add_argument("--app", default="backend", help="backend|frontend")
    check.set_defaults(handler=_secrets_check)
    render = secret_kinds.add_parser("render", help="regenerate .env.example")
    render.add_argument("--write", action="store_true")
    render.set_defaults(handler=_secrets_render)
    secret_kinds.add_parser("rotate", help="§6.3's quarterly procedure").set_defaults(
        handler=_secrets_rotate
    )

    kinds.add_parser("mint-signing-key", help="§11.1").set_defaults(handler=_mint)
    publish = kinds.add_parser("publish-signing-key", help="§11.2 step 3")
    publish.add_argument("--yes", action="store_true")
    publish.set_defaults(handler=_publish)
    rotate = kinds.add_parser("rotate-signing-key", help="§11.2: prints the procedure")
    rotate.add_argument("--compromised", action="store_true", help="§11.4's urgency")
    rotate.set_defaults(handler=_rotate)
    compromise = kinds.add_parser("compromise-signing-key", help="§11.4 step 2")
    compromise.add_argument("key_id")
    compromise.add_argument(
        "--earliest",
        required=True,
        type=_timestamp,
        help="EARLIEST possible compromise, ISO-8601. Not when you noticed.",
    )
    compromise.add_argument("--reason", required=True)
    compromise.add_argument("--yes", action="store_true")
    compromise.set_defaults(handler=_compromise)

    keys = kinds.add_parser("keys", help="signing keys")
    key_kinds = keys.add_subparsers(dest="key_command")
    key_kinds.add_parser("list").set_defaults(handler=_keys_list)

    nightly = kinds.add_parser("nightly", help="§12.1: every scheduled job, now")
    nightly.add_argument("--dry-run", action="store_true")
    nightly.add_argument("--yes", action="store_true")
    nightly.set_defaults(handler=_nightly)

    retention = kinds.add_parser("retention", help="§12.1, §12.2")
    retention_kinds = retention.add_subparsers(dest="retention_command")
    run = retention_kinds.add_parser("run", help="the retention stage alone")
    run.add_argument("--dry-run", action="store_true", help="count, delete nothing")
    run.add_argument("--yes", action="store_true")
    run.set_defaults(handler=_retention_run)
    log = retention_kinds.add_parser("log", help="§12.2's audit trail")
    log.add_argument("--limit", type=int, default=40)
    log.set_defaults(handler=_retention_log)

    hold = kinds.add_parser("set-retention-hold", help="§12.4")
    hold.add_argument("table")
    hold.add_argument("row_id", type=uuid.UUID)
    hold.add_argument("reason")
    hold.set_defaults(handler=_set_hold)

    release = kinds.add_parser("release-retention-hold")
    release.add_argument("hold_id", type=uuid.UUID)
    release.add_argument("--reason", default="")
    release.set_defaults(handler=_release_hold)

    holds = kinds.add_parser("holds")
    hold_kinds = holds.add_subparsers(dest="hold_command")
    listing = hold_kinds.add_parser("list")
    listing.add_argument("--all", action="store_true", help="include released")
    listing.set_defaults(handler=_holds_list)

    erase = kinds.add_parser("erase-user", help="§12.3: right to erasure, immediate")
    erase.add_argument("user_id", type=uuid.UUID)
    erase.add_argument("--reason", default="")
    erase.add_argument("--yes", action="store_true")
    erase.set_defaults(handler=_erase_user)

    return parser


# --- handlers --------------------------------------------------------------


def _smoke(args: argparse.Namespace) -> int:
    from . import smoke

    result = smoke.run(base_url=args.base_url, full=args.full)
    print(result.render())
    return 0 if result.ok else 1


def _verify_restore(args: argparse.Namespace) -> int:
    from . import owner_session, restore

    with owner_session() as session:
        verification = restore.verify(session, expected_head=args.expect_head)
    print(verification.render())
    return 0 if verification.ok else 1


def _check_alerts(args: argparse.Namespace) -> int:
    from . import alerts, owner_session

    with owner_session() as session:
        results = alerts.evaluate(session)

    if args.as_json:
        print(
            json.dumps(
                [
                    {
                        "key": a.condition.key,
                        "firing": a.measurement.firing,
                        "value": a.measurement.value,
                        "detail": a.measurement.detail,
                        "threshold": a.condition.threshold,
                        "delivery": a.condition.delivery,
                        "response_within": a.condition.response_within,
                        "action": a.condition.action,
                        **a.measurement.context,
                    }
                    for a in results
                    if args.all or a.measurement.firing
                ],
                indent=2,
                default=str,
            )
        )
    else:
        print(f"§8 conditions with a probe ({len(results)} of {len(alerts.CONDITIONS)}):\n")
        for alert in results:
            if args.all or alert.measurement.firing:
                print(alert.render())
        for alert in alerts.firing(results):
            print(f"\n  {alert.condition.key}: {alert.condition.action}")
        externally = [c for c in alerts.CONDITIONS if c.externally_watched]
        print(
            f"\n{len(externally)} further condition(s) are watched outside this "
            f"process and cannot be checked here:"
        )
        for condition in externally:
            print(f"  {condition.key:28} {condition.configured_in}")

    return 1 if alerts.firing(results) else 0


def _cost_report(args: argparse.Namespace) -> int:
    from . import cost, owner_session

    today = dt.date.today()
    start = args.start or today.replace(day=1)
    end = args.end or today

    with owner_session() as session:
        report = cost.report(session, start=start, end=end)
    print(report.render())
    return 0


def _secrets_check(args: argparse.Namespace) -> int:
    from . import secrets

    findings = secrets.audit(environment=args.environment, app=args.app)
    print(f"{args.app} secrets and configuration, environment={args.environment}\n")
    for finding in findings:
        if finding.level != "ok":
            print(finding.render())

    errors = secrets.errors(findings)
    warnings = secrets.warnings(findings)
    ok = len(findings) - len(errors) - len(warnings)
    print(f"\n  {ok} present or not required, {len(warnings)} degrading, {len(errors)} missing")
    if errors:
        print(
            "\n  Missing means the deployment does not work. `flyctl secrets set "
            "NAME=value` for each, then redeploy."
        )
    if warnings:
        print(
            "\n  Degrading means a subsystem runs in a reduced mode on purpose. "
            "Read the consequence above and decide; none of them is an error here."
        )
    return 1 if errors else 0


def _secrets_render(args: argparse.Namespace) -> int:
    from . import secrets

    rendered = secrets.render_env_example()
    if not args.write:
        print(rendered)
        return 0
    ENV_EXAMPLE.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote {ENV_EXAMPLE}")
    return 0


def _secrets_rotate(args: argparse.Namespace) -> int:
    """§6.3's procedure. Printed rather than automated, deliberately.

    Steps 1 and 4 happen in a provider's web console and step 5 in GitHub's;
    a command that automated the two in the middle would leave an operator
    holding a half-rotated key with no record of which half.
    """
    from . import secrets

    print("Quarterly API key rotation (§6.3). ~10 minutes.\n")
    for index, name in enumerate(secrets.QUARTERLY_ROTATION, start=1):
        variable = secrets.BY_NAME[name]
        print(f"  {index}. {name}  -- {variable.purpose}")
    print(
        """
  For each, in this order (§6.3):

    1. Generate the new key in the provider console. Do not revoke the old
       one yet.
    2. flyctl secrets set NAME=<new>          (deploys with the new key)
    3. studium ops smoke-test --base-url https://studium.app
       Verify the provider check for that key passes before going further.
    4. Revoke the old key in the provider console.
    5. Update the GitHub Actions secret.
    6. Update .env.local on your workstation.

  Step 3 sits between setting and revoking on purpose: revoke first and the
  window between the two is an outage whose cause is a rotation nobody has
  finished writing down yet.

  §6.4: when a person's access is revoked, every secret they could reach is
  rotated within 48 hours -- all of the above plus STUDIUM_SIGNING_KEY
  (`studium ops rotate-signing-key`) and the database credentials.
"""
    )
    return 0


def _mint(args: argparse.Namespace) -> int:
    from studium.eval.credentials import SIGNING_KEY_ENV, SIGNING_KEY_ID_ENV

    from . import keys

    seed = keys.mint()
    print("A new Ed25519 seed. It is printed once and stored nowhere.\n")
    print(f"  {SIGNING_KEY_ENV}={seed}")
    print(f"  {SIGNING_KEY_ID_ENV}=<a stable name, e.g. studium-{dt.date.today():%Y}>\n")
    print(
        "Put it in Fly secrets AND in the password manager (§9.1: two "
        "independent copies). A lost signing key cannot be recovered from the "
        "running system, and every credential it signed becomes unverifiable.\n"
    )
    print(keys.rotation_plan())
    return 0


def _publish(args: argparse.Namespace) -> int:
    from studium.eval.credentials import load_identity

    from . import keys, owner_session

    identity = load_identity()
    print(f"key_id     {identity.key_id}")
    print(f"public key {identity.public_key_b64()}")
    print(f"issuer     {identity.issuer}")
    print("\nThis publishes the public half and retires the incumbent, in one "
          "transaction (§11.2 step 4).")
    if not _confirm(args.yes, "publish?"):
        return 1

    with owner_session() as session:
        keys.publish(session, identity)
        session.commit()
    print("published. `studium ops smoke-test` verifies the round trip.")
    return 0


def _rotate(args: argparse.Namespace) -> int:
    from . import keys

    print(keys.rotation_plan(compromised=args.compromised))
    return 0


def _compromise(args: argparse.Namespace) -> int:
    from . import keys, owner_session

    print(keys.rotation_plan(compromised=True))
    print(
        f"\nAbout to mark {args.key_id!r} compromised from "
        f"{args.earliest.isoformat()}.\n"
        f"That timestamp is published to verifiers as the start of the "
        f"scrutiny window (§11.4 step 3). Widening it later is possible; "
        f"narrowing it is not."
    )
    if not _confirm(args.yes, "mark it?"):
        return 1

    with owner_session() as session:
        changed = keys.mark_compromised(
            session,
            args.key_id,
            earliest_possible=args.earliest,
            reason=args.reason,
        )
        at_risk = keys.credentials_at_risk(session, args.key_id)
        session.commit()

    if not changed:
        print("already marked at or before that time; nothing changed.")
    else:
        print(f"{args.key_id} marked compromised.")

    print(f"\n{len(at_risk)} credential(s) were signed inside the window (§11.4 step 3):")
    for item in at_risk:
        print(f"  {item['id']}  {item['kind']:20} {item['created_at']}")
    if at_risk:
        print(
            "\nThese are what the published notice has to name. They still "
            "verify -- the mathematics did not change -- which is why the "
            "notice says 'additional scrutiny' rather than 'invalid'."
        )
    return 0


def _keys_list(args: argparse.Namespace) -> int:
    from . import keys, owner_session

    with owner_session() as session:
        records = keys.published(session)
    if not records:
        print("no published keys; credentials cannot be issued or verified")
        return 1
    for record in records:
        print(record.render())
    overdue = [r for r in records if r.overdue]
    if overdue:
        print(f"\n{len(overdue)} key(s) past §11.2's annual rotation. "
              f"`studium ops rotate-signing-key`.")
    return 0


def _nightly(args: argparse.Namespace) -> int:
    """The whole §12.1 pass, on demand. What the scheduler runs at 02:00 UTC."""
    from . import nightly, owner_session

    if not args.dry_run and not _confirm(
        args.yes,
        "This deletes rows past their retention window and hard-deletes "
        "accounts past the 30-day dispute window. Proceed?",
    ):
        return 1

    with owner_session() as session:
        result = nightly.run_nightly(session, dry_run=args.dry_run)
    print(result.render())
    return 1 if result.failures else 0


def _retention_run(args: argparse.Namespace) -> int:
    from . import owner_session, retention

    if not args.dry_run and not _confirm(
        args.yes,
        "This deletes rows past their retention window. Nothing short of a "
        "§9.2 restore brings them back. Proceed?",
    ):
        return 1

    with owner_session() as session:
        run = retention.run_retention(session, dry_run=args.dry_run)
    print(run.render())

    truncated = [r for r in run.results if r.truncated]
    if truncated:
        print(
            f"\n{len(truncated)} policy/policies stopped at the batch ceiling "
            f"and are INCOMPLETE. That usually means a genuine backlog; it can "
            f"also mean a predicate matching more than it should. Look before "
            f"re-running."
        )
    return 1 if run.failures else 0


def _retention_log(args: argparse.Namespace) -> int:
    from . import owner_session, retention

    with owner_session() as session:
        actions = retention.recent_actions(session, limit=args.limit)
    if not actions:
        print(
            "no retention actions recorded. Either the worker has never run "
            "(STUDIUM_RETENTION_WORKER is not 1) or this is a fresh database."
        )
        return 0
    for action in actions:
        run_id = (action["metadata"] or {}).get("run_id", "-")
        print(
            f"  {action['ran_at']:%Y-%m-%d %H:%M}  {action['table_name']:26} "
            f"{action['rows_deleted']:>7} rows  {action['duration_ms']:>6} ms  "
            f"run {str(run_id)[:8]}"
        )
    return 0


def _set_hold(args: argparse.Namespace) -> int:
    from . import owner_session, retention

    with owner_session() as session:
        try:
            hold_id = retention.place_hold(
                session, table=args.table, row_id=args.row_id, reason=args.reason
            )
        except retention.HoldRefused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        session.commit()
    print(f"hold {hold_id} placed on {args.table} {args.row_id}")
    print("It is honoured by the nightly worker and by `retention run`. It does "
          "NOT block §12.3's erasure, which is a learner's right rather than a "
          "retention window.")
    return 0


def _release_hold(args: argparse.Namespace) -> int:
    from . import owner_session, retention

    with owner_session() as session:
        released = retention.release_hold(session, args.hold_id, reason=args.reason)
        session.commit()
    if not released:
        print(f"{args.hold_id} is not a live hold")
        return 1
    print(f"{args.hold_id} released. The row stays as the record that it was held.")
    return 0


def _holds_list(args: argparse.Namespace) -> int:
    from . import owner_session, retention

    with owner_session() as session:
        holds = retention.list_holds(session, include_released=args.all)
    if not holds:
        print("no holds")
        return 0
    for hold in holds:
        state = f"released {hold['released_at']:%Y-%m-%d}" if hold["released_at"] else "live"
        print(f"  {hold['id']}  {hold['table_name']:24} {hold['row_id']}  {state}")
        print(f"      {hold['reason']}")
    return 0


def _erase_user(args: argparse.Namespace) -> int:
    """§12.3. Runs immediately, not on the nightly schedule."""
    from sqlalchemy import text as sql

    from studium.privacy import DISPUTE_WINDOW_DAYS, erase_user

    from . import owner_session

    with owner_session() as session:
        row = session.execute(
            sql("SELECT email, deleted_at FROM users WHERE id = :id"),
            {"id": args.user_id},
        ).one_or_none()
        if row is None:
            raise LookupError(f"no user {args.user_id}")
        if row.deleted_at is not None:
            print(f"{row.email} was already erased at {row.deleted_at}")
            return 1

        print(f"About to erase {row.email} ({args.user_id}).\n")
        print(
            "  Anonymises assessment attempts, responses, cost ledger rows and\n"
            "  the audit log; deletes the enrollment and everything cascading\n"
            "  from it -- sessions, turns, traces, journal, review cards,\n"
            "  portfolio items -- plus profile and auth sessions.\n"
            f"  The account row itself is hard-deleted after "
            f"{DISPUTE_WINDOW_DAYS} days.\n"
        )
        print(
            "  A retention hold does not protect any of it. §12.4's holds are\n"
            "  about retention windows; erasure is the learner's right, and a\n"
            "  hold that could block it would be a hold that defeats it."
        )
        if not _confirm(args.yes, "erase?"):
            return 1

        erase_user(session, args.user_id, reason=args.reason or "operator request")

    print(f"{args.user_id} erased.")
    return 0


# --- helpers ---------------------------------------------------------------


def _date(raw: str) -> dt.date:
    return dt.date.fromisoformat(raw)


def _timestamp(raw: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # Refusing a naive timestamp rather than assuming UTC: this value is
        # published to verifiers as a window boundary, and "assume UTC" is how
        # a local-time entry silently widens or narrows it by hours.
        raise argparse.ArgumentTypeError(
            f"{raw!r} has no timezone. Give one, e.g. 2026-08-01T00:00:00Z -- "
            f"this timestamp is published, and a wrong one misreports which "
            f"credentials are affected."
        )
    return parsed


def _confirm(skip: bool, question: str) -> bool:
    if skip:
        return True
    if not sys.stdin.isatty():
        print(
            f"{question} -- refusing to guess on a non-interactive stdin; pass --yes",
            file=sys.stderr,
        )
        return False
    return input(f"\n{question} [y/N] ").strip().lower() in {"y", "yes"}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

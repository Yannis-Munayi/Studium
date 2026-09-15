# Studium operations

Runbooks for the deployment described in the infrastructure specification
(subsystem 7, v1.0). Everything here is written to be read during an incident:
short, ordered, and specific about which command to type.

| Document | When you need it |
|---|---|
| [`deploy.md`](deploy.md) | Shipping a change; rolling one back |
| [`postgres-restore.md`](postgres-restore.md) | The database is wrong or gone (§9.2) |
| [`volume-restore.md`](volume-restore.md) | Source files are wrong or gone (§9.2) |
| [`signing-keys.md`](signing-keys.md) | Rotation, and the compromise response (§11) |
| [`secrets.md`](secrets.md) | Adding, rotating or revoking a secret (§6) |
| [`retention.md`](retention.md) | The nightly worker, holds, erasure (§12) |
| [`runbook.md`](runbook.md) | Something is on fire and you want the index (§15) |
| [`storage-migration.md`](storage-migration.md) | The volume is filling; move to R2 (§14.3) |
| [`drill-log.md`](drill-log.md) | Recording a quarterly restore drill (§9.3) |
| [`scaling.md`](scaling.md) | A scaling trigger fired (§14) |
| [`content-review.md`](content-review.md) | Classifying a source; the weekly queue triage (ingestion §11) |
| [`evaluation-regression.md`](evaluation-regression.md) | The weekly golden-dataset run (evaluation §13.2) |

## The operational calendar

§3: "Rotation is scheduled, not reactive. ... skipping rotation 'because
nothing's wrong' is how the muscle atrophies."

**The calendar is `backend/content/operational-calendar.yml` and the command
that reads it is `studium ops calendar`.** It is a file in the repository
rather than a reminder service so that it survives the operator, ships with the
deployment, and leaves a git history of who said each obligation was done.

```sh
studium ops calendar --list-all               # everything, by next_due
studium ops calendar --due-this-week          # next 7 days, overdue included
studium ops calendar --due-this-month         # next 30 days
studium ops calendar --on-event on_source_add # what an event triggers
studium ops calendar --complete backup_drill  # done today; advances next_due
studium ops calendar --verify                 # schema, and that runbooks resolve
```

The date queries **exit non-zero when something is overdue**, so the missing
piece — something that tells you without being asked — is a cron line rather
than more code:

```sh
0 9 * * 1  cd /app && studium ops calendar --due-this-week
```

`--complete` rewrites two lines and leaves the rest of the file alone. Commit
the change; that commit is the record.

| Cadence | Task | Where |
|---|---|---|
| Daily, 02:00 UTC | Retention pass | automatic, in-process (§12.1) |
| Daily | Cost trend check | `studium ops check-alerts` (§13.3) |
| Weekly | Evaluation regression | [`evaluation-regression.md`](evaluation-regression.md) |
| Weekly | Review-queue triage | [`content-review.md`](content-review.md) |
| Monthly | Retained-credential audit | [`retention.md`](retention.md) (amendment §3.1) |
| Quarterly | API key rotation | [`secrets.md`](secrets.md) (§6.3) |
| Quarterly | Secret rotation | [`secrets.md`](secrets.md) (§6.3) |
| Quarterly | Restore drill | [`drill-log.md`](drill-log.md) (§9.3) |
| Annually | Signing key rotation | [`signing-keys.md`](signing-keys.md) (§11.2) |
| On source add | License classification | [`content-review.md`](content-review.md) |
| On schema downgrade | Downgrade sign-off | [`deploy.md`](deploy.md) (data layer §12.4) |

Three obligations from SD10's original table are **not** in the file yet, and
that is a gap rather than a decision: monthly invoice reconciliation (§13.2),
the quarterly alert-accuracy review (§16 Tier 3), and destroying the retired
private key 90 days after a rotation (§11.2 step 6) — which SD10 calls the most
forgettable item on the list. Add them with `studium ops calendar --add` once
their owners are settled.

## First principles, when a runbook does not cover it

- **Read before you write.** `studium ops verify-restore`, `studium ops
  check-alerts` and `studium ops retention run --dry-run` all report without
  changing anything.
- **Roll forward, not back, for schema.** §10.4: "Alembic downgrade is
  available but rarely the right choice — data changes typically can't be
  safely reversed." For pure code changes, `flyctl releases rollback` is 30
  seconds and is almost always right.
- **The signing key is the one irreversible loss.** §9.1. Everything else is
  restorable from a backup; a lost signing key makes every credential it signed
  unverifiable forever.
- **Say what you did in the drill log**, even when it went fine. A drill nobody
  recorded is a drill nobody can tell happened.

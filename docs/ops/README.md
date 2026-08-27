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

## The operational calendar

§3: "Rotation is scheduled, not reactive. ... skipping rotation 'because
nothing's wrong' is how the muscle atrophies."

| Cadence | Task | Where |
|---|---|---|
| Daily, 02:00 UTC | Retention pass | automatic, in-process (§12.1) |
| Daily | Cost trend check | `studium ops check-alerts` (§13.3) |
| Weekly | Full evaluation regression | evaluation §13.3 |
| Monthly | Reconcile invoices against `cost-report` | §13.2, §16 Tier 3 |
| Quarterly | API key rotation | `studium ops secrets rotate` (§6.3) |
| Quarterly | Restore drill | [`drill-log.md`](drill-log.md) (§9.3) |
| Quarterly | Alert-accuracy review | §16 Tier 3 |
| Annually | Signing key rotation | [`signing-keys.md`](signing-keys.md) (§11.2) |
| On access revocation | Rotate everything that person could reach, within 48h | §6.4 |

Nothing in the system reminds you about any of these except the daily one.
That is a real gap and it is named in `backend/SPEC_DEBT.md` (SD10) rather than
papered over.

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

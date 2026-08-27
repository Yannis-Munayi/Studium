# Restoring Postgres

Infrastructure §9.2. Total time: 15–45 minutes depending on data volume.

This is the procedure §9.2 names and §9.3's quarterly drill exercises. Run the
drill against a scratch instance; run this against production only when
something is actually wrong.

## Before anything

**Decide what you are restoring to, and write it down.** Fly Postgres keeps a
daily backup plus 14 days of point-in-time recovery, so the restore point is a
choice, not a given. §9.4's RPO is 24 hours from the daily backup and 5 minutes
with PITR — the difference is one flag, and picking wrong costs a day of
learner work.

The question to answer first is *when did the problem start*, not *when did we
notice*. For data corruption those are usually hours apart, and restoring to a
point after the corruption began restores the corruption.

```sh
# What the newest data looks like right now, if the database is still up.
flyctl postgres connect -a studium-db
  SELECT max(created_at) FROM session_turns;
  SELECT max(ran_at), table_name, rows_deleted
    FROM retention_actions ORDER BY ran_at DESC LIMIT 10;
```

That second query matters more often than it looks: "rows are missing" and
"the retention worker did its job" produce identical symptoms, and the audit
trail is what tells them apart before you restore over a working database.

## 1. Restore into a *new* instance

Never in place. A restore that overwrites the running database removes the
option of comparing them, and comparing them is how you find out whether the
restore point was right.

```sh
flyctl postgres create --name studium-db-restore --region yyz \
  --vm-size shared-cpu-2x --volume-size 40

# Point-in-time (§9.4's 5-minute RPO):
flyctl postgres restore studium-db-restore \
  --from studium-db --restore-target-time 2026-08-26T14:05:00Z

# Or the most recent daily snapshot:
flyctl postgres restore studium-db-restore --from studium-db
```

## 2. Verify before you switch anything

```sh
flyctl proxy 15432:5432 -a studium-db-restore &
STUDIUM_OWNER_DATABASE_URL=postgresql+psycopg://postgres:PASSWORD@localhost:15432/studium \
  python -m studium.ops.cli ops verify-restore
```

Eight checks; the module docstring in `backend/studium/ops/restore.py` explains
what each is for. Two are worth calling out:

- **Schema version.** A restore from before a migration is a database the
  current code fails against on its first query, and at 03:00 that reads as a
  code bug. If it reports an older revision, run `python -m alembic upgrade
  head` against the restored instance before going further.
- **Restore point.** Reported and never failed, because only you know the
  target. Compare "newest session turn" against when the incident started.

`verify-restore` exits non-zero when the restore is unusable. **A pass means
the schema, extensions, routines and the integrity Postgres cannot re-check
itself are intact. It does not mean the restore point is the one you wanted.**

## 3. Switch traffic

```sh
flyctl secrets set STUDIUM_DATABASE_URL=<restored app URL> \
                   STUDIUM_OWNER_DATABASE_URL=<restored owner URL> \
  -a studium-backend
```

Setting a secret redeploys, which is what picks up the new URL. Then:

```sh
python -m studium.ops.cli ops smoke-test --base-url https://studium.app
```

## 4. Afterwards

- **Keep the old instance for at least a week.** It is the only copy of
  whatever was in the gap between the restore point and the incident, and
  destroying it is the one step of this procedure that cannot be undone.
- **Re-run the daily cost roll-up** for any day the restore rolled back. It is
  idempotent and recomputes from source, so running it for the whole window is
  safe: `python -c "import datetime as dt; from studium.db import owner_engine;
  ..."` — or simply let the next nightly pass handle a single missed day.
- **Write it up in [`drill-log.md`](drill-log.md)** even though it was not a
  drill. What actually took the time is the thing the next restore needs to
  know.

## What this procedure does not cover

- **A lost signing key.** Not restorable from the database — it was never in
  it (§9.1). See [`signing-keys.md`](signing-keys.md).
- **Source PDFs and extracted intermediates.** They live on the Fly volume, not
  in Postgres. See [`volume-restore.md`](volume-restore.md), and note that
  restoring one without the other leaves `sources.storage_path` rows pointing
  at files that are not there.

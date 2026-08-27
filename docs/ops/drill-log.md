# Restore drill log

Infrastructure §9.3. One entry per quarter, and one per real restore.

> "The point is not the specific procedure — the point is that backup
> mechanisms decay silently and only exercise proves they still work. A backup
> that has never been restored is unverified." — §9.3

## The drill

1. Restore Postgres to a scratch instance ([`postgres-restore.md`](postgres-restore.md)
   steps 1–2; do **not** run step 3).
2. `studium ops verify-restore`.
3. Launch a backend instance against the restored database.
4. Confirm it starts and serves `/health`.
5. Tear the scratch instance down.
6. Add an entry below.

Record what actually took the time, and anything that surprised you. A drill
whose entry reads "went fine" teaches the next person nothing; the useful entry
is the one that says the restore took 40 minutes because the volume size
defaulted to 10GB.

## Entries

### Template

```
### YYYY-MM-DD — quarterly drill / real restore

Performed by:
Restore point requested:
Restore point achieved:

| Step | Time | Notes |
|---|---|---|
| Create scratch instance |  |  |
| Restore |  |  |
| verify-restore |  |  |
| Backend starts |  |  |
| Teardown |  |  |

Total:
Issues:
Changes made to the procedure as a result:
```

---

### 2026-08-26 — no drill has been run

**Status: the backup mechanism is unverified.**

The infrastructure build landed on this date. Fly Postgres's automated backups
are configured and `studium ops verify-restore` is written and exercised
against a seeded local database (Tier 2) — but **no restore from a real Fly
backup has ever been performed**, so:

- the restore *procedure* above is written from Fly's documentation and from
  what the verification needs, not from having done it;
- §9.2's "15–45 minutes" is the spec's estimate, not a measurement;
- §9.4's 4-hour RTO is therefore also an estimate.

That is exactly the state §3 calls "a coin flip that could have been avoided by
testing", and it is recorded here rather than left implicit because the first
drill is the one most likely to find something.

**The first drill should run as soon as there is a deployed database to drill
against**, and its entry should replace the estimates above with measurements.

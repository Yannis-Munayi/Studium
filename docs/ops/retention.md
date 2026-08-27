# Retention, holds and erasure

Infrastructure §12. Data layer §10 defines the windows; this covers running
them.

## The nightly pass (§12.1)

Fires daily at 02:00 UTC, in the backend process, when
`STUDIUM_RETENTION_WORKER=1` (set in `backend/fly.toml`, nowhere else).

**Four stages, not one.** §12.1 asks for the retention worker; building it
found three other jobs written to run on a schedule with no caller anywhere.
They are stages of this pass now:

| Stage | What it does | What its absence looked like |
|---|---|---|
| `retention` | Data layer §10's windows | — |
| `erasure_purge` | Hard-deletes accounts past the 30-day dispute window (data layer §10 step 4) | **Erasure never completed.** The account stayed in `users`, with an email, forever |
| `mastery_decay` | Recomputes `concept_mastery.p_known_decayed` | Sorting and reporting read a number frozen at the learner's last write. Gating was unaffected — it computes decay in SQL |
| `consistency` | Flags dangling `concept_sources.chunk_ids` into the ingestion review queue | Silence. The array cannot carry a foreign key, so a broken reference degrades gracefully by design |

```sh
curl -s https://studium.app/health | jq .retention   # is it armed, when next
studium ops nightly --dry-run                        # every stage, changing nothing
studium ops nightly                                  # run it now
studium ops retention log                            # §12.2's audit trail
studium ops retention run --dry-run                  # the retention stage alone
```

A stage that raises is recorded and the pass continues. One bad predicate must
not stop the erasure purge.

A row is written to `retention_actions` **per policy per pass, including
zeros**. That is what makes "nothing deleted it, investigate" distinguishable
from "the worker never ran", and it is what
`alerts.retention_worker_stale` reads — no audit row in 48 hours fires an
alert, because a silent worker looks exactly like a system with nothing to
delete until the storage alert fires weeks later with the wrong diagnosis
attached.

### If a policy reports INCOMPLETE

The worker deletes in batches of 2,000 and stops after 500 of them per policy
per pass. Hitting that ceiling is usually a genuine backlog — the first pass
after the worker was off for a month. It can also be a predicate matching more
than it should, which is the case worth looking at before re-running.

```sh
studium ops retention log --limit 20      # metadata.batch_truncated
studium ops retention run --dry-run       # the count it would delete now
```

### If a policy FAILED

One bad policy does not stop the others: each runs in its own transaction and
the failure lands in the audit row's metadata. Read it, fix it, re-run. The
worker will retry tomorrow regardless.

## Holds (§12.4)

A hold protects one row from the worker — evidence in a rights dispute, a
research study, a longitudinal analysis.

```sh
studium ops set-retention-hold agent_traces <row_id> "GDPR complaint 2026-114"
studium ops holds list
studium ops release-retention-hold <hold_id> --reason "complaint closed"
```

Released, never deleted. A hold that vanishes when lifted destroys the only
record that the data was deliberately kept, which is what the dispute would
later ask about.

### Holds only work where the worker deletes directly

`set-retention-hold` refuses any other table, and this is the part worth
understanding rather than working around.

A hold is a row the worker consults in the `WHERE` clause of its own `DELETE`.
Postgres's referential actions consult nothing: when the `learning_sessions`
policy deletes a two-year-old session, the cascade removes its turns, traces,
summaries and retrieval checks with no predicate at all. A hold on one of those
rows would sit in the database looking exactly like protection and provide
none — and you would find that out when the dispute reached the point of asking
for the data.

So the refusal names the parent to hold instead. Hold the `learning_sessions`
row; it protects everything cascading from it.

Tables with no retention window are refused too, for the cheerful reason:
nothing deletes them on a schedule, so a hold would be decoration.

### A hold does not block erasure

Deliberately. §12.4's holds are about retention *windows*; erasure is the
learner's right, and a hold that could block it would be a hold that defeats
it. `studium ops erase-user` says so before it runs.

## Erasure (§12.3)

Runs immediately, not on the nightly schedule.

```sh
studium ops erase-user <user_id> --reason "learner request 2026-08-26"
```

The procedure is `studium.privacy.erase_user`, and the order of its steps is
load-bearing: anonymise the aggregates §10 retains *before* deleting their
parents. The v1.0 procedure had it the other way round and cascaded away the
very rows later steps said to keep. The cost-ledger merge in step 2 (the "V13
resolution" §12.3 refers to) folds the erased learner's spend into the shared
`user_id IS NULL` row rather than nulling in place, because
`uq_cost_ledger_day` is `NULLS NOT DISTINCT` and the naive version raises a
unique violation on the *second* learner erased on a given day — aborting the
erasure.

The account row itself is hard-deleted after 30 days by the nightly pass's
`erasure_purge` stage (`privacy.purge_expired_soft_deletes`). The window exists
so an accidental deletion can be undone, which is also why the email is left
intact: recovery needs the identity.

**That stage is new in this build**, and until it existed nothing called
`purge_expired_soft_deletes` outside a test — so every erasure request was
permanently half-finished. If this deployment predates the infrastructure
build, the first nightly pass will purge a backlog; `studium ops nightly
--dry-run` says how many before it does.

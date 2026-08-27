# Divergences from Infrastructure Specification v1.0

The implementation targets
`Documentation/Planning/Sub System 7 - Infrastructure/07-infrastructure-v1.0.md`.
This file records where it differs, and why. It is the sixth of its kind, after
`DIVERGENCES.md` (data layer), `DIVERGENCES-RUNTIME.md` (agent runtime),
`DIVERGENCES-RETRIEVAL.md` (retrieval), `DIVERGENCES-INGESTION.md` (ingestion)
and `DIVERGENCES-EVALUATION.md` (evaluation); the N-series numbering keeps the
six sets distinguishable in code comments.

**This list is shorter than its predecessors, and the spec predicted that.** §1
says infrastructure is "mostly configuration and integration rather than new
behavior. Every choice here is defensible against alternatives, but few of the
choices are load-bearing in the way that agent-runtime state machine choices or
ingestion invariants are." That held: most of §4 through §14 was implementable
as written, and the entries below are mostly places where the spec named a
thing the codebase spells differently, or asked for something whose literal
form would have cost more than it bought.

**One entry is load-bearing and it is not about this spec.** N9 records three
scheduled jobs that had no scheduler — the fifth instance of the pattern
`SPEC_DEBT.md` calls "later subsystems find earlier defects". One of them meant
**every right-to-erasure request was permanently half-finished**.

---

## Load-bearing

### N9 — three jobs were written to run on a schedule and nothing ran them

**Spec.** §12.1 asks for one scheduled task: "Runs as a scheduled task
(cron-like) in the backend process. Fires daily at 02:00 UTC. Iterates tables
with retention policies, deletes rows past retention, logs actions."

**Reality.** Building that scheduler raised the question of what else was
supposed to be on it. The inventory, taken with a grep before any code was
written:

```sh
grep -rn "purge_expired_soft_deletes\|refresh_decay\|check_dangling_chunk_refs" \
     studium/ tests/ scripts/ | grep -v "def \|__init__.py"
```

| Function | Where it says it is scheduled | Callers found |
|---|---|---|
| `privacy.purge_expired_soft_deletes` | data layer §10 step 4; its own docstring | one test |
| `cost_rollup.refresh_decay` | its own docstring: "Daily." | one test |
| `retention.check_dangling_chunk_refs` | its own docstring: "Daily consistency check for `concept_sources.chunk_ids` (§6.2)." | **none at all** |

Each is exported from `studium.jobs`, described as scheduled work, and covered
by a passing test. None had an execution site.

**The first one is the serious one.** `erase_user` marks the account
soft-deleted and anonymises the aggregates §10 retains. Step 4 — the hard
delete after the 30-day dispute window — never happened, because nothing came
back for it. So a learner who exercised their right to erasure remained in
`users`, with their email address intact, indefinitely. The 30-day window is
described in the code as the reason the email is *deliberately* not scrambled
("recovery needs the identity"), which makes the missing step exactly the one
that turns a considered design into a data-retention problem. Every test
passed: `tests/integrity/test_erasure.py` calls `purge_expired_soft_deletes`
directly and asserts it does the right thing, which it does.

`refresh_decay` is milder and instructive about why nobody noticed:
`graph.unlock_status` computes decay in SQL at query time, so *gating* was
always correct. What read the stale column was sorting and reporting — the
symptom is a desk whose concepts are ordered by a number from whenever the
learner last touched them, which nobody would report as a bug.

`check_dangling_chunk_refs` had never been called by anything.

**Code.** `studium.ops.nightly.run_nightly` — four stages, run in order by the
02:00 scheduler, each failure-isolated. The consistency stage flags into
`ingestion_review_queue` rather than logging, because that table is the surface
`SPEC_DEBT` SD1 asked for and logging is the stopgap SD1 exists to complain
about; it de-duplicates against open rows, since a nightly job would otherwise
produce 365 identical queue items a year.

`tests/ops/test_retention_worker.py::test_the_nightly_pass_runs_the_previously_orphaned_jobs`
asserts all three names still appear in that module. A stage removed is this
state returning, and nothing else in the suite would notice.

**Why it is here rather than in SPEC_DEBT.** The specs are not wrong. Data
layer §10 describes step 4 correctly; the infrastructure spec correctly says
the backend runs "the scheduled retention/warming jobs" (§4.2). What was
missing was the thing subsystem 7 exists to provide, and no earlier subsystem
could have provided it.

---

## The rest

### N1 — the published key list lives at two URLs

**Spec.** §11.3: "Public keys are published at
`https://studium.app/api/signing-keys` as a JSON array."

**Reality.** The evaluation build shipped that data at `/api/portfolio/keys`
three weeks earlier, before any spec named a path. `studium-web` calls it, the
Tier 2 tests call it, and §12.4's verification flow documents it.

**Code.** Both are served. `/api/signing-keys` is the canonical path going
forward and is what §11.3 names; `/api/portfolio/keys` remains and returns the
same payload.

**Why not just move it.** A published verification endpoint is a URL that other
people's code holds — that is the entire point of §11.3, which exists so a
verifier can fetch a key without contacting us for permission. Moving it to
satisfy a spec written afterwards would break every verifier that had already
read the first one, to fix nothing. The cost is one alias.

The new path carries slightly more: `compromised_at` per key. A verifier
fetching this list is exactly the audience §11.4 step 3's notice is aimed at,
and putting the flag beside the key means they get it without having read a
notice.

### N2 — `retention_actions.metadata` carries the run grouping

**Spec.** §12.2 writes the DDL out in full: six columns, one index. §18 counts
"a `metadata` field on retention_actions with structured provenance" as its own
item in the data layer v1.2 batch, without saying what goes in it.

**Reality.** §12.2's columns answer "what did the worker delete from this table
and when". They do not answer "did last night's pass complete", because nothing
groups the rows of one pass — the only handle is a time range, and a pass that
died halfway through looks like a pass with fewer policies.

**Code.** Every row from one pass shares a `run_id` in `metadata`, alongside
the policy window, the batch count, whether the batch ceiling was hit, and any
error. The column list is §12.2's, unchanged.

**Why not a column.** §18 counts the batch precisely, and the spec wrote this
DDL out rather than describing it — both signal that the shape is deliberate. A
seventh column would be a wider divergence than using the field the spec added
for exactly this purpose.

A second index is added (`idx_retention_actions_table`). §12.2's `ran_at DESC`
serves "what happened last night"; the question a reviewer chasing one missing
row actually asks is "what has ever been deleted from this table", which the
first index answers only by scanning every pass since the worker was switched
on.

### N3 — a retention hold is refused on any table the worker does not delete from directly

**Spec.** §12.4: "Applied via `studium ops set-retention-hold <table> <row_id>
<reason>` which prevents the retention worker from deleting that specific row."

**Reality.** A hold is a row the worker consults in the `WHERE` clause of its
own `DELETE`. Postgres's referential actions consult nothing. When the
`learning_sessions` policy deletes a two-year-old session, the cascade removes
its turns, traces, summaries and retrieval checks with the referencing table's
owner privileges and no predicate at all.

So `set-retention-hold session_turns <id> "..."` would insert a row that looks
exactly like protection and provides none. The operator would discover that
when the dispute the hold was placed for reached the point of asking for the
data.

**Code.** `ops.retention.place_hold` accepts only tables with a retention
policy of their own, and the refusal names the parent to hold instead — derived
from the ORM metadata, so it cannot drift from the foreign keys. Tables with no
window at all are refused too, with the cheerful reason: nothing deletes them
on a schedule.

Also refused: a hold on a row that does not exist. The usual cause is a
mistyped id, and a hold on a nonexistent row is indistinguishable from one that
worked until someone goes looking.

**What a hold deliberately does not do:** block §12.3's erasure. Holds are
about retention *windows*; erasure is a learner's right, and a hold that could
block it would be a hold that defeats it. `studium ops erase-user` says so
before it runs.

### N4 — `compromised_at` records the earliest possible compromise, and is advisory

**Spec.** §11.4 step 2: "Mark the compromised key with a `compromised_at`
timestamp." Step 3: publish a notice that items "signed with the compromised
key between the earliest possible compromise and rotation should be treated
with additional scrutiny."

**Reality.** Those two steps want different timestamps if you read step 2 alone
as "stamp it now". Stamping the moment of discovery makes step 3's window too
narrow by exactly the interval an attacker was using the key.

**Code.** `mark_compromised` takes an explicit `earliest_possible`, refuses a
timezone-naive value (this timestamp is published; "assume UTC" silently shifts
the window by hours), refuses a value before the key was activated, and refuses
to move an existing mark *later* — narrowing a published scrutiny window
retroactively tells verifiers that credentials they were warned about are fine.
Widening is allowed.

**Advisory, not invalidating.** `verify_item` reports `key_compromised_at`
beside `valid` rather than folding it in. The signature still verifies; the
mathematics did not change. What changed is whether a verifier should believe
only Studium could have produced it — which is their decision, and invalidating
every credential the key ever issued would punish the learners rather than the
attacker.

`credentials_at_risk` derives the actual list §11.4 step 3's notice has to
name, bounded by `compromised_at` at the start and `retired_at` at the end.

### N5 — `studium.storage` is a byte interface, and one caller still needs a path

**Spec.** §4.5: "All code accesses source files via `studium.storage`
interface. Local implementation reads from the Fly volume; R2 implementation
reads via S3-compatible API. Migration is a configuration change plus a
one-time data migration, not a code change."

**Reality.** That promise holds only if the interface is expressible over both
backends. A `Path`-based interface is not: `Path.exists` on a bucket is a
network call with a different failure mode, and an `open()` handle over HTTP is
not the same object. So the protocol is four operations over opaque string
keys, and `LocalStorage` is the implementation that happens to turn a key into
a path.

`studium.ingestion.storage` keeps ingestion §4's `{source_id}.pdf` and
`{source_id}/extracted.jsonl` layout — that layout is ingestion's business —
but composes those names into *keys* and hands them here rather than opening
files itself.

**The leak.** `pdfplumber` opens a file, not a byte stream whose lifetime we
control, so `studium.ingestion.extract` needs a real filesystem path.
`Storage.local_path` exists for that caller and **raises** on the R2 backend
rather than inventing one. The day storage moves to R2, extraction downloads to
a temporary file first — a code change, in one function, announced by a clear
exception at a known call site instead of a subtle failure at runtime.

**The R2 backend is written and unexercised against a real bucket.** §4.5
provisions R2 and does not use it until §14.3's 30GB trigger fires, so there is
nothing to test against and `boto3` is not in the MVP install. Its key handling
*is* tested, because that is the half that has to agree with the local backend:
an object store has no directories, so `a/../b` is a different key in R2 and
the same file on a volume, and both backends normalise identically.

### N6 — the smoke test's session round-trip is opt-in

**Spec.** §10.3: "run `studium ops smoke-test` which exercises: health check,
database connectivity, Anthropic reachability, Voyage reachability (when
provisioned), one full session round-trip against a test user."

**Reality.** Five of those are cheap. The sixth is a real Opus call through the
Orchestrator on every invocation. At one deploy a day that is pennies; at
twenty deploys during an afternoon of fixing something it is both a bill and
twenty synthetic sessions in the reviewer's history.

**Code.** Five checks by default plus the schema-head check; the round trip
runs under `--full` and the command *says which check it skipped and why*
rather than reporting a clean pass over less than §10.3 asked for. When it does
run, the session belongs to the system account, not to a real learner —
`docs/ops/deploy.md` says to use it after a change that touches the agent path.

Also here: provider reachability is a real authenticated request, not a DNS
lookup. A revoked key resolves, connects and TLS-handshakes exactly like a good
one.

### N7 — `STUDIUM_SESSION_SECRET` is registered and unused

**Spec.** §6.1 lists "Session signing secret (used for cookie authentication)"
among the secrets. §6.3: "Rotated when auth infrastructure gets rebuilt."

**Reality.** Nothing signs a cookie. Frontend §14's auth is a placeholder
(`DIVERGENCES-FRONTEND.md` F7) and the backend trusts a `user_id` in a request
body.

**Code.** Registered anyway, with a note saying it is unused and why. §6.3
gives it a rotation trigger, so it will exist eventually; a secret already in
the registry the day auth lands is one nobody has to remember to add, and
`secrets check` will not report it missing in the meantime because it is
required in no environment.

The same reasoning covers `STUDIUM_ALLOW_UNAUTHENTICATED`, which *is* set in
the frontend's deployment. It is in the registry so "why is this single-tenant"
has a written answer, and so removing it the day auth lands is a diff someone
reviews.

### N8 — the CI regression gate has no durable baseline

**Spec.** §10.2 asks for `prompt-regression.yml`, which "runs the evaluation
harness (subsystem 6 §13) against affected datasets when prompts change. Blocks
merge if regression exceeds tolerance."

**Reality.** Evaluation §13.1 step 4 compares against "the last passing run for
the same dataset", read from `evaluation_runs`. The workflow runs against a
throwaway Postgres created for the job, so there is no previous run to compare
to. §8's absolute per-agent thresholds block; §13.2's *tolerance* comparison
has nothing to compare against.

**Code.** The workflow runs the gate and says this in a step that always
executes, rather than reporting a green check that measured half of what §13.1
describes. Closing it needs runs persisted somewhere durable — a small managed
database, or the production one reached read-write from CI — and both are
decisions with cost and access implications that belong to whoever is paying
for them.

### N10 — the weekly full-suite regression is not wired to a cron

**Spec.** Evaluation §13.3 asks for a weekly full-suite run; §10.2 puts the
regression workflow in this subsystem's scope.

**Code.** Not wired. GitHub's `schedule` trigger would run $5–15 of model calls
(evaluation §7.4) at 03:00 against whatever is on the default branch, with
nobody awake to read the result or stop a run that had started failing for an
unrelated reason.

It is on the operational calendar in `docs/ops/README.md` instead, run
deliberately. §8.3 is explicit that "nothing is truly 'immediate' because the
operator sleeps", and a scheduled paid job is the one kind of automation that
assumes otherwise. `tests/ops/test_deployment.py` asserts no `schedule:`
trigger appears in that workflow, because adding one is two lines that would
look reasonable in review.

### N11 — the health endpoint reports provider reachability and gates on Postgres alone

**Spec.** §4.2: "`GET /health` returns 200 when the FastAPI process is healthy
(checked separately for Postgres connection and Anthropic reachability); Fly's
load balancer routes only to healthy instances."

**Reality.** "Checked separately" has to mean *reported* separately rather than
*gating* equally, and the difference matters at exactly the wrong moment. Fly
routes on the status code. If an Anthropic outage failed this check, every
instance would fail it at the same moment, Fly would find nothing healthy, and
a provider outage that agent runtime §16 already degrades gracefully would
become a total outage of a product that still serves the desk, the journal and
every read.

**Code.** Postgres unreachable → 503, because the instance genuinely cannot
serve. Provider keys are reported in a `providers` block and never gate.

Anthropic is not *called* here either: §7.4's monitor polls this every five
minutes and Fly's own check far more often, so a live call per health check
would be a standing bill and would make the endpoint's latency depend on a
third party. What is reported is whether the key is configured, which is the
failure a deploy actually produces.

The frontend's `/health` — the URL §7.4 names — mirrors the asymmetry one layer
up: 503 when it cannot reach the backend (§15's "Backend down" row detects
exactly that way), 200 with detail when the backend answers degraded.

### N12 — the correlation-id log hook is a record factory, not a logging filter

**Spec.** §3: "log messages include correlation IDs".

**Reality.** The obvious implementation — `logging.getLogger().addFilter(...)`
— does not work, and reports success while not working. A `Filter` attached to
a logger runs only for records created *by that logger*. Every line in this
codebase comes from a module logger and propagates to the root logger's
*handlers*, and propagation does not re-run the ancestor logger's filters. So
the filter decorates records logged directly to root, of which there are almost
none.

**Code.** `logging.setLogRecordFactory`, chained rather than replacing, so
another library's factory survives. It runs at record construction, before any
handler question arises, and needs no handlers to exist yet — which in a
uvicorn process at import time they do not.

Found by `tests/ops/test_observability.py`, which logs through
`studium.agents.lecturer` rather than through root for exactly this reason.

### N13 — over-length correlation ids are rejected, not truncated

**Spec.** Not addressed; §7.3 asks only that trace ids propagate.

**Code.** `sanitize` returns empty (and a fresh id is generated) for anything
over 64 characters, rather than truncating to 64. Truncating makes two distinct
long ids collide — silently, and only in the logs, which is where you would go
to notice.

It also has to match the frontend: `resolveCorrelationId` in the Next proxy
rejects at the same length. If the two disagreed, the proxy and the backend
would record a request under different ids, which defeats the one thing the
header exists to do.

### N14 — `retention_actions` is append-only, and 0012 revokes for itself

**Spec.** §12.2 gives no privilege guidance.

**Code.** Added to `APPEND_ONLY_TABLES`, with `REVOKE UPDATE, DELETE ... FROM
studium_app` in migration 0012 rather than relying on 0003. 0003 ends with an
`ALTER DEFAULT PRIVILEGES` granting both on every table created after it, so a
later table that belongs in that tuple has to revoke for itself.

The §14 grants test now scans the whole migration history rather than 0003
alone. Asserting against 0003 had two failure modes and both were wrong: it
fails for a correctly-revoked later table, and it would pass for one added to
the tuple and revoked nowhere at all.

An audit trail the audited process can rewrite answers nothing.

---

## Things the spec asked for that are not built

Listed so the gap is a decision rather than an omission.

| §  | Asked for | Status |
|---|---|---|
| §5.2 | Staging environment | Not built. §5.2 declines it for MVP; the trigger it names ("when you find yourself needing to test something you're afraid to test in production") has not fired. |
| §7.1, §7.2 | Langfuse and Sentry dashboards and alert rules | The *code* emits and scrubs; the dashboards and alert rules are configured in those products' web UIs and cannot be created from here. `studium/ops/alerts.py` records what each externally-watched condition needs configured, and a Tier 1 test fails on any condition with neither a probe nor a named external home. |
| §7.4 | Uptime Robot monitor | Same: an account-side configuration. The endpoint it polls is built and tested. |
| §7.4 | Grafana | §7.4 defers it explicitly ("Grafana deferred until it's needed"). |
| §9.3 | A completed restore drill | **No drill has been run.** `verify-restore` is written and exercised against a seeded database; no restore from a real Fly backup has ever been performed, so §9.2's "15–45 minutes" and §9.4's 4-hour RTO remain the spec's estimates. Recorded at the top of `docs/ops/drill-log.md` rather than left implicit. |
| §10.3 | A verified deploy pipeline | Both images build in CI. Nothing deploys: there is no Fly account or token in CI by design (§6.2). §16's Tier 2 deploy and rollback lines are run by hand against a scratch environment. |
| §13.2 | Actual infrastructure cost | The spec's own figures, reproduced and *labelled as estimates* in the cost report. §13.2 says they become known once the system runs; it has not run. |
| §14 | Any scaling action | None of the triggers is close. `docs/ops/scaling.md` records what each one implies, including two consequences of §14.1 that §14 does not mention: the cache warmer runs per instance and the Orchestrator registry is process-local. |

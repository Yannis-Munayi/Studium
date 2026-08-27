# Divergences from Evaluation and Assessment Harness Specification v1.0

The implementation targets
`spec/Sub System 6 - Assessment and evaluation harness/06-evaluation-v1.0.md`.
This file records where it differs, and why. It is the fifth of its kind,
after `DIVERGENCES.md` (data layer), `DIVERGENCES-RUNTIME.md` (agent runtime),
`DIVERGENCES-RETRIEVAL.md` (retrieval) and `DIVERGENCES-INGESTION.md`
(ingestion); the E-series numbering keeps the five sets distinguishable in code
comments.

Each entry says what the spec asks for, what the code does, and what would have
gone wrong the other way. Four of them (E1, E2, E9, E10) are cases where
following the spec literally produces something that either cannot run or is
quietly wrong — those are marked **load-bearing**.

Two of the four were not divergences from the evaluation spec at all. E9 and
E10 are defects in *already-built* subsystems that this one is the first to
exercise: a portfolio write that always raised, and a session mode that could
be entered and never left. Both had passing test suites. That is worth stating
plainly at the top, because it is the strongest argument in this document for
building the evaluation harness at all.

E14–E16 are a separate category again: defects in *this* subsystem's own code,
found by running Tier 3 and invisible to every tier below it. They are recorded
at the same length as the rest because the pattern generalises — each is a
place where a fake agent and a real agent differ in a way that made the harness
report success.

---

## Load-bearing divergences

### E1 — `signing_keys` does not exist, and `portfolio_items` cannot hold a credential

**Spec.** §12 opens: "Portfolio items are signed, verifiable records of learner
accomplishment. Backed by data layer §6.10's `portfolio_items` and
`signing_keys` tables." §12.2 gives the payload a `kind` of `"assessment_pass"`
or `"subject_completion"` and an `issuer_public_key_id`. §5 states the schema
additions this subsystem needs — four tables, none of them a key store — and
says "All are non-breaking."

**Reality.** Three separate problems, and the spec sees none of them.

1. **There is no `signing_keys` table.** Data layer §6.10 defines
   `portfolio_items` alone. Its `signature` column is documented as "signed
   with a server-held key" with the note "Key management belongs to the
   Infrastructure spec" — subsystem 7, which does not exist yet. So
   §12.2's `issuer_public_key_id` had nothing to point at and §12.4's
   verification endpoint had no key to check against.
2. **`portfolio_item_kind` has neither credential value.** It is
   `('proof', 'code', 'prose', 'derivation', 'diagram', 'notebook')` — six
   kinds of learner *work*. Neither `assessment_pass` nor `subject_completion`
   could be written.
3. **The table already means something else.** `portfolio_items` is the
   learner's body of work: a proof they wrote, an essay, a notebook export,
   hash-chained per enrollment so "did the learner actually do this" is
   verifiable. §12.4's verification endpoint is unauthenticated —
   "Verification does not require an account or authentication" — and takes an
   item id. Served naively, it publishes a learner's coursework to anyone who
   can guess a UUID.

**Code.** Migration 0011 adds `signing_keys` (public key material only) and
extends `portfolio_item_kind` with the two credential values. Credentials share
`portfolio_items` with work rather than getting a table of their own, because
the hash chain already answers "was this row inserted after that one, and has
either been edited since" — a credential benefits from that as much as a proof
does. What separates them is `kind`, and `credentials.verify_item` filters on
it: a non-credential id returns the same "not found" as an unknown one, so the
endpoint cannot even be used to probe which ids exist.

The private key is never stored. `load_identity` reads it from
`STUDIUM_SIGNING_KEY` and nothing writes it anywhere; a signing key in a
database is a signing key in every backup, every read replica, and every
`pg_dump` in a developer's downloads folder.

**If followed literally.** Nothing in §11.5 or §12 could execute — the insert
fails on the enum before it reaches the missing key table. The version that
"works" is the dangerous one: add the enum values, skip the key store, sign
with a key generated at startup. Every credential then verifies against a
public key nobody published, which means it verifies for nobody, and the
learner holds a document that fails every check with no way to find out why.

This is a fifth and sixth addition to the v1.2 batch beyond §5's four. §5's
count, and §19's, are correspondingly wrong.

---

### E2 — `grading_kind` needs three values, and the spec's own example uses the third

**Spec.** §5 addition 2 gives the column the comment
`-- 'deterministic' or 'meta_graded'`. Two values.

**Reality.** §7.2's worked example — the only complete entry in the document —
declares `grading_kind: hybrid  # some deterministic, some meta_graded`. It has
two deterministic properties and one meta-graded one, which is not an unusual
entry but the *normal* one: §8 asks nearly every agent for a mix of mechanical
checks and judgment calls, so almost every well-authored entry is hybrid.

**Code.** `GRADING_KINDS = ("deterministic", "meta_graded", "hybrid")`, with a
CHECK. And the value is **derived from the properties, then cross-checked**
against whatever the YAML declared, rather than trusted:
`datasets._validate_properties` walks the properties, and a declared value that
contradicts them is an authoring error naming both.

**Why derive rather than trust.** Because the spec disagrees with itself, an
author will write `deterministic` on an entry carrying a `meta_graded`
property — and the consequence is silent. The grader would skip the Evaluator
call and report the entry as passing, having evaluated one fewer property than
the entry declares. Nothing in the output distinguishes that from a genuine
pass. Deriving makes the disagreement an error at `studium eval validate`,
before a run has spent anything.

**If followed literally.** The two-value enum rejects §7.2's own example at
insert. The obvious patch — map `hybrid` onto `meta_graded` at sync time —
loses the distinction the column exists to make, and `evaluation_runs` could no
longer answer "how much of this dataset costs money to run", which §15.1 needs
for cost attribution.

---

### E9 — every `record_portfolio_item` effect raised, and no test covered it

**Spec.** Not this spec's, which is the point. Data layer §6.10 makes
`portfolio_items.signature` `NOT NULL` and describes its contents: "previous
item's SHA-256, this item's SHA-256, session id, user id, timestamp, signed
with a server-held key."

**Reality.** `orchestration.effects._record_portfolio_item` constructed a
`PortfolioItem` with `user_id`, `learner_subject_id`, `chain_index`,
`concept_id`, `session_id`, `kind`, `title`, `body`, `language` and
`content_sha256` — and no `signature`. The column is `NOT NULL` with no server
default, so **every invocation raised `NotNullViolation` at insert**.

It is worse than one failed effect. `effects._apply_all` wraps a handler
exception in `EffectApplicationError` and rolls back the whole batch, so a turn
that produced a portfolio item lost its mastery evidence and its journal update
too. The learner does the work, the system records none of it, and the failure
surfaces as a degraded turn with no obvious cause.

Nothing caught it because nothing exercised it: `record_portfolio_item` appears
in the effect dispatch table, in the `ToolEffect` kind allow-list and in the
`end` chunk's id fields, and in no test.

**Code.** `credentials.build_manifest` builds what §6.10 specifies, and the
effect handler calls it. Work items are written **unsigned** when no key is
configured, recording `signed: false` and a reason rather than omitting the
field: the chain digest is their tamper-evidence and the signature is
additional, so refusing to record a learner's proof because a development box
has no `STUDIUM_SIGNING_KEY` trades something real for something marginal.
Credentials take the opposite path — signing is what they are for, so
`issue_credential` raises and §16's row applies.

`parent_item_id` is deliberately *not* set from the chain tail. It is the
revision link ("a draft essay revised three times is four rows"), not the chain
link; chain order is `chain_index`. Pointing it at the previous chain entry
would make every item read as a revision of an unrelated one.

**If followed literally.** There is nothing to follow — this is a defect, not a
divergence. It is recorded here because the evaluation subsystem is what found
it, and because the shape recurs: a `NOT NULL` column whose only writer forgets
it, on a path no test walks.

---

### E10 — `SUMMATIVE_ASSESSMENT` was reachable and inescapable

**Spec.** §11.2 introduces "a new mode: `summative_assessment` (already
reserved in data layer §6.0's `session_mode` enum)" and describes the session's
behaviour in it. It says nothing about transitions, reasonably, since the state
machine is subsystem 2's.

**Reality.** `State.SUMMATIVE_ASSESSMENT` existed. `PERSISTED_MODE` mapped it.
`MODE_ENTRY_STATE` put a session into it. `budget_gate` had a per-mode
multiplier for it. And `TRANSITIONS` contained **not one row with it as a
source**. A session opened in that mode entered the state and the only event
that matched was the wildcard `END_SESSION`; a learner who submitted an answer
got `IllegalTransition`.

Four tiers of tests passed over it because no test ever opened a session in
that mode — the same shape as R14, the transition with no caller that the
v1.0.1 patch was written to catch.

**Code.** Two rows, and the interesting thing is that there are two rather than
three. LAB branches three ways on `ANSWER_SUBMITTED` — correct, incorrect with
attempts left, incorrect and exhausted. A summative attempt branches only on
whether criteria remain, because §11.2 is single-submission: the retry branch
*is* the multi-attempt cycle that closed-book assessment replaces, so a verdict
guard here would reintroduce it. Both rows emit from
`POST /api/session/{id}/practice/submit`, the endpoint LAB and REVIEW already
share.

**Two of §11.2's six conditions turned out to be already enforced, by
accident.** No `PrimitiveRule` lists `SUMMATIVE_ASSESSMENT` in its `valid_from`,
so the command palette raises `InvalidPrimitive` before dispatch; and
`LEARNER_INTERRUPT` has rows only from `LECTURING` and `TUTORIAL`, so "raise
your hand" is unreachable. Both were true before this subsystem and neither was
written down. `tests/eval/test_summative.py` asserts them, which is what stops a
later widening of the primitive matrix from silently handing a learner the
palette mid-examination.

**If followed literally.** The assessment engine would be built against a state
machine that cannot run it, and the failure would appear as an
`IllegalTransition` in front of a learner sitting an examination.

---

## Other divergences

### E3 — `golden_dataset_entries` needs a stable entry key

§7.2 gives every entry an `id:` (`entry_001`). §5's DDL has `entry_index` and
no column for it. Without one, a failing entry can be named only by its index —
and indices shift when an entry is inserted, which silently repoints every
reviewer note, CI log line and `evaluation_results` cross-reference written
against a number. `entry_key` is `UNIQUE (dataset_id, entry_key)` and is what
`sync` matches on, so inserting `entry_002a` between two entries renumbers
nothing.

### E4 — `entry_count` has no writer, and `regression_tolerance` has no column

Two gaps in §5 addition 1, both of the same kind: a value the spec relies on
elsewhere with nowhere to come from.

`entry_count INTEGER NOT NULL DEFAULT 0` has no named writer anywhere in the
spec. Left alone it reads 0 forever while the dataset has twenty entries — and
§14.3's queue-depth dashboards read exactly this sort of column. `sync`
recomputes it from `COUNT(*)` inside the sync transaction rather than
incrementing, so it cannot drift; the Tier 2 test asserts they match.

§13.2 puts `regression_tolerance` in the dataset's YAML and §5 gives it no
column. The CI gate can read the YAML — it has a checkout — but the weekly
scheduled run (§6.2) does not, so the gate that fires without a human present
was the one with no tolerance to read. Added as a JSONB column, materialised by
`sync`.

### E5 — money is `NUMERIC`, not `REAL`

§5 gives `cost_usd REAL` on both `evaluation_runs` and `evaluation_results`.
Data layer §5 is explicit that money is `NUMERIC`, and the §14 convention test
enforces it on every `*_usd` column in the schema — it failed on both tables
the moment they were added, which is the test doing its job.

Not pedantry: a run's cost is the sum of ~20 per-entry costs each in the 10⁻³
range. `REAL` carries about seven significant digits, so the total drifts from
its parts in the fourth decimal — the exact failure that made
`cost_ledger.cost_usd` a generated column in the first place.

### E6 — the `CASCADE`/`RESTRICT` pair on datasets is decorative

`golden_dataset_entries.dataset_id` is `ON DELETE CASCADE` and
`evaluation_results.entry_id` is `ON DELETE RESTRICT`, both per §5. Deleting a
dataset therefore cascades to its entries and is then refused at the second
hop by the results — so the delete fails either way, just with an error naming
a table the caller did not mention.

Left as specified rather than "fixed". §7.3 is explicit that datasets are
retired via `active = FALSE` and never deleted, so nothing takes this path;
changing the CASCADE to RESTRICT would make the failure clearer and would also
be a schema change made to improve an error message on a path that should not
exist. `sync` refuses the deletion earlier and with a better message, which is
where the fix belongs.

### E7 — §8's metrics and §7.2's properties are not joined, and the gap fails green

**The gap.** §8 names metrics per agent ("grounding rate", "citation
validity", "unlock respect") and states blocking thresholds against them. §7.2
names *properties* on a dataset entry (`cites_provided_passages`,
`within_word_limit`). Nothing in the spec says which property produces which
metric, so "citation validity < 100% blocks deploy" has no input.

**Why it matters more than it looks.** The failure is silent and reads as
success. A Lecturer dataset with no `citations_resolve` property leaves
§8.1's threshold with nothing to measure: `metrics.compute` skips the metric,
`metrics.violations` finds nothing to violate, and the gate reports green. That
is strictly worse than having no threshold at all, because the green gets read
as evidence that citations were checked.

**Code.** `metrics.METRICS` pins the join — every §8 metric names the property
that produces it — and `metrics.coverage_gaps` reports blocking metrics a
dataset's properties would never produce. `studium eval validate` prints those
as warnings, so a dataset can be built up incrementally; the CI workflow treats
one as a failure, which is where it needs to bite.

`compute` also reports `n` alongside every value, because 1.0 measured over a
sample of one is not a clean bill of health and a bare percentage hides that.

### E8 — precision@k divides by what was returned, not by k

§9.2: "Precision@k. Of the k retrieved chunks, how many are relevant? Score =
|retrieved ∩ relevant| / k."

Retrieval returns fewer than k routinely — retrieval §13 makes thin grounding a
normal, flagged state of an evolving corpus rather than an error. Dividing by k
charges the retriever for passages that do not exist: a query where the corpus
holds exactly two relevant chunks and retrieval returns both scores 2/6 = 0.33
under the spec's formula and 1.0 under this one. The second is what precision
means, and the first would make "improve the corpus" and "return more junk"
look identical to the metric.

Two smaller notes in the same section. §9.2's *prose* for recall ("Of the k
retrieved chunks, how many are in the relevant list?") describes precision; its
formula is correct and is what is implemented. And "drop by more than 5%"
(§9.4) is read as **absolute**, matching the same decision for §13.2's
`aggregate_score_drop_max` — both specs write a tolerance as a bare number
against a metric that is already a proportion, and one reading for both beats
two readings that differ by which section you happen to be in. Relative would
give the weakest datasets the tightest gate, which is backwards.

### E11 — the reviewer role's permissions were wider than §14.1 allows

§14.1 is the first place the reviewer role is defined operationally. Measured
against it, `studium.acl` was wrong in both directions.

**Too permissive.** `assert_can_write` returned unconditionally for a reviewer,
on every table. §14.1 says the role does *not* "change signing keys or issue
portfolio items directly (portfolio items only issue via the summative
assessment flow)" or "modify learner mastery estimates directly". Those are now
a denylist. And the ACL had no notion of deletion at all, so `assert_can_write`
returning was implicitly granting deletes on the whole schema — §14.1 says the
role does not "delete rows from any table", and `assert_can_delete` now says so.
That one matters: §14.1 grounds it in "data retention is per data layer §10,
not per-reviewer discretion", and a reviewer who can delete can quietly undo a
written retention policy for one row and leave no trace but a gap.

**Too restrictive.** `assert_can_read` excluded `audit_log`. §14.1 says a
reviewer may "read every table in the schema". Following §14.1 here, because
§14.2 hands the reviewer direct database access *on purpose* — "locking them
out of raw access would prevent them from responding to unforeseen situations"
— so denying one table through the ACL while handing over `psql` is theatre
that makes the ACL describe something other than the real posture.

### E12 — the CI workflow gates on measurement, not on scores

§13.3 puts affected regressions on pushes to `yannis`/`main` and a weekly
full-suite run. Neither is wired into `.github/workflows/evaluation.yml`, and
the workflow says so at length rather than quietly omitting them.

Two reasons. They spend real money per run ($5–15, §7.4) and need an API key in
CI, which is subsystem 7's to provision. And a regression gate wired to a push
makes a model provider's bad afternoon look like a failing build — the signal
is real but it is not a signal about the diff.

What the workflow gates instead is the machinery those runs depend on, plus the
one thing that would make a future gate lie: a dataset that leaves a blocking
threshold unmeasured (E7). `studium eval gate` already exits non-zero on a
§13.2 or §8 violation, so the job is a wiring exercise once there is a budget
for it.

### E13 — §15.3's revert leaves evaluation sharing the learner budget cap

§15.2 and §15.3 are visibly a train of thought in the shipped document: a
`cost_evaluation_usd` column is added, the count is corrected from four to
five, then to six with `daily_evaluation_usd_max`, then the whole thing is
reverted — "no new ledger columns from this subsystem. Four additions total,
not six."

The final decision is implementable and is what is implemented: evaluation cost
books to `cost_agent_usd`, and "is this evaluation or real learner work" is
answered by the `evaluation_runs` table.

But the revert took the budget cap with it, and that half does not survive
contact with the other numbers. Evaluation runs now draw on the reviewer's
ordinary `user_budget_caps` — $5 daily soft, $8 daily hard. §7.4 puts a full
regression at $5–15. **One full-suite run can exhaust the reviewer's daily hard
cap**, and §13.3 wants one weekly plus per-change runs on every integration
push. The reviewer would then be unable to open a learning session, for reasons
that have nothing to do with learning.

Not resolved here, because it is a spec decision and inventing a seventh column
after §15.3 explicitly declined one would be building around a choice rather
than raising it. Recorded in `SPEC_DEBT.md` (SD9) with the trigger that should
close it, and noted in the CI workflow as what blocks the §13.3 job.

---

## What Tier 3 found in this subsystem's own build

Three defects, all in code written for this subsystem, none visible to any
tier below the paid one. Recorded here rather than quietly fixed because the
*pattern* is the useful part: each is a place where a fake agent's behaviour
and a real agent's behaviour differ in a way that made the harness report
success.

### E14 — real agent calls are foreign-key bound to rows a fixture does not create

`fixtures.build_context` minted every id by `uuid5` derivation, and said so
proudly: stable, reproducible, no database needed. That is correct for Tier 1
and wrong for anything real.

Every billable call writes an `agent_traces` row, and `traces._write_sync`
writes a `session_turns` row first. That takes `next_turn_index`, which does
`SELECT id FROM learning_sessions WHERE id = :session_id FOR UPDATE` and
requires exactly one row; the turn also carries `concept_id`, a foreign key
that every agent's `call_spec` copies off `ctx.focus_concept_id`. So the first
real Lecturer call raised `NoResultFound`, and once that was fixed, the first
real Evaluator call raised `ForeignKeyViolation` on `concepts` — **after the
model had been paid for, both times.**

`fixtures.ensure_eval_session` and `ensure_eval_concept` provision real rows,
under a never-published `evaluation-harness` subject owned by the system
account. Skipping the trace instead would have been the tempting fix and the
wrong one: §15.1 attributes evaluation cost *through* traces, §5's
`agent_trace_id` links every result to one, and agent runtime §19 is explicit
that a billable call without a trace is unaccounted spend.

One row per entry rather than one per dataset, for the concept: the concept id
is part of every agent prefix's cache key (`prompts._key(concept["id"], ...)`),
so sharing one row would give two entries with different concepts the same
cache key while their prefix text differed.

### E15 — a streaming agent's cost was recorded as zero

`_consume_stream` summed `cost_usd` from `trace`-kind chunks. No streaming
agent emits one: agent runtime §20's stream carries `text`, `tool_effect`,
`end` and `degraded`, and the cost is known only after the stream closes, when
the client writes the trace.

So **every Lecturer and Tutor entry recorded $0**. A full regression would have
written `evaluation_runs.cost_usd = 0` and told §15.1's attribution that a
fifteen-dollar run was free — the ledger discipline agent runtime §19 was built
around, defeated by the one subsystem whose §15 is about paying for itself.

Invisible below Tier 3 by construction: a fake agent's cost legitimately *is*
zero, so every Tier 1 and Tier 2 assertion about cost was satisfied by the bug.
The fix reads the cost back from `agent_traces` by `session_turn_id`, which is
also the authoritative number rather than a second opinion — it is what the
cost roll-up bills from.

### E16 — the first Tier 3 attempt hung, and the hang was the diagnosis

Worth recording because the debugging path was misleading. The first full run
produced no output for fifteen minutes. Three plausible causes were checked and
eliminated in order: the API itself (a Haiku round trip: 0.7s), the model
routing (`claude-opus-4-8` at `effort: high`: 2.8s), and a dead default
`STUDIUM_DATABASE_URL` pointing at port 5432 (connection *refused*, which is
fast, not a hang).

The actual cause was E14 — but surfaced as a hang rather than an error because
`pytest -q` buffers, and five tests each failing after a real model call is
slow. **The lesson for the next paid tier: run it `-v` from the start.** A tier
that costs money is the one where you least want to be guessing at which of
five tests you are in.

Also noted while investigating, and *not* changed: the codebase pins
`claude-opus-4-8` while evaluation §7.4 costs the harness at "Opus 5 pricing".
Model choice is subsystem 2 §18's, changing it would alter every agent's
behaviour, and it would invalidate every regression baseline taken before the
change — which is precisely what §3's prompt-and-model pinning exists to make
visible. A deliberate decision for whoever revises subsystem 2, not a silent
edit from here.

---

## Not divergences, recorded to stop the question recurring

**The `meta_graded_needs_rubric` CHECK looks stricter than it is.** §7.2's
worked example puts the rubric on the *property*, not on the entry, so a hybrid
entry with per-property rubrics has a NULL entry-level `rubric` and is
perfectly valid. The first draft of the constraint was `rubric IS NOT NULL` and
rejected the shipped Lecturer dataset — caught by the Tier 2 sync test, not by
anything at the model layer, because the ORM tests never insert the real
content tree. The constraint now uses `jsonb_path_exists`, which is immutable
and therefore legal in a CHECK where the subquery it would otherwise want is
not.

**Meta-graded metrics are all non-blocking, and that is §8's choice.** Every
threshold §8 states is against a deterministic metric; every meta-graded one is
sent "to reviewer for judgment". That is right and worth not quietly
improving: gating a deploy on one model's opinion of another model's output
makes §19 open question 2 — meta-grader stability — a production incident
rather than a measurement.

**Evaluation runs do not apply their effects.** A run consumes a streaming
agent's `tool_effect` chunks for the trace id and cost, and applies none of
them. An evaluation run is not a learner's turn: applying them would fill
`content_artifacts` with fixture output and the reviewer's queue with the
harness's own test cases.

**The retake pool guard is automated even though §11.5 calls it manual.**
§11.5: "if the pool is small enough that a retake would repeat problems, the
reviewer must expand the pool before retakes are permitted. This is a manual
guard rather than an automated policy." Read literally, nothing checks it.
`check_eligibility` checks it anyway — a retake drawing the same problems is no
longer assessment on unseen material, and §3 makes unseen material the thing
separating a credential from a transcript. Automating the *check* does not
automate the fix; expanding the pool is still the reviewer's work, and the
refusal says so.

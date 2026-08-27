# Changelog — Studium data layer

Spec §13 CI check 4: no migration is committed without an entry here
describing the *semantic* change, not just the DDL.

## Against spec v1.1

The specification was ratified as v1.1 on 15 August 2026, folding in the
corrections from the v1.0 build. These migrations bring the database to that
version. Divergences that remain are in `DIVERGENCES.md`.

### 0004_widen_cost_ledger

The ledger stops being a single number. One column per cost source
(`cost_agent_usd`, `cost_content_usd`, `cost_ingestion_usd`,
`cost_summary_usd`, `cost_grading_usd`) with `cost_usd` generated as their sum,
so the total cannot drift from its parts and "where is the money going" is
answerable without a second query.

Cache-write tokens split into `cache_write_5m_tokens` / `cache_write_1h_tokens`
because the two TTLs bill differently (1.25× vs 2× base input). Existing rows
are booked to the 5-minute column — the default TTL, and the reading that
under-states rather than over-states.

`idx_cost_ledger_user_day` narrowed to `WHERE user_id IS NOT NULL`:
post-erasure rows are never fetched by owner.

Non-additive. Needs a maintenance window per §13; a formality with no
production data.

### 0005_add_memory_updated_at

`session_summaries` and `retrieval_checks` gain `updated_at` and its trigger.
Both describe themselves as regenerable, so both are mutable, and without the
column there was no way to tell a first generation from a fifth.

### 0006_align_functions_to_spec_v1_1

`uuid_generate_v7()` adopts v1.1's component-by-component body. Functionally
equivalent to the 0001 version — both emit 32 characters — but each piece is
explicitly `lpad`-ed, so it cannot shorten if an intermediate value is small.

`bump_session_cost()` renamed to `accumulate_session_cost()` with the spec's
subquery form; trigger renamed to match.

### 0007_add_content_cost_attribution

Adds `content_artifacts.generated_for_session_id`. §6.12 attributes content
cost to "the learner in whose session the generation was triggered", but the
table had no session or user column — the rule could not be implemented as
written. NULL means Curator pre-generation, which routes to the system account.

Also narrows the two nullable-FK indexes on the table to the partial form §7
now names as preferred.

### 0008_seed_system_user

Creates the synthetic account §6.12 refers to but never defines, at a fixed id
(`00000000-0000-7000-8000-000000000001`). It cannot be NULL: §6.12 reserves a
NULL owner for post-erasure aggregates, and overloading it would make an erased
learner's spend indistinguishable from shared infrastructure cost. No password
and no auth sessions, so it cannot be logged into.

A data migration, separate from 0007 per §13 so it can be re-run without
re-applying the DDL.

## For the retrieval subsystem (retrieval spec v1.0 §5)

### 0009_add_chunk_type_and_tsvector

The two additive columns the retrieval spec asks be folded into the data
layer's v1.2 batch. Both are prerequisites for hybrid search, so they are here
rather than waiting on the rest of that batch.

`source_chunks.chunk_type` (new `chunk_kind` enum, `DEFAULT 'body'`) says what
kind of text a chunk is. It is what lets retrieval exclude headings and
bibliography entries from results — structurally present, useless as evidence —
and what keeps code and math blocks atomic through chunking.

`source_chunks.tsvector_text` is a `GENERATED ALWAYS AS ... STORED` column with
a GIN index: the keyword half of hybrid search. Generated rather than
trigger-maintained so it cannot drift from `text`; stored because Postgres
cannot index a virtual generated column, and because it is read on every
keyword query and written once per chunk.

The `english` configuration is baked into the generation expression — the
two-argument `to_tsvector` is immutable, which is what makes it legal in a
generated column at all, while the one-argument form reads a GUC and is only
stable. A second corpus language therefore means a per-language column, not a
parameter.

Additive. The generated column backfills in one pass, which is seconds at MVP
corpus size.

## For the ingestion subsystem (ingestion spec v1.0 §5, §7.3)

### 0010_add_ingestion_review_and_provenance

Six changes: the ingestion spec's five, plus one it needed and did not name.

**`ingestion_review_queue` is the S3 resolution.** `content_review_queue`'s
`has_target` CHECK requires an artifact or a session turn, because it models
review of *generated content* and a reviewer inspecting a Lecturer segment is
always inspecting a specific thing the runtime produced. An ingestion failure
has neither: a PDF that will not extract has produced no artifact and belongs
to no turn. So every ingestion-side write to that table failed the constraint,
and retrieval's embedding worker — the first caller to need one — could only
log. A chunk that never got a vector is invisible to vector search, so the
failure was real and the only record of it was a log line nobody greps.

The new table takes source, chunk, subject or concept as its target, which is
what ingestion-side failures actually point at. A dedicated table rather than a
relaxed CHECK on the old one: dropping `has_target` would also let a content
review row through with no target at all, and that constraint is correct for
the rows it governs.

Four partial indexes, one per target column. All four cascade on delete and
Postgres does not index the referencing side of a foreign key, so without them
deleting a single source sequential-scans the queue four times.

**`sources.extractor_version` / `normalizer_version`** record which code
produced the text currently on a source. Both nullable: the draft
carry-forward's corpus was ingested by a pipeline that recorded neither, and
back-filling a guess would be precisely the confident-wrong-provenance the
ingestion spec's §13 invariant exists to forbid. Their absence is what makes a
corpus-wide re-extraction decidable at all — without them the only way to learn
which sources predate an extractor change is to redo every source and diff,
which means paying to re-embed books that did not change.

**`source_chunks.extraction_confidence`** (REAL, default 1.0, 0–1 CHECK) with a
partial index below 0.7 for the reviewer's "what is doubtful in this source"
query. The default asserts nothing about quality — pdfplumber exposes no
confidence, so 1.0 means "the extractor had no opinion" and has to be read
alongside `extractor_version`.

**`source_chunks.superseded_at`** marks chunks replaced by a re-extraction.
They are not deleted: `content_citations.source_chunk_id` is
`ON DELETE RESTRICT`, so a citation already written against an old chunk must
keep resolving. Retrieval filters them out of new results instead, through the
partial index on the live rows — `search.py` gained that filter on its curated,
vector, keyword and sibling-expansion paths, while `citations.py` deliberately
did not.

**`ingestion_job_kind` gains `normalize`.** Not in the spec's list of
additions, and required by it: §6.4 triggers normalisation from
`ingestion_jobs` rows with `kind = 'normalize'`, a value the enum did not have.
See DIVERGENCES-INGESTION (I1). `ADD VALUE` is transactional from Postgres 12
on and nothing in the migration uses the new value, so it is safe inside
Alembic's transaction. The downgrade rebuilds the type, which is the only way
Postgres removes a value, and deletes any `normalize` jobs first rather than
relabelling them — a normalize job renamed `chunk` would be picked up by the
chunk worker and run against text that was never normalised.

Additive throughout: one new type, one new value, one new table, four new
columns, nine new indexes. Existing rows are untouched beyond two column
defaults, which Postgres 16 applies without a rewrite.

## For the evaluation subsystem (evaluation spec v1.0 §5, §12)

### 0011_add_evaluation_harness

The evaluation harness's storage. §5 names four additions; this applies seven.
The three extra ones are the subject of `DIVERGENCES-EVALUATION.md`.

**The four §5 names.** `golden_datasets` and `golden_dataset_entries` hold the
authored cases, materialised from `content/evaluation/` by `studium eval sync`
— the repo stays the source of truth (§4), and these exist so a scheduled run
and a dashboard can query what the CI gate reads off disk. `evaluation_runs`
and `evaluation_results` hold outcomes, with `prompt_hash` matching
`agent_traces.system_prompt_hash` so §3's "prompt versions are what get
evaluated, not agents" is enforceable rather than aspirational.

**`signing_keys` is new, and §12 already depended on it.** §12 describes
credentials as "backed by data layer §6.10's `portfolio_items` and
`signing_keys` tables". §6.10 defines only `portfolio_items` and defers key
management to subsystem 7 without naming a table, so §12.2's
`issuer_public_key_id` had nothing to point at. Public key material only: the
private half is loaded from the environment and never written anywhere, because
a signing key in a database is a signing key in every backup and every
`pg_dump`. A partial unique index keeps at most one un-retired key per issuer —
two current keys make "which key signed this" unanswerable by the issuer
itself. Retired keys are kept, never deleted; deleting one invalidates every
credential it ever signed.

**`portfolio_item_kind` gains `assessment_pass` and `subject_completion`**
(§12.1). The existing six values are learner *work* — proof, code, prose,
derivation, diagram, notebook — and a credential is a different lifecycle with
no value to be written under. The downgrade deletes credential rows rather than
relabelling them: a credential relabelled `prose` would be a signed, externally
verifiable claim of mastery sitting in the learner's work portfolio, and the
signature would still verify.

**`golden_datasets.regression_tolerance`** (§13.2). The tolerance lives in the
dataset YAML and §5's DDL gives it no column, so a run driven from the database
rather than from a checkout had nowhere to read it — which is every scheduled
run.

**Four departures from §5's DDL as written**, each caught by an existing §14
convention test rather than by inspection:

- `cost_usd` on both run and result tables is `NUMERIC(12,4)`, not `REAL`. Data
  layer §5 is explicit that money is NUMERIC. A run's cost is ~20 per-entry
  costs in the 10⁻³ range summed, and REAL's seven significant digits let the
  total drift from its parts.
- `evaluation_results` gains `idx_eval_results_trace` on `agent_trace_id`, and
  a `UNIQUE (run_id, entry_id)`. Without the index the §10 retention job
  sequential-scans every result once per expired trace; without the constraint
  a retried entry writes a second row and the aggregate silently double-counts
  it.
- `evaluation_runs` gains `idx_eval_runs_baseline`, which is the shape §13.1
  step 4 actually queries ("the last passing run for the same dataset") and
  which neither of §5's two indexes serves.
- `golden_dataset_entries` gains `entry_key`, the stable `entry_001` identifier
  §7.2's YAML gives every entry. Without a column for it a failing entry can be
  named only by index, and indices shift on insertion — which would silently
  repoint every reviewer note written against a number.

Additive throughout: one new type, two new values on an existing type, five new
tables, one new column, eleven new indexes. Existing rows are untouched.

## For the infrastructure subsystem (infrastructure spec v1.0 §11.4, §12)

### 0012_add_retention_operations

The infrastructure spec's §18 names three additions plus one implied by §12.2.
All four land here.

**`retention_actions` is the retention worker's audit trail.** §12.2's DDL is
reproduced column for column — the spec wrote it out in full, so departing from
it would be a wider divergence than the one thing it does not carry, which is
the grouping of a nightly pass. That went into `metadata`, which is precisely
the fourth item §18 counts ("a `metadata` field on retention_actions with
structured provenance"): every row from one pass shares a `run_id`, and each
carries the policy window, the batch count and whether the batch ceiling was
hit. See DIVERGENCES-INFRASTRUCTURE (N2).

A row is written **per policy per pass, including zeros**. §12.2's audit trail
answers "why did that data go away"; it only answers the other case — "nothing
deleted it, investigate" — if a pass that deleted nothing is distinguishable
from a pass that never ran. `alerts.retention_worker_stale` reads exactly that.

Append-only, and this migration revokes `UPDATE` and `DELETE` on it from
`studium_app`. 0003's `ALTER DEFAULT PRIVILEGES` grants both on every future
table, so a table added afterwards that belongs in `APPEND_ONLY_TABLES` has to
revoke for itself — which is why the §14 grants test now scans the whole
migration history rather than 0003 alone.

Two indexes rather than §12.2's one. `idx_retention_actions_recent (ran_at
DESC)` is the spec's and serves "what happened last night";
`idx_retention_actions_table` serves "what has ever been deleted from this
table", which is the question a reviewer chasing one missing row actually asks
and which the first index answers only by scanning every pass ever run.

**`retention_holds` protects one row from the nightly worker** (§12.4).
Released rather than deleted when lifted: a hold that vanishes destroys the
only record that the data was deliberately kept, which is what a rights dispute
would later ask about. The partial unique index permits one live hold per
(table, row), so a second `set-retention-hold` updates the reason rather than
stacking — two live holds with different reasons make "why is this still here"
unanswerable.

`placed_by` is `ON DELETE SET NULL` rather than CASCADE. A hold outlives the
operator who placed it, and deleting a reviewer's account must not quietly
release evidence being kept for a dispute.

**Holds are honoured only where the worker deletes directly**, and that is
enforced in code rather than left as a caveat. Several policies delete a parent
and let Postgres cascade; a cascade runs with the referencing table's owner
privileges and consults no predicate, so a hold on a cascade-reached row would
read as protection and provide none. `ops.retention.place_hold` refuses those
tables and names the parent to hold instead, deriving the parent set from the
ORM metadata so it cannot drift from the foreign keys. See
DIVERGENCES-INFRASTRUCTURE (N3).

**`signing_keys.compromised_at`** (§11.4 step 2) records the *earliest
possible* compromise rather than the moment of discovery. §11.4 step 3
publishes the window "between the earliest possible compromise and rotation",
and stamping when someone noticed makes that window too narrow by exactly the
interval an attacker was using the key. A CHECK keeps it at or after
`activated_at`; `keys.mark_compromised` refuses to move it later, because
narrowing a published scrutiny window tells verifiers that credentials they
were warned about are fine.

Advisory throughout. A compromised key still verifies what it signed — the
mathematics did not change — and invalidating every credential it ever issued
would punish the learners rather than the attacker. `credentials.verify_item`
reports it beside `valid` and lets the verifier decide. See
DIVERGENCES-INFRASTRUCTURE (N4).

Additive: two new tables, one new column, six new indexes, one new trigger, one
revoke. Existing rows are untouched.

## Initial build (against spec v1.0)

### 0001_initial_schema

The full schema of spec §6: 34 tables, 78 indexes, 18 enum types, the
`uuid_generate_v7()` and `set_updated_at()` functions, one `updated_at` trigger
per mutable table, the `bump_session_cost()` trigger, and the
`refresh_subject_metadata()` procedure.

Departures from the spec text, each explained in `DIVERGENCES.md`:

- `uuid_generate_v7()` is rewritten. The spec's fallback concatenates to 33 hex
  characters, so every `INSERT` would fail with `invalid input syntax for type
  uuid`. Extensions installed are `pgcrypto`, `citext`, `pg_trgm` and `vector`;
  `uuid-ossp` is dropped because it has no v7 generator.
- `content_artifacts` and `learning_sessions` gain `created_at`/`updated_at`
  and their triggers.
- `learning_sessions` loses `summary`, `key_points` and `open_threads`; those
  live only on `session_summaries`.
- Twenty foreign-key columns gain the index §7 claims they already have.
- `assessment_attempts.user_id` / `learner_subject_id` and `cost_ledger.user_id`
  are nullable with `ON DELETE SET NULL`, so §10's right-to-erasure can retain
  de-identified rows instead of cascading them away.
- Composite foreign keys tie `user_id` to `learner_subject_id` on the four
  tables that denormalise both.
- Hash columns are `TEXT` + a length `CHECK` rather than `CHAR(64)`.
- `concepts.metadata` and `subjects.assessment_threshold` are added; the spec
  refers to both but defines neither.
- `portfolio_items.chain_index` makes the hash chain totally ordered.

### 0002_add_module_slug

Adds the nullable `concepts.module_slug` that §12 names as the landing place
for the draft's `Module` grouping, plus a partial index and a slug-format
check. Additive; no downtime.

### 0003_grants

Creates the `studium_app` and `studium_owner` roles and revokes `UPDATE` and
`DELETE` on the five append-only tables (`mastery_events`, `journal_events`,
`review_events`, `agent_traces`, `audit_log`) from the application role.

The owner role exists because §10's retention and erasure jobs must delete from
exactly those tables. Default privileges are set so future tables inherit the
same posture without a follow-up migration.

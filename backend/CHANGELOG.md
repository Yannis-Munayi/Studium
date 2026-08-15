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

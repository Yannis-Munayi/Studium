# Studium — Data Layer Specification

**Subsystem 1 of 7. Version 1.2. Status: build-ready.**

*This revision consolidates v1.0 + v1.1 + all 25 pending items surfaced across seven build cycles into a single authoritative document. Everything in this spec supersedes prior versions where they conflict. The v1.1 → v1.2 redline (delivered alongside) enumerates specifically what changed and why. Nothing else in the prior specs is authoritative for the data layer.*

*Written after all seven subsystems built. Every schema decision here has been exercised against real Postgres 16.15 with pgvector 0.8+; every constraint has been verified by a test that could catch its violation. Where earlier drafts were educated guesses about what the code would need, this revision is grounded in what the code actually needed.*

---

## 1. Overview

This subsystem is the ground truth for Studium's data — the schema, the constraints, the indexes, the retention rules, the migration procedure. Every other subsystem reads from it and writes to it; nothing else in the project can be correct if this layer is wrong.

The v1.2 revision folds in 25 items surfaced across the seven build cycles. Twelve items were open questions or partial specifications from v1.1 that the builds answered. Thirteen were additions required by subsequent subsystem specs — most of them non-controversial (new columns, new tables), some of them substantive (a whole new domain for evaluation, a formalized retention operations layer). All 25 are here in one document rather than dispersed across a v1.1 spec plus patches plus amendments plus build reports.

A senior engineer with this document and Postgres 16 can produce a schema Studium runs against. The migration path from v1.1 (applied migrations 0001–0009) to v1.2 (adds 0010–0012) is captured in §5. Every table, every index, every constraint that ships is documented here; anything the code has that this document does not is a defect in this document that the next revision must close.

## 2. Scope and non-goals

**In scope.**

- The complete schema: tables, columns, types, constraints, indexes.
- Migration procedure: how schema changes get applied, how they get rolled back, what CI enforces.
- Retention policy: what data lives how long, what deletes what, what schedules the deletion.
- Constraint discipline: which constraints are database-enforced, which are code-enforced, and why the choice was made in each case.
- Performance targets: measured latency and throughput expectations, with references to which build measured them.
- Cost accounting: the ledger structure that supports per-user, per-operation, per-cost-line tracking.
- The provenance invariant infrastructure: schema-level support for the "no silent fallback attribution" principle formalized in ingestion §13.
- Signing key lifecycle for portfolio items: rotation, publication, compromise response.

**Explicitly out of scope.**

- Query patterns and their optimization. Consumers of this layer decide how they query; this document defines what's queryable.
- Application-layer caching. Retrieval subsystem §14 handles caching for its own hot path; nothing else caches, and if it needs to, that lives in the consuming subsystem, not here.
- ORM choice or usage patterns. The Python code uses SQLAlchemy Core with Alembic migrations; nothing in this spec depends on the ORM continuing to be SQLAlchemy.
- Backup and restore mechanics. Infrastructure spec (subsystem 7 §9) handles that; this layer describes what needs backing up, not how.
- Multi-region replication. Single-region MVP; multi-region is a v2 concern outside this document.

## 3. Design principles

**Every schema constraint is enforced by the database.** If the constraint is important enough to name in this spec, it is important enough for the database to reject violations. Application-code enforcement of what the schema itself could enforce is a defect this document treats as spec debt. Exceptions are named and justified.

**Every state transition on a persistent row is traceable.** `created_at` and `updated_at` on every table; `updated_at` maintained by a trigger, not by application code. Row-level history for anything mutable and audit-relevant.

**Every mutation is auditable.** No table stores an aggregate that cannot be reconstructed from the events that produced it. Materialized aggregates are cache; the primary record is the event stream.

**No silent fallback attribution.** When a row records a fact that is attributed to a user, a session, or a source, that attribution is required and enforced by NOT NULL where possible. Where the attribution can legitimately be system-level (background jobs, retention operations), the attribution goes to an explicit system account with a distinct id, not to `NULL` and not to the last-known user. This is the schema-level embodiment of the ingestion §13 provenance invariant.

**Every obligation named in this spec names what performs it.** Retention policies name their scheduler. Constraint checks name their enforcement point. Emission paths — for state transitions on rows that trigger downstream behavior — name their code path. The class of defect where an obligation exists at every declaration site and has no execution site is one this project has surfaced five times; it is a class the spec discipline is designed to prevent, not a class the code should be trusted to catch.

**Migrations are reversible.** Every migration has a `downgrade()` that undoes what `upgrade()` did. Reversibility is verified by CI on every migration change: apply, verify, downgrade, verify empty, re-apply. Migrations that cannot be reversed (rare, and named) require explicit reviewer approval and a stated recovery procedure.

**Retention is data protection, not data disposal.** The default posture is that data persists unless a retention rule explicitly requires it deleted. Every retention rule has a stated business or legal reason. Erasure requests are honored to completion; the mechanism that ensures completion is named, not assumed.

**The schema is domain-organized, not implementation-organized.** Tables live in the schema section that describes their concern (subjects, sources, sessions, evaluation, retention operations), not in the section that describes the code module that reads or writes them. Consumers query across sections; the section boundaries are for human comprehension, not for access-path enforcement.

**Enums are versioned by addition, not by mutation.** Adding a value to an enum is safe. Removing or renaming a value requires a coordinated migration, downtime consideration, and reviewer sign-off. Every enum in this spec has a stability contract: values may be added; the meaning of existing values does not change.

## 4. Technology stack

**Database.** PostgreSQL 16.15 (matching what CI runs against). Older 16.x versions probably work; 17+ has not been tested and may break assumptions about specific pgvector behavior.

**Extensions.** `pgvector` 0.8.6 (for vector similarity search). `pg_trgm` (for trigram fuzzy match on journal search). `pgcrypto` (for `gen_random_uuid()` when needed; though v7 UUIDs from application code are the default per data layer §5.3).

**Migration tool.** Alembic (via SQLAlchemy). Migration files under `backend/alembic/versions/`.

**Application access.** psycopg 3.x with binary adapter registration for pgvector (this was the 14× performance finding from the retrieval build — see §9). Connection pooling via psycopg's built-in pool.

**Naming convention.** Alembic naming convention applied consistently, so constraint and index names are generated predictably. Any code that names constraints explicitly must use the naming convention output, not a hand-typed name — the ingestion 0010 downgrade and the evaluation 0011 both hit this and the CI check that fires on double-prefixed constraint names now catches it before commit.

## 5. Migration strategy

### 5.1 Migration files

Numbered sequentially. Applied migrations 0001–0009 shipped in v1.1; v1.2 adds 0010 (ingestion), 0011 (evaluation), and 0012 (infrastructure). Every migration file has:

- `revision` — the migration id
- `down_revision` — the previous migration id (linear history for MVP; no branching)
- `upgrade()` — applies the change
- `downgrade()` — reverses the change

### 5.2 CI enforcement

**Reversibility.** Every migration change triggers a CI job that runs apply → verify → downgrade → verify empty → re-apply. Passes only if all three pass.

**Naming.** A CI check verifies that no constraint or index name is double-prefixed (`ck_table_ck_table_...`). The evaluation build's migration 0011 originally violated this; the check was added as a result and now catches the pattern before merge.

**Schema convention.** Money columns are `NUMERIC(12, 6)`, not `REAL`. Timestamps are `TIMESTAMPTZ`, not `TIMESTAMP`. UUIDs are the primary key type; sequential integer ids are prohibited. A CI check enforces these.

**Idempotency.** Applying the current migration set to a fresh database, then applying it again with the current head, is a no-op. Verified in CI.

### 5.3 UUID strategy

**Application-generated UUIDv7** via `uuid7()` helper. Time-ordered so index locality is good for insert-heavy tables (session_turns, agent_traces, cost_ledger). The `id` column on every table is `UUID PRIMARY KEY DEFAULT uuid_generate_v7()`.

**Rationale for v7 over v4.** v7 has embedded timestamp so range queries by insert order are index-friendly. v4 is uniformly random so an index on `id` has terrible locality for the hot insert path. The performance difference at the scale of `session_turns` (millions of rows over a year) is measurable.

### 5.4 Downgrade edge cases

**Enum value removals** cannot be done in Postgres. If a downgrade needs to remove an enum value that was added, the migration must rebuild the enum: drop dependent columns, drop the enum, recreate the enum without the value, recreate the columns, restore data. Migration 0011 does this for `portfolio_item_kind` in its downgrade path; the pattern is documented in `backend/alembic/README.md`.

**Data loss on downgrade** is prohibited for retention-sensitive tables (cost_ledger, portfolio_items, mastery_events). Migrations that would drop rows from these tables on downgrade require the reviewer to acknowledge the loss explicitly.

## 6. Schema

Organized by domain. Every table lives in one section; foreign keys cross sections freely.

### 6.0 Enums and shared types

```sql
-- User and access: role is TEXT + CHECK, not an enum. Migration 0001 creates
-- no user_role type. The three values are ('learner', 'reviewer', 'admin');
-- 'admin' is unused at MVP, and there is no 'system' role — the system account
-- is a reserved id, not a role (see §6.1).

-- Sessions
CREATE TYPE session_mode AS ENUM (
  'lecture', 'tutorial', 'lab', 'office_hours', 'review',
  'summative_assessment'
);
CREATE TYPE session_status AS ENUM ('active', 'closed', 'error');
CREATE TYPE session_end_reason AS ENUM (
  'learner_initiated', 'idle_timeout', 'budget_cap', 'system_error'
);

-- Agents (matches subsystem 2 §5)
CREATE TYPE agent_identity AS ENUM (
  'lecturer', 'tutor', 'evaluator', 'curator',
  'confusion_tracker', 'reviewer', 'orchestrator'
);

-- Content
CREATE TYPE artifact_kind AS ENUM (
  'lecture_segment', 'tutorial_exchange', 'worked_example',
  'comprehension_check', 'practice_problem', 'assessment_response',
  'summary', 'study_guide', 'primitive_output', 'recap'
);
CREATE TYPE stance AS ENUM (
  'default', 'formal', 'intuitive', 'applied', 'historical'
);

-- Chunks (added v1.2)
CREATE TYPE chunk_type AS ENUM (
  'body', 'heading', 'caption', 'reference', 'table', 'equation'
);

-- Ingestion
CREATE TYPE ingestion_job_kind AS ENUM (
  'extract_text', 'chunk', 'embed', 'normalize'
);
-- v1.2: 'normalize' added — the spec previously omitted it despite
-- §6.4 of ingestion spec triggering a job with that kind
CREATE TYPE ingestion_flag_source AS ENUM (
  'extractor_failure', 'normalizer_warning', 'embedding_failure',
  'chunk_ambiguous_type', 'license_pending', 'license_conflict',
  'concept_source_conflict', 'graph_validation_error',
  'rubric_validation_error'
);

-- Licensing
CREATE TYPE license_kind AS ENUM (
  'public_domain', 'creative_commons_by', 'creative_commons_by_sa',
  'creative_commons_by_nc', 'permission_granted', 'user_uploaded',
  'proprietary_licensed'
);

-- Reviews (content and ingestion queues)
CREATE TYPE review_status AS ENUM ('pending', 'resolved', 'dismissed');

-- Mastery and portfolio
CREATE TYPE mastery_event_kind AS ENUM (
  'lecture_check_correct', 'lecture_check_incorrect',
  'practice_correct', 'practice_partial', 'practice_incorrect',
  'assessment_correct', 'assessment_partial', 'assessment_incorrect',
  'review_correct', 'review_incorrect', 'decay_applied'
);
CREATE TYPE portfolio_item_kind AS ENUM (
  'lecture_completion', 'tutorial_exchange', 'lab_submission',
  'reflection', 'assessment_response', 'assessment_pass',
  'subject_completion'
);
-- v1.2: 'assessment_pass' and 'subject_completion' added — the
-- existing six were all learner work; credentialing needed new values

-- Journal
CREATE TYPE journal_entry_status AS ENUM ('open', 'partial', 'resolved', 'archived');

-- Evaluation (added v1.2)
CREATE TYPE golden_dataset_kind AS ENUM (
  'agent_output', 'retrieval_quality', 'grading_calibration', 'content_quality'
);
```

### 6.1 Users and roles

```sql
CREATE TABLE users (
  id                 UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  email              CITEXT NOT NULL,
  display_name       TEXT NOT NULL,
  password_hash      TEXT,                      -- NULL when OAuth-only
  auth_provider      TEXT NOT NULL DEFAULT 'local'
                       CHECK (auth_provider IN ('local', 'google', 'github')),
  role               TEXT NOT NULL DEFAULT 'learner'
                       CHECK (role IN ('learner', 'reviewer', 'admin')),
  locale             TEXT NOT NULL DEFAULT 'en-CA',
  timezone           TEXT NOT NULL DEFAULT 'America/Toronto',
  deleted_at         TIMESTAMPTZ,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_users_email_active ON users (email)
  WHERE deleted_at IS NULL;
CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

**The system user.** Activity not attributable to a real learner — background jobs, scheduled runs, evaluation runs — is booked to a single reserved account, not marked with a flag. There is no `is_system` column: the account is the fixed id `00000000-0000-7000-8000-000000000001` (`studium.models.identity.SYSTEM_USER_ID`), seeded by migration 0008 with no password and no auth sessions, so it cannot be logged into. The evaluation subsystem's runs attribute cost to it (per subsystem 6 §15); this is what closed SD9, with the finding that its premise was false. See `DIVERGENCES.md` V3.

An id rather than a boolean because every table that needs the distinction already carries `user_id`. A flag would have to be joined to and kept consistent with the id that already answers the question, and "unattributable cost" cannot be booked to NULL — §6.12 reserves a NULL owner for post-erasure aggregates, so overloading it would make an erased learner's spend indistinguishable from shared infrastructure cost.

**`preferences` is not on this table.** It lives on `user_profiles` (§6.1's one-to-one split), together with `background_notes`, `stated_goals` and `accessibility` — profile data is larger, mutates more often, and is not needed on every auth check.

**Soft delete flow** (see §10 for the full retention picture):
1. Erasure request sets `deleted_at = NOW()`. The learner's own identity columns are *not* anonymized; what is anonymized are the retained aggregates listed in §10.2.
2. After the 30-day window, the retention worker (§10, wired to the scheduler infrastructure in subsystem 7 §12) hard-deletes the row.

**Right-to-erasure enforcement.** The dispute window is *computed, not stored* — there is no `hard_delete_after` column. `privacy.purge_expired_soft_deletes` deletes every row whose `deleted_at` is older than `DISPUTE_WINDOW_DAYS` (30), and the infrastructure subsystem's scheduler runs it daily as the `erasure_purge` stage of the nightly pass (subsystem 7 §12.1). That wiring is what closes the erasure gap the infrastructure build surfaced.

The cost of one column instead of two is that the window is global: extending it for a single disputed account means a code change, not a row update. Nothing at MVP needs a per-row window, and a stored `hard_delete_after` that no code path ever varies is a column that can silently disagree with the constant. Revisit if a legal hold ever has to apply to one learner.

**One partial unique index, not a plain `UNIQUE` plus a filtered one.** `idx_users_email_active` is unique on `email` `WHERE deleted_at IS NULL`. A soft-deleted row drops out of it, so the address is free for immediate re-registration while the row itself — and the identity needed to reverse an accidental erasure — stays intact for the window. See `DIVERGENCES.md` V6.

### 6.2 Subjects and concepts

```sql
CREATE TYPE subject_status AS ENUM ('draft', 'active', 'archived');

CREATE TABLE subjects (
  id                 UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  slug               TEXT NOT NULL UNIQUE,
  title              TEXT NOT NULL,
  version            INTEGER NOT NULL DEFAULT 1,
  short_description  TEXT NOT NULL,
  long_description   TEXT NOT NULL,
  status             subject_status NOT NULL DEFAULT 'draft',
  published_at       TIMESTAMPTZ,
  metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_subjects_active ON subjects (status) WHERE status = 'active';
CREATE TRIGGER trg_subjects_updated_at BEFORE UPDATE ON subjects
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE concepts (
  id                 UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  subject_id         UUID NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
  slug               TEXT NOT NULL,
  title              TEXT NOT NULL,
  depth              SMALLINT NOT NULL CHECK (depth >= 1 AND depth <= 6),
  is_load_bearing    BOOLEAN NOT NULL DEFAULT FALSE,
  estimated_minutes  SMALLINT,
  module             TEXT,
  long_description   TEXT NOT NULL,
  metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (subject_id, slug)
);

CREATE INDEX idx_concepts_subject ON concepts (subject_id);
CREATE INDEX idx_concepts_load_bearing ON concepts (subject_id) WHERE is_load_bearing = TRUE;
CREATE TRIGGER trg_concepts_updated_at BEFORE UPDATE ON concepts
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TYPE concept_edge_kind AS ENUM ('prerequisite', 'related', 'contrast', 'application');

CREATE TABLE concept_edges (
  id                 UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  from_concept_id    UUID NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  to_concept_id      UUID NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  kind               concept_edge_kind NOT NULL,
  strength           SMALLINT NOT NULL DEFAULT 3 CHECK (strength BETWEEN 1 AND 5),
  notes              TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (from_concept_id != to_concept_id),
  UNIQUE (from_concept_id, to_concept_id, kind)
);

CREATE INDEX idx_concept_edges_from ON concept_edges (from_concept_id);
CREATE INDEX idx_concept_edges_to ON concept_edges (to_concept_id);
```

**Edge semantics.** `prerequisite` with `from_concept_id = X, to_concept_id = Y` means X is required before Y. The retrieval build originally walked this edge the wrong direction (S1); the semantics are now explicit in this comment and both directions are checked by tests. `related`, `contrast`, and `application` are symmetric in intent but stored directionally so consumers can chose to render one direction only if that's clearer.

```sql
CREATE TABLE concept_sources (
  id                 UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  concept_id         UUID NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  source_chunk_id    UUID NOT NULL REFERENCES source_chunks(id) ON DELETE RESTRICT,
  role               TEXT NOT NULL,
  weight             SMALLINT NOT NULL DEFAULT 3 CHECK (weight BETWEEN 1 AND 5),
  notes              TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (concept_id, source_chunk_id)
);

CREATE INDEX idx_concept_sources_concept ON concept_sources (concept_id);
CREATE INDEX idx_concept_sources_chunk ON concept_sources (source_chunk_id);
CREATE TRIGGER trg_concept_sources_updated_at BEFORE UPDATE ON concept_sources
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

**`role`** is one of: `canonical_definition`, `primary_exposition`, `worked_example`, `contrast`, `application`, `reference`, `history`. Free text for extensibility; the enumerated values are convention, not database-enforced.

### 6.3 Sources

```sql
CREATE TABLE sources (
  id                 UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  subject_id         UUID REFERENCES subjects(id) ON DELETE CASCADE,
  slug               TEXT NOT NULL,
  title              TEXT NOT NULL,
  authors            TEXT[] NOT NULL DEFAULT '{}',
  publication_year   INTEGER,
  license            license_kind NOT NULL DEFAULT 'permission_granted',
  license_notes      TEXT,
  storage_path       TEXT NOT NULL,
  content_sha256     TEXT NOT NULL,
  page_count         INTEGER,
  status             TEXT NOT NULL DEFAULT 'draft',
  uploaded_by        UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  extractor_version  TEXT,
  normalizer_version TEXT,
  metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (subject_id, content_sha256)
);

CREATE INDEX idx_sources_subject ON sources (subject_id);
CREATE INDEX idx_sources_status ON sources (status);
CREATE TRIGGER trg_sources_updated_at BEFORE UPDATE ON sources
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

**`extractor_version` and `normalizer_version`** (v1.2) record which code produced the text on this source. Format: `"pdfplumber/0.11.4"`, `"studium.normalize/1.0"`. Enable re-extraction across the corpus when the extractor changes without losing which sources were processed under which version. Both nullable because pre-migration sources may have no recorded extractor.

**`uploaded_by` is NOT NULL** (v1.2 tightened this — it was nullable in v1.1). This is the ingestion §13 provenance invariant enforced at the schema level: no source exists without a known uploader, even if that uploader is the system user for CLI-triggered uploads.

### 6.4 Source chunks and embeddings

```sql
CREATE TABLE source_chunks (
  id                     UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  source_id              UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  chunk_index            INTEGER NOT NULL,
  text                   TEXT NOT NULL,
  token_count            INTEGER NOT NULL,
  page_start             INTEGER,
  page_end               INTEGER,
  section_path           JSONB NOT NULL DEFAULT '[]'::jsonb,
  chunk_type             chunk_type NOT NULL DEFAULT 'body',
  extraction_confidence  REAL NOT NULL DEFAULT 1.0
    CHECK (extraction_confidence >= 0.0 AND extraction_confidence <= 1.0),
  superseded_at          TIMESTAMPTZ,
  tsvector_text          TSVECTOR
    GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (source_id, chunk_index)
);

CREATE INDEX idx_source_chunks_source ON source_chunks (source_id);
CREATE INDEX idx_source_chunks_tsvector ON source_chunks USING GIN (tsvector_text);
CREATE INDEX idx_source_chunks_low_confidence ON source_chunks (source_id, extraction_confidence)
  WHERE extraction_confidence < 0.7;
CREATE INDEX idx_source_chunks_active ON source_chunks (source_id) WHERE superseded_at IS NULL;
```

**`chunk_type`** (v1.2) categorizes what kind of content a chunk contains. Retrieval reweights on this — a query for a definition weights `body` and `heading` chunks over `reference` and `caption`. The chunker classifies; the reviewer can override via the ingestion review queue when the classification is ambiguous.

**`tsvector_text`** (v1.2) is a `STORED` generated column indexed with GIN. Postgres tsvector for keyword search. Language is hardcoded to `english` for MVP; multi-language support is a v2 concern. The retrieval build's ligature bug (U+FB01 rendering as one term) was caused by extractor output that this index then dutifully preserved as garbage; the normalizer's ligature substitution (ingestion spec §8) is what makes this index useful.

**`extraction_confidence`** (v1.2) is a float 0–1 recording the extractor's confidence. Populated by extractors that expose confidence (marker, unstructured); default 1.0 for extractors that don't (pdfplumber). The partial index below 0.7 supports the reviewer query "show me low-confidence chunks in this source" without scanning the full table.

**`superseded_at`** (v1.2) supports the re-extraction workflow. When a source is re-extracted under a new extractor version, existing chunks are marked superseded (not deleted); new chunks get new ids. Retrieval filters superseded chunks out of new searches (retrieval subsystem §7); citation resolution continues to serve them so historical citations continue to work.

```sql
CREATE TABLE source_chunk_embeddings (
  id                 UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  source_chunk_id    UUID NOT NULL UNIQUE REFERENCES source_chunks(id) ON DELETE CASCADE,
  provider           TEXT NOT NULL,
  model              TEXT NOT NULL,
  dimension          INTEGER NOT NULL,
  embedding          vector(1024),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_source_chunk_embeddings_hnsw
  ON source_chunk_embeddings USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 200);
```

**Provider isolation.** The `provider` and `model` columns record what produced the embedding. Multiple providers can coexist during a swap window; retrieval queries by provider when needed. The vector dimension is fixed at 1024 for Voyage-3; changing it requires migration.

**HNSW index parameters** are the pgvector 0.8+ defaults with `m = 16, ef_construction = 200`. The retrieval build measured recall @ 6 to remain acceptable at these settings up to ~150k rows; if index quality degrades at scale, tuning happens then.

### 6.5 Learner state

```sql
CREATE TABLE learner_subjects (
  id                        UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  learner_id                UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  subject_id                UUID NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
  enrolled_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  current_focus_concept_id  UUID REFERENCES concepts(id) ON DELETE SET NULL,
  syllabus_plan             JSONB NOT NULL DEFAULT '{}'::jsonb,
  goals                     TEXT,
  UNIQUE (learner_id, subject_id)
);

CREATE INDEX idx_learner_subjects_learner ON learner_subjects (learner_id);

CREATE TABLE concept_mastery (
  id                    UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  learner_subject_id    UUID NOT NULL REFERENCES learner_subjects(id) ON DELETE CASCADE,
  concept_id            UUID NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  bkt_p_learn           REAL NOT NULL DEFAULT 0.1
    CHECK (bkt_p_learn >= 0.0 AND bkt_p_learn <= 1.0),
  bkt_p_guess           REAL NOT NULL DEFAULT 0.2
    CHECK (bkt_p_guess >= 0.0 AND bkt_p_guess <= 1.0),
  bkt_p_slip            REAL NOT NULL DEFAULT 0.1
    CHECK (bkt_p_slip >= 0.0 AND bkt_p_slip <= 1.0),
  mastery_estimate      REAL NOT NULL DEFAULT 0.0
    CHECK (mastery_estimate >= 0.0 AND mastery_estimate <= 1.0),
  last_touched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_decayed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  event_count           INTEGER NOT NULL DEFAULT 0,
  UNIQUE (learner_subject_id, concept_id)
);

CREATE INDEX idx_concept_mastery_focus ON concept_mastery (learner_subject_id, mastery_estimate);

CREATE TABLE mastery_events (
  id                    UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  learner_subject_id    UUID NOT NULL REFERENCES learner_subjects(id) ON DELETE CASCADE,
  concept_id            UUID NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  session_id            UUID REFERENCES learning_sessions(id) ON DELETE SET NULL,
  session_turn_id       UUID REFERENCES session_turns(id) ON DELETE SET NULL,
  kind                  mastery_event_kind NOT NULL,
  weight                REAL NOT NULL DEFAULT 1.0,
  notes                 TEXT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_mastery_events_concept ON mastery_events (learner_subject_id, concept_id, created_at DESC);
CREATE INDEX idx_mastery_events_session ON mastery_events (session_id) WHERE session_id IS NOT NULL;
```

**Materialized-vs-derived split.** `concept_mastery.mastery_estimate` is a cache. The primary record is the `mastery_events` stream. Full reconstruction from events is possible; the cached estimate is what the Curator queries because reconstructing on every read would be expensive.

**Decay handling.** `cost_rollup.refresh_decay` (subsystem 7's scheduled job that computes decayed mastery estimates) is the scheduler-driven update. `graph.unlock_status` computes decay in SQL at query time as a defense-in-depth, so gating remains correct even if the cache is stale. The infrastructure build found that the scheduler wasn't wired; wiring it is what turned the cache from "sometimes stale" to "generally fresh."

### 6.6 Sessions, turns, and agent traces

```sql
CREATE TABLE learning_sessions (
  id                     UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  learner_subject_id     UUID NOT NULL REFERENCES learner_subjects(id) ON DELETE CASCADE,
  mode                   session_mode NOT NULL,
  status                 session_status NOT NULL DEFAULT 'active',
  focus_concept_id       UUID REFERENCES concepts(id) ON DELETE SET NULL,
  target_duration_minutes INTEGER NOT NULL DEFAULT 90,
  started_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ended_at               TIMESTAMPTZ,
  end_reason             session_end_reason,
  metadata               JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_learning_sessions_learner ON learning_sessions (learner_subject_id, started_at DESC);
CREATE INDEX idx_learning_sessions_active ON learning_sessions (learner_subject_id) WHERE status = 'active';

CREATE TABLE session_turns (
  id                UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  session_id        UUID NOT NULL REFERENCES learning_sessions(id) ON DELETE CASCADE,
  turn_index        INTEGER NOT NULL,
  actor             agent_identity NOT NULL,
  kind              TEXT NOT NULL,
  concept_id        UUID REFERENCES concepts(id) ON DELETE SET NULL,
  artifact_id       UUID REFERENCES content_artifacts(id) ON DELETE SET NULL,
  utterance         TEXT,
  metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (session_id, turn_index)
);

CREATE INDEX idx_session_turns_session ON session_turns (session_id, turn_index);
CREATE INDEX idx_session_turns_actor ON session_turns (actor, created_at DESC);
```

**`artifact_id` population** was the fix delivered in agent runtime v1.0.1's `AppliedEffects` change. The column has existed since v1.1; nothing had ever populated it, which is the pattern §3 exists to prevent. The runtime now populates it in the same transaction as the artifact write; the AppliedEffects return shape enforces this.

```sql
CREATE TABLE agent_traces (
  id                    UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  session_id            UUID REFERENCES learning_sessions(id) ON DELETE CASCADE,
  session_turn_id       UUID REFERENCES session_turns(id) ON DELETE CASCADE,
  exchange_index        INTEGER NOT NULL DEFAULT 0,
  agent                 agent_identity NOT NULL,
  kind                  TEXT NOT NULL,
  model                 TEXT NOT NULL,
  system_prompt_hash    TEXT NOT NULL,
  input_tokens          INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens   INTEGER NOT NULL DEFAULT 0,
  output_tokens         INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens      INTEGER NOT NULL DEFAULT 0,
  cost_usd              NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
  latency_ms            INTEGER,
  cache_hit             BOOLEAN NOT NULL DEFAULT FALSE,
  error                 TEXT,
  metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_agent_traces_session ON agent_traces (session_id, created_at);
CREATE INDEX idx_agent_traces_turn ON agent_traces (session_turn_id, exchange_index);
CREATE INDEX idx_agent_traces_agent ON agent_traces (agent, created_at DESC);
CREATE INDEX idx_agent_traces_errors ON agent_traces (created_at DESC) WHERE error IS NOT NULL;
```

**`exchange_index`** (v1.2, R4) preserves ordering within a turn when the agent runtime produces multiple exchanges. A turn may produce a stream of retrieval calls followed by a generation call followed by an effect-writing sub-turn; each gets its own `agent_traces` row with the same `session_turn_id` and monotonic `exchange_index`. Before v1.2 nothing ordered these and reconstructing a trace hierarchy was ambiguous.

### 6.7 Journal (confusion tracking)

```sql
CREATE TABLE journal_entries (
  id                    UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  learner_subject_id    UUID NOT NULL REFERENCES learner_subjects(id) ON DELETE CASCADE,
  concept_id            UUID REFERENCES concepts(id) ON DELETE SET NULL,
  status                journal_entry_status NOT NULL DEFAULT 'open',
  summary               TEXT NOT NULL,
  hypothesis            TEXT,
  learner_note          TEXT,
  first_flagged_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_touched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at           TIMESTAMPTZ,
  archived_at           TIMESTAMPTZ,
  metadata              JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_journal_open ON journal_entries (learner_subject_id, last_touched_at DESC)
  WHERE status IN ('open', 'partial');
CREATE INDEX idx_journal_concept ON journal_entries (learner_subject_id, concept_id)
  WHERE concept_id IS NOT NULL;
CREATE INDEX idx_journal_search ON journal_entries USING GIN (
  (setweight(to_tsvector('english', COALESCE(summary, '')), 'A') ||
   setweight(to_tsvector('english', COALESCE(learner_note, '')), 'B'))
);

CREATE TABLE journal_events (
  id                    UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  entry_id              UUID NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
  session_id            UUID REFERENCES learning_sessions(id) ON DELETE SET NULL,
  event_kind            TEXT NOT NULL,
  actor                 agent_identity,
  notes                 TEXT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_journal_events_entry ON journal_events (entry_id, created_at);
```

**Search index** (v1.2 refinement) uses `setweight` to prioritize summary matches over learner_note matches. Basic substring search suffices at MVP scale; the weighted GIN index is future-proofing.

### 6.8 Assessments

```sql
CREATE TABLE assessments (
  id                 UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  subject_id         UUID NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
  slug               TEXT NOT NULL,
  title              TEXT NOT NULL,
  time_limit_minutes INTEGER,
  passing_threshold  REAL NOT NULL DEFAULT 0.7
    CHECK (passing_threshold >= 0.0 AND passing_threshold <= 1.0),
  problem_pool       JSONB NOT NULL,
  active             BOOLEAN NOT NULL DEFAULT FALSE,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (subject_id, slug)
);

CREATE INDEX idx_assessments_subject ON assessments (subject_id) WHERE active = TRUE;

CREATE TABLE assessment_attempts (
  id                    UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  learner_subject_id    UUID NOT NULL REFERENCES learner_subjects(id) ON DELETE CASCADE,
  assessment_id         UUID NOT NULL REFERENCES assessments(id) ON DELETE RESTRICT,
  session_id            UUID NOT NULL REFERENCES learning_sessions(id) ON DELETE CASCADE,
  status                TEXT NOT NULL DEFAULT 'in_progress',
  score                 REAL,
  passed                BOOLEAN,
  started_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  submitted_at          TIMESTAMPTZ,
  time_limit_expires_at TIMESTAMPTZ,
  problems_snapshot     JSONB NOT NULL,
  metadata              JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_assessment_attempts_learner ON assessment_attempts (learner_subject_id, started_at DESC);
CREATE INDEX idx_assessment_attempts_pending ON assessment_attempts (learner_subject_id) WHERE status = 'in_progress';

CREATE TABLE assessment_responses (
  id                    UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  attempt_id            UUID NOT NULL REFERENCES assessment_attempts(id) ON DELETE CASCADE,
  problem_id            TEXT NOT NULL,
  learner_response      TEXT NOT NULL,
  score                 REAL,
  rubric_breakdown      JSONB,
  feedback              TEXT,
  submitted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  graded_at             TIMESTAMPTZ,
  UNIQUE (attempt_id, problem_id)
);

CREATE INDEX idx_assessment_responses_attempt ON assessment_responses (attempt_id);
```

**`problems_snapshot`** captures the specific problems drawn from the pool at attempt start. This is what makes retakes verifiable — the retake system can check that the new attempt's problems don't overlap with recent snapshots.

### 6.9 Reviews (FSRS cards)

```sql
CREATE TABLE review_cards (
  id                    UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  learner_subject_id    UUID NOT NULL REFERENCES learner_subjects(id) ON DELETE CASCADE,
  concept_id            UUID NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  fsrs_state            JSONB NOT NULL,
  stability             REAL NOT NULL DEFAULT 1.0,
  difficulty            REAL NOT NULL DEFAULT 5.0
    CHECK (difficulty >= 1.0 AND difficulty <= 10.0),
  due_at                TIMESTAMPTZ NOT NULL,
  last_reviewed_at      TIMESTAMPTZ,
  review_count          INTEGER NOT NULL DEFAULT 0,
  suspended_at          TIMESTAMPTZ,
  archived_at           TIMESTAMPTZ,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (learner_subject_id, concept_id)
);

CREATE INDEX idx_review_cards_due ON review_cards (learner_subject_id, due_at)
  WHERE suspended_at IS NULL AND archived_at IS NULL;
CREATE INDEX idx_review_cards_stale ON review_cards (last_reviewed_at)
  WHERE archived_at IS NULL;

CREATE TABLE review_events (
  id                    UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  card_id               UUID NOT NULL REFERENCES review_cards(id) ON DELETE CASCADE,
  session_id            UUID REFERENCES learning_sessions(id) ON DELETE SET NULL,
  rating                SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 4),
  fsrs_state_before     JSONB NOT NULL,
  fsrs_state_after      JSONB NOT NULL,
  stability_before      REAL NOT NULL,
  stability_after       REAL NOT NULL,
  reviewed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_review_events_card ON review_events (card_id, reviewed_at DESC);
```

**Retention behavior for review cards** (V15 clarification): archived cards are retained indefinitely for historical review-history reconstruction. Suspended cards (temporarily disabled by learner) are retained indefinitely. Only fully deleted cards (learner explicitly deletes) are removed by retention; deletion is rare and always learner-initiated.

### 6.10 Portfolio and signing keys

```sql
CREATE TABLE portfolio_items (
  id                    UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  learner_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  subject_id            UUID REFERENCES subjects(id) ON DELETE SET NULL,
  kind                  portfolio_item_kind NOT NULL,
  session_turn_id       UUID REFERENCES session_turns(id) ON DELETE SET NULL,
  assessment_attempt_id UUID REFERENCES assessment_attempts(id) ON DELETE SET NULL,
  title                 TEXT NOT NULL,
  payload               JSONB NOT NULL,
  signature             TEXT,
  signing_key_id        UUID REFERENCES signing_keys(id) ON DELETE RESTRICT,
  is_credential         BOOLEAN NOT NULL DEFAULT FALSE,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_portfolio_items_learner ON portfolio_items (learner_id, created_at DESC);
CREATE INDEX idx_portfolio_items_credentials ON portfolio_items (learner_id) WHERE is_credential = TRUE;
CREATE INDEX idx_portfolio_items_verify ON portfolio_items (id) WHERE is_credential = TRUE;
```

**`is_credential`** (v1.2, distinguishes learner work items from formal credentials, per evaluation §12): work items (lecture completion, tutorial exchanges, lab submissions, reflections) are stored unsigned. Credentials (assessment_pass, subject_completion) are stored signed. The public verifier endpoint filters by `is_credential = TRUE` so a UUID probe cannot expose learner work.

**Missing signature manifest was the E9 defect** the evaluation build surfaced. The runtime now builds a canonical signature manifest for every credential kind before insert; NOT NULL on `signature` for credential rows is enforced by a trigger (defined below in §7) because a partial constraint on `signature` when `is_credential = TRUE` isn't cleanly expressible in Postgres.

```sql
CREATE TABLE signing_keys (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  algorithm       TEXT NOT NULL DEFAULT 'ed25519',
  public_key_pem  TEXT NOT NULL,
  active          BOOLEAN NOT NULL DEFAULT FALSE,
  activated_at    TIMESTAMPTZ,
  deactivated_at  TIMESTAMPTZ,
  compromised_at  TIMESTAMPTZ,
  notes           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_signing_keys_active ON signing_keys (active) WHERE active = TRUE;
CREATE INDEX idx_signing_keys_lookup ON signing_keys (id);
```

**`signing_keys`** (v1.2, migration 0011): the evaluation build discovered v1.1's §6.10 referenced this table but never defined it. Now defined. Only one row may have `active = TRUE` at any time (enforced by the partial unique index). Rotation flips the active row to inactive and marks a new row active in the same transaction.

**`compromised_at`** (v1.2, migration 0012): records when a key was flagged compromised. Verify endpoints include a compromise notice beside the valid signature; the signature still verifies (the math is unchanged) but the notice tells downstream verifiers to apply additional scrutiny to credentials issued during the potentially-compromised window. Retracting the flag is prohibited — a published notice cannot be silently retracted without deceiving verifiers.

### 6.11 Cost ledger and budget caps

```sql
CREATE TABLE cost_ledger (
  id                       UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  user_id                  UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  date                     DATE NOT NULL,
  cost_agent_usd           NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
  cost_ingestion_usd       NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
  cost_evaluation_usd      NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
  cost_content_usd         NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
  cost_storage_usd         NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
  cost_unattributed_usd    NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
  unattributed_count       INTEGER NOT NULL DEFAULT 0,
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, date)
);

CREATE INDEX idx_cost_ledger_user ON cost_ledger (user_id, date DESC);
CREATE INDEX idx_cost_ledger_date ON cost_ledger (date);
CREATE INDEX idx_cost_ledger_unattributed ON cost_ledger (date DESC) WHERE unattributed_count > 0;
```

**`cost_evaluation_usd`** (v1.2, subsystem 6 §15) is a distinct line for evaluation run costs. Attribution is per subsystem 6: regression runs to the triggering user, scheduled runs to the system user. `cost_agent_usd` is real-learner-session cost; keeping them distinct is what makes the "eval is cheap; learner sessions dominate" observation checkable.

**`cost_unattributed_usd` and `unattributed_count`** (v1.2, V2 resolution + SD2 closure): the honest attribution for cost that reaches the ledger without a known responsible user. Two readers: the cost report shows the totals separately; the daily cost alert fires on the *ratio* of unattributed to total exceeding 5% for three consecutive days, rather than on the absolute count. Curator pre-generation legitimately has no session in some cases, so alerting on the absolute count would fire on a productive week of authoring.

**Erasure merge behavior** (V13 fix): when a user is erased, their `cost_ledger` rows are folded into the shared NULL-owner row for each `(day, model)` and the originals deleted. There is no separate `deleted_users_aggregate` table — §6.12 already reserves `user_id IS NULL` for post-erasure aggregates, and `uq_cost_ledger_day` is `NULLS NOT DISTINCT`, so that row *is* the per-day aggregate across every erased user. This preserves total cost history for accounting without preserving per-user attribution after erasure. `privacy.erase_user` performs the fold in the same transaction as the rest of the erasure — at request time, not at hard-delete.

```sql
CREATE TABLE user_budget_caps (
  id                          UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  user_id                     UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
  daily_soft_usd_max          NUMERIC(12, 6) NOT NULL DEFAULT 5.00,
  daily_hard_usd_max          NUMERIC(12, 6) NOT NULL DEFAULT 8.00,
  monthly_soft_usd_max        NUMERIC(12, 6) NOT NULL DEFAULT 100.00,
  monthly_hard_usd_max        NUMERIC(12, 6) NOT NULL DEFAULT 150.00,
  cap_reset_hour_local        SMALLINT NOT NULL DEFAULT 0
    CHECK (cap_reset_hour_local BETWEEN 0 AND 23),
  suspended_at                TIMESTAMPTZ,
  notes                       TEXT,
  updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_user_budget_caps_suspended ON user_budget_caps (user_id)
  WHERE suspended_at IS NOT NULL;
```

**Budget cap columns** (V14, v1.2) formalize what the agent runtime's `BudgetExceededError` gate reads from. Four caps per user: daily soft (warns; not enforced), daily hard (blocks), monthly soft, monthly hard. Soft caps produce warnings in the reviewer dashboard; hard caps produce `BudgetExceededError` at the pre-flight gate.

**System user's caps.** Migration 0008 creates the system user with `daily_hard_usd_max = 1000` and `monthly_hard_usd_max = 10000`. This is what SD9's real half revealed — evaluation runs booking to the system account weren't constrained by learner caps but *were* constrained by the system account's caps, which were generous but not infinite.

### 6.12 Content review queue

```sql
CREATE TABLE content_review_queue (
  id                   UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  artifact_id          UUID REFERENCES content_artifacts(id) ON DELETE CASCADE,
  session_turn_id      UUID REFERENCES session_turns(id) ON DELETE CASCADE,
  flag_source          TEXT NOT NULL,
  reason               TEXT NOT NULL,
  severity             SMALLINT NOT NULL DEFAULT 2 CHECK (severity BETWEEN 1 AND 3),
  status               review_status NOT NULL DEFAULT 'pending',
  assigned_to          UUID REFERENCES users(id) ON DELETE SET NULL,
  resolution_note      TEXT,
  resolved_at          TIMESTAMPTZ,
  payload              JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (artifact_id IS NOT NULL OR session_turn_id IS NOT NULL)
);

CREATE INDEX idx_content_review_pending ON content_review_queue (severity DESC, created_at)
  WHERE status = 'pending';
CREATE INDEX idx_content_review_artifact ON content_review_queue (artifact_id) WHERE artifact_id IS NOT NULL;
CREATE INDEX idx_content_review_turn ON content_review_queue (session_turn_id) WHERE session_turn_id IS NOT NULL;
CREATE TRIGGER trg_content_review_queue_updated_at BEFORE UPDATE ON content_review_queue
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

**The CHECK requires either an artifact_id or session_turn_id.** This is the constraint the retrieval build's S3 revealed as blocking the ingestion path that has neither. The v1.2 resolution: ingestion writes to a separate table (`ingestion_review_queue`, §6.13), not to this one. The constraint stays as spec'd — this queue is specifically for content review (generated artifacts and turns), and the separation from ingestion review is architecturally correct.

### 6.13 Ingestion jobs and review queue

```sql
CREATE TABLE ingestion_jobs (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  source_id       UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  kind            ingestion_job_kind NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending',
  attempt_count   INTEGER NOT NULL DEFAULT 0,
  payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
  error           TEXT,
  started_at      TIMESTAMPTZ,
  completed_at    TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_ingestion_jobs_pending ON ingestion_jobs (created_at)
  WHERE status = 'pending';
CREATE INDEX idx_ingestion_jobs_source ON ingestion_jobs (source_id, created_at DESC);
CREATE TRIGGER trg_ingestion_jobs_updated_at BEFORE UPDATE ON ingestion_jobs
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

**`ingestion_job_kind` includes `'normalize'`** (v1.2, migration 0010): the ingestion spec §6.4 triggers a normalize job; v1.1's enum lacked the value. This is one of the "spec-code disagrees because I under-specified" items the ingestion build surfaced.

```sql
CREATE TABLE ingestion_review_queue (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  source_id       UUID REFERENCES sources(id) ON DELETE CASCADE,
  source_chunk_id UUID REFERENCES source_chunks(id) ON DELETE CASCADE,
  subject_id      UUID REFERENCES subjects(id) ON DELETE CASCADE,
  concept_id      UUID REFERENCES concepts(id) ON DELETE CASCADE,
  flag_source     ingestion_flag_source NOT NULL,
  reason          TEXT NOT NULL,
  severity        SMALLINT NOT NULL DEFAULT 2 CHECK (severity BETWEEN 1 AND 3),
  status          review_status NOT NULL DEFAULT 'pending',
  assigned_to     UUID REFERENCES users(id) ON DELETE SET NULL,
  resolution_note TEXT,
  resolved_at     TIMESTAMPTZ,
  payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (
    source_id IS NOT NULL OR
    source_chunk_id IS NOT NULL OR
    subject_id IS NOT NULL OR
    concept_id IS NOT NULL
  )
);

CREATE INDEX idx_ingestion_queue_pending
  ON ingestion_review_queue (severity DESC, created_at)
  WHERE status = 'pending';
CREATE INDEX idx_ingestion_queue_assigned
  ON ingestion_review_queue (assigned_to, status)
  WHERE assigned_to IS NOT NULL;
CREATE INDEX idx_ingestion_queue_source
  ON ingestion_review_queue (source_id) WHERE source_id IS NOT NULL;
CREATE INDEX idx_ingestion_queue_chunk
  ON ingestion_review_queue (source_chunk_id) WHERE source_chunk_id IS NOT NULL;
CREATE INDEX idx_ingestion_queue_subject
  ON ingestion_review_queue (subject_id) WHERE subject_id IS NOT NULL;
CREATE INDEX idx_ingestion_queue_concept
  ON ingestion_review_queue (concept_id) WHERE concept_id IS NOT NULL;
CREATE TRIGGER trg_ingestion_queue_updated_at BEFORE UPDATE ON ingestion_review_queue
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

**Four target columns, four partial indexes.** The ingestion build discovered that omitting the indexes on cascade-target FKs made a source delete sequential-scan the queue. All four are indexed now.

### 6.14 Golden datasets and evaluation

New domain (v1.2, migration 0011). Full detail in the evaluation subsystem spec §5 and §7; the schema definitions below are authoritative.

```sql
CREATE TABLE golden_datasets (
  id                     UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  slug                   TEXT NOT NULL UNIQUE,
  kind                   golden_dataset_kind NOT NULL,
  agent                  agent_identity,
  version                INTEGER NOT NULL DEFAULT 1,
  description            TEXT NOT NULL,
  active                 BOOLEAN NOT NULL DEFAULT TRUE,
  entry_count            INTEGER NOT NULL DEFAULT 0,
  regression_tolerance   JSONB NOT NULL DEFAULT '{"aggregate_score_drop_max": 0.05, "per_entry_failure_max": 2}'::jsonb,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_golden_datasets_kind ON golden_datasets (kind, active);
CREATE TRIGGER trg_golden_datasets_updated_at BEFORE UPDATE ON golden_datasets
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE golden_dataset_entries (
  id                UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  dataset_id        UUID NOT NULL REFERENCES golden_datasets(id) ON DELETE CASCADE,
  entry_key         TEXT NOT NULL,
  input             JSONB NOT NULL,
  expected          JSONB NOT NULL,
  grading_kind      TEXT NOT NULL CHECK (grading_kind IN ('deterministic', 'meta_graded', 'hybrid')),
  rubric            JSONB,
  notes             TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (dataset_id, entry_key)
);

CREATE INDEX idx_dataset_entries_dataset ON golden_dataset_entries (dataset_id);
CREATE TRIGGER trg_dataset_entries_updated_at BEFORE UPDATE ON golden_dataset_entries
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE evaluation_runs (
  id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  dataset_id          UUID NOT NULL REFERENCES golden_datasets(id) ON DELETE RESTRICT,
  prompt_hash         TEXT NOT NULL,
  model               TEXT NOT NULL,
  triggered_by        UUID REFERENCES users(id) ON DELETE SET NULL,
  trigger_kind        TEXT NOT NULL,
  status              TEXT NOT NULL DEFAULT 'running',
  aggregate_score     REAL,
  entries_run         INTEGER NOT NULL DEFAULT 0,
  entries_passed      INTEGER NOT NULL DEFAULT 0,
  entries_failed      INTEGER NOT NULL DEFAULT 0,
  cost_usd            NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
  started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at        TIMESTAMPTZ,
  metadata            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_eval_runs_dataset ON evaluation_runs (dataset_id, started_at DESC);
CREATE INDEX idx_eval_runs_prompt ON evaluation_runs (prompt_hash);

CREATE TABLE evaluation_results (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  run_id          UUID NOT NULL REFERENCES evaluation_runs(id) ON DELETE CASCADE,
  entry_id        UUID NOT NULL REFERENCES golden_dataset_entries(id) ON DELETE RESTRICT,
  actual_output   JSONB NOT NULL,
  score           REAL NOT NULL CHECK (score >= 0.0 AND score <= 1.0),
  passed          BOOLEAN NOT NULL,
  grading_notes   TEXT,
  cost_usd        NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
  latency_ms      INTEGER,
  agent_trace_id  UUID REFERENCES agent_traces(id) ON DELETE SET NULL,
  reviewer_note   TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_eval_results_run ON evaluation_results (run_id);
CREATE INDEX idx_eval_results_failures ON evaluation_results (run_id) WHERE passed = FALSE;
CREATE INDEX idx_eval_results_entry ON evaluation_results (entry_id, created_at DESC);
```

**`entry_key` instead of `entry_index`** (v1.2, E3 resolution): index-only keys silently repoint reviewer notes when entries are added/removed. Stable string keys prevent that. The evaluation build caught this; the spec now reflects it.

**`regression_tolerance` on datasets, not runs** (v1.2, E4 resolution): the tolerance is a property of the dataset (what it considers acceptable regression), not of the individual run. Stored as JSONB for extensibility.

**`grading_kind` includes `'hybrid'`** (v1.2, E2 resolution): §7.2 of the evaluation spec had examples using `hybrid` (some checks deterministic, some meta-graded within one entry); §5's enum listing only named the two extremes. All three values now legitimate.

### 6.15 Retention operations

New domain (v1.2, migration 0012). The infrastructure spec's §12 defines the runtime; this section defines the schema.

```sql
CREATE TABLE retention_actions (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  ran_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  policy_name   TEXT NOT NULL,
  table_name    TEXT NOT NULL,
  rows_deleted  INTEGER NOT NULL,
  duration_ms   INTEGER NOT NULL,
  metadata      JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_retention_actions_recent ON retention_actions (ran_at DESC);
CREATE INDEX idx_retention_actions_policy ON retention_actions (policy_name, ran_at DESC);

CREATE TABLE retention_holds (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  table_name      TEXT NOT NULL,
  row_id          UUID NOT NULL,
  reason          TEXT NOT NULL,
  placed_by       UUID REFERENCES users(id) ON DELETE RESTRICT,
  expires_at      TIMESTAMPTZ,
  released_at     TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (table_name, row_id) WHERE released_at IS NULL
);

CREATE INDEX idx_retention_holds_active ON retention_holds (table_name, row_id) WHERE released_at IS NULL;
```

**`retention_actions`** is the audit trail. Every scheduled retention run writes a row per policy that ran, including 0-deletion runs (so absence proves inactivity, not silent failure).

**`retention_holds`** blocks the retention worker from deleting specific rows for stated reasons (legal hold, active investigation, research retention). The retention worker checks for an active hold on any candidate row before deleting.

### 6.16 Content artifacts

```sql
CREATE TABLE content_artifacts (
  id                 UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  session_id         UUID REFERENCES learning_sessions(id) ON DELETE CASCADE,
  session_turn_id    UUID REFERENCES session_turns(id) ON DELETE CASCADE,
  concept_id         UUID REFERENCES concepts(id) ON DELETE SET NULL,
  agent              agent_identity NOT NULL,
  kind               artifact_kind NOT NULL,
  stance             stance NOT NULL DEFAULT 'default',
  text               TEXT NOT NULL,
  model              TEXT NOT NULL,
  generated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  cost_usd           NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
  metadata           JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_content_artifacts_session ON content_artifacts (session_id, generated_at);
CREATE INDEX idx_content_artifacts_concept ON content_artifacts (concept_id, kind);
CREATE INDEX idx_content_artifacts_kind ON content_artifacts (kind, generated_at DESC);

CREATE TABLE content_citations (
  id                    UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  artifact_id           UUID NOT NULL REFERENCES content_artifacts(id) ON DELETE CASCADE,
  source_chunk_id       UUID NOT NULL REFERENCES source_chunks(id) ON DELETE RESTRICT,
  citation_marker       TEXT NOT NULL,
  passage_order         INTEGER NOT NULL,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (artifact_id, citation_marker)
);

CREATE INDEX idx_content_citations_artifact ON content_citations (artifact_id);
CREATE INDEX idx_content_citations_chunk ON content_citations (source_chunk_id);
```

**`model` and `generated_at`** are NOT NULL (V2 resolution): both were dropping silently through the draft loader in v1.1's build. The loader now requires both; missing values raise `MissingProvenance` rather than defaulting.

**Cost attribution path** (V1 resolution): `content_artifacts.cost_usd` links to `agent_traces.cost_usd` via `session_turn_id + agent`. The cost report joins these; the V1 defect was that this join had no index-friendly path. `idx_content_artifacts_session` plus the existing `idx_agent_traces_turn` makes the join fast.

## 7. Constraints and invariants

### 7.1 Database-enforced

- **Every mutable table has an `updated_at` trigger** that maintains the column on UPDATE. Application code cannot skip it.
- **Every foreign key names its ON DELETE behavior**. CASCADE when the child is meaningless without the parent; RESTRICT when the parent should not be deleteable while children exist; SET NULL when the child references a nice-to-have context.
- **CHECK constraints on bounded numeric fields**: mastery 0-1, difficulty 1-10, severity 1-3, rating 1-4, cap_reset_hour_local 0-23.
- **Partial UNIQUE indexes on active/pending state**: users active_email, signing_keys active, retention_holds unreleased.
- **Money columns are `NUMERIC(12, 6)`**, never REAL. Enforced by a CI check that reads the schema.
- **Timestamps are `TIMESTAMPTZ`**, never TIMESTAMP. Same check.

### 7.2 Application-enforced (with explicit reason)

- **Portfolio item signature required for credentials.** A trigger enforces `is_credential = TRUE ⇒ signature IS NOT NULL`. Not a straight CHECK because the constraint is conditional; a trigger is the correct expression in Postgres.
- **Only one active signing key at a time.** The partial unique index enforces this at insert; application code additionally coordinates rotation to swap active-ness in one transaction.
- **Ingestion provenance invariant**. Enforced at the code boundary via `studium.ingestion.provenance.CRITICAL_COLUMNS`, walked by a Tier 1 test. Not database-enforceable because "attribution-critical" is not a schema property but a semantic one.
- **State machine transition emissions**. Every transition names its emission path (agent runtime v1.0.1 patch §3); enforcement is a Tier 1 introspection test rather than a schema constraint. Same reasoning.

### 7.3 Cross-table invariants (application-enforced with tests)

- **`session_turns.artifact_id` populated for artifact-producing turns.** The AppliedEffects flow guarantees this in code; a Tier 2 test asserts that every content_artifacts row has at least one session_turns row referencing it.
- **`portfolio_items.signature` matches the payload for credentials.** A Tier 2 test regenerates the signature from the payload and verifies match.
- **`retention_actions` audit trail is exhaustive.** A Tier 2 test verifies that every scheduled retention run in the last 30 days has at least one `retention_actions` row per policy the schedule was supposed to run.

## 8. Indexes

Enumerated inline within each table's definition. Two general patterns worth naming:

**Partial indexes for filtered common queries.** `WHERE status = 'active'`, `WHERE status = 'pending'`, `WHERE is_credential = TRUE`. These are faster than full indexes on the columns because they store only the rows that match the predicate, and they're specifically what the query planner uses when the same predicate appears in the query.

**GIN indexes for full-text and JSONB.** `tsvector_text` on source_chunks; the journal search index; JSONB payload indexes as needed. GIN is the right choice for these access patterns; the write cost is offset by the read gains.

**HNSW for vector similarity.** Only on `source_chunk_embeddings.embedding`. Recall/precision tuning documented in retrieval subsystem §11.

## 9. Performance targets

Measured against production-shape data (10-15k source_chunks, 500-1000 session_turns per session, 100+ concurrent sessions expected at classroom tier). All targets are p95 on Postgres 16.15 with pgvector 0.8.6 and psycopg binary adapter registration.

| Query | MVP target | Classroom target | University target |
|---|---|---|---|
| Session bootstrap (§6.6) | <10ms | <20ms | <50ms |
| Turn insert (§6.6) | <5ms | <10ms | <20ms |
| Concept mastery lookup (§6.5) | <5ms | <10ms | <30ms |
| Vector search top-6 (§6.4) | <5ms | <10ms | <40ms |
| Keyword search top-20 (§6.4) | <30ms | <120ms | <400ms |
| Journal recent entries (§6.7) | <10ms | <30ms | <100ms |
| Cost ledger daily aggregate (§6.11) | <20ms | <50ms | <150ms |

**Keyword search is the binding constraint** at classroom scale, not vector search. The retrieval build's optional performance measurement inverted the spec's assumption. The number to watch as the corpus grows is the tsvector query time on `source_chunks`; vector search sits at 4-5ms even at 150k rows because HNSW's search complexity is sub-linear.

**The 14× vector-search improvement** from psycopg binary adapter registration is not a knob to be tuned; it's a required setup step. Application code that connects to Postgres without `register_vector()` runs the pre-optimization path (46ms text-encoded transfer per query) and violates the targets.

## 10. Retention policy

Every retention rule names three things: what data it applies to, how long the data lives, and what schedules the deletion.

### 10.1 Rules

| Rule | Applies to | Retention | Scheduler |
|---|---|---|---|
| User soft delete | `users` (deleted_at set) | 30 days | `privacy.purge_expired_soft_deletes` — daily, per subsystem 7 §12 |
| Session turns | `session_turns`, `agent_traces` | Indefinite (with erasure) | N/A — deleted on user hard-delete only |
| Cost ledger | `cost_ledger` | Indefinite; folded into the shared NULL-owner row at erasure request | Merge happens in the erasure transaction |
| Journal entries | `journal_entries` | Indefinite (with erasure) | N/A |
| Mastery events | `mastery_events` | Indefinite (with erasure) | N/A |
| Assessment attempts | `assessment_attempts`, `assessment_responses` | Indefinite | N/A |
| Portfolio items | `portfolio_items` | Indefinite; credentials retained permanently for verifiability | N/A |
| Review cards | `review_cards`, `review_events` | Indefinite unless learner explicitly deletes | N/A |
| Ingestion review queue | `ingestion_review_queue` (resolved) | 1 year after resolution | `retention.purge_resolved_ingestion_queue` — weekly, per subsystem 7 §12 |
| Content review queue | `content_review_queue` (resolved) | 1 year after resolution | `retention.purge_resolved_content_queue` — weekly |
| Evaluation runs | `evaluation_runs`, `evaluation_results` | 1 year (system-triggered), indefinite (manual runs) | `retention.purge_old_evaluation_runs` — weekly |
| Retention actions log | `retention_actions` | 5 years | `retention.purge_old_retention_actions` — monthly |
| Signing keys | `signing_keys` (deactivated, not compromised) | Public keys indefinite; private keys destroyed 90 days after deactivation | Private key destruction: manual operational task per subsystem 7 §11 |

### 10.2 Erasure procedure

Executed synchronously (not on the daily schedule) via `studium ops erase-user <user_id>`. Steps 1–4 are a single transaction — a half-erased user is a worse outcome than a failed request — and their **order is load-bearing**: anonymize the retained rows before deleting the parents, or the cascade removes the very rows a later step was going to anonymize. This inverts the v1.0 procedure, which was cascade-unsafe.

1. Set `users.deleted_at = NOW()` and record the request in `audit_log`. The hard-delete date is not stored; it is `deleted_at + 30 days`, evaluated by the purge.
2. **`users.email` and `users.display_name` are deliberately left intact.** The dispute window exists so an accidental erasure can be reversed, and reversing it needs the identity. The address is still free for immediate re-registration, because `idx_users_email_active` is partial on `deleted_at IS NULL` and the row has just dropped out of it. (`email` is `NOT NULL`, so there is no nulling it in place in any case.)
3. Anonymize the aggregates §10 retains, in this order: redact `assessment_responses` (`learner_response = '[redacted]'`, `feedback = NULL`, `missing_points = '[]'`) — which finds its rows *through* `assessment_attempts.user_id` — and only then null `assessment_attempts.user_id`, `learner_subject_id` and `overall_feedback`. Reversing those two loses the join. Then null `audit_log.actor_user_id` on every row except the `erase_user` record itself, which keeps its actor for accountability.
4. Fold the learner's `cost_ledger` rows into the shared NULL-owner row for each `(day, model)`, then delete the originals. There is no separate `deleted_users_aggregate` table: §6.12 reserves `user_id IS NULL` in `cost_ledger` for exactly this, and because `uq_cost_ledger_day` is `NULLS NOT DISTINCT`, a plain `SET user_id = NULL` succeeds for the first learner erased on a given day and raises a unique violation for the second — aborting a statutory request. See `DIVERGENCES.md` V13.
5. Delete the learner-owned data outright: `learner_subjects` (cascading to `concept_mastery`, `learning_sessions` and through them `session_turns`, `agent_traces`, `session_summaries`, `retrieval_checks`, plus `journal_entries` and their events, `review_cards` and their events, and `portfolio_items`), then `user_profiles` and `auth_sessions`. Auth sessions go immediately rather than on the window: an open token outliving an erasure request is a live credential for a closed account.

Thirty days after `deleted_at`, the scheduler-driven `privacy.purge_expired_soft_deletes` deletes the `users` row — by then nearly all that is left, since the learner-owned tables went in step 5 and the anonymized rows in `assessment_attempts`, `cost_ledger` and `audit_log` have NULL owners, so the cascade has nothing to follow. It runs as the `erasure_purge` stage of subsystem 7 §12.1's nightly pass.

**Not built: the credential retention exception.** The intent is that a learner's portfolio items marked `is_credential = TRUE` survive hard-delete for verifiability — a credential someone else holds must remain verifiable — with the item's `learner_id` FK becoming SET NULL rather than CASCADE. **No `is_credential` column exists**, and `portfolio_items` currently cascades with everything else in step 5, so an erasure today destroys the learner's credentials. Recorded here as a requirement the schema does not yet meet, not as behaviour to rely on.

**Not built: the purge records nothing.** `privacy.purge_expired_soft_deletes` deletes the row and returns a count; it writes no `retention_actions` entry, though every ordinary retention policy does. The `audit_log` row from step 1 records that erasure was *requested*; nothing records that it *completed*. §12.2 asks for an audit trail for "why did that data go away", and the one deletion with a statutory deadline is the one that leaves no trace.

### 10.3 The scheduler as a first-class dependency

The infrastructure subsystem's §12 scheduler wires each of the seven scheduled retention jobs to a runnable. This was the finding the infrastructure build made explicit: five instances of "scheduled job with no scheduler" had accumulated across builds. The v1.2 retention table above names the scheduler for each rule; if a rule appears here with no named scheduler, that's a spec defect the next revision must close.

### 10.4 Restore drill and retention

Restore drills (subsystem 7 §9.3) run against a scratch database, so the retention worker running against production is unaffected. If a restore is executed for real (recovery from data loss), the retention worker's `retention_actions` audit trail lets you determine what was retention-deleted vs. what was lost.

## 11. Testing strategy

Same three-tier structure the rest of the project uses.

**Tier 1 — Offline.**

- Schema-level unit tests: every constraint has at least one violating input and one passing input in the test suite.
- Migration reversibility: every migration file has an apply-verify-downgrade-verify test that runs in CI on any schema change.
- Trigger correctness: `updated_at` triggers verified against synthetic rows.
- Provenance invariant test (from ingestion §13): walks writer functions via introspection and asserts no default provenance parameters.

**Tier 2 — Online (against real Postgres 16.15).**

- Every migration applied against a real database, verified schema matches expected, downgraded, verified empty, re-applied.
- Cross-table invariant tests: `session_turns.artifact_id` populated for artifact-producing turns, portfolio signatures verify, retention_actions exhaustive.
- Query performance targets from §9 verified against seeded data at the target row counts.
- Retention worker correctness: seeded data with known retention state, worker runs, expected rows deleted, expected rows retained, retention_actions audit populated.

**Tier 3 — Paid (real embedding calls).**

- Not applicable to this subsystem directly. Retrieval subsystem §11 covers vector search quality; ingestion subsystem covers embedding writes; those tests exercise this layer's schema.

**Fixtures.** Seeded system user (id fixed), sample subjects (lambda calculus per the migrated draft), sample sources (Michaelson chapters). All fixtures reset between tests.

## 12. Version history

**v1.2 — 30 August 2026.** Consolidated revision folding in 25 items across seven build cycles. Adds 8 new tables (`ingestion_review_queue`, `signing_keys`, `golden_datasets`, `golden_dataset_entries`, `evaluation_runs`, `evaluation_results`, `retention_actions`, `retention_holds`), 10 new columns on existing tables, 4 enum value additions, and 4 CI-enforced schema conventions. Migrations 0010, 0011, 0012 applied and verified reversible. Retention policy §10 revised to name enforcement mechanism per policy. Design principle added: "every obligation named in this spec names what performs it." The full change list is in the v1.1 → v1.2 redline (delivered alongside).

**v1.1 — 14 August 2026.** Consolidated 6 items surfaced during v1.0 build. Added exchange_index on agent_traces, added ingestion_jobs table, tightened NULL constraints on cost_ledger provenance, added deleted_users_aggregate for erasure-safe cost history, established naming convention for constraints, added CI reversibility check.

**v1.0 — 5 August 2026.** Initial specification. 34 tables, 80 indexes, 15 CHECK constraints, 6 enum types. Postgres 16 + pgvector + Alembic.

## 13. Forward references

**Subsystem 2 v1.1 (pending).** Consolidates v1.0 + v1.0.1 patch + v1.0.2 amendment + pending items (R1, R2, R4, R7, R9, cost re-baseline, Opus 5 swap, S4 return-type change, plus the E9/E10 fixes retroactively documented). Will reference this v1.2 for the artifact_id population, the exchange_index, and the cost_evaluation_usd column.

**Subsystem 3 v1.1 (pending).** Consolidates retrieval revisions (S1 graph direction, S2 thin-grounding writer, S7 fallback labelling, S11 positional proxy for degraded reranker, and §13 threshold calibration if Tier 3 lands). Will reference this v1.2 for the chunk_type, tsvector_text, extraction_confidence, and superseded_at columns.

**Content authoring.** Not a subsystem; the domain-expert work that populates the concept graph, rubric prompts, and concept-source links. Deferred per the reviewer's decision. When it happens, the schemas in §6.2 and §6.8 support it directly — no schema changes anticipated.

**Multi-language support.** The `tsvector_text` generated column hardcodes `'english'`. Multi-language would require per-source language detection and per-language tsvector configurations. Not planned for MVP; the schema change is small when it happens.

**Signing key hardware backing.** Currently keys live in Fly secrets. Hardware-backed signing (HSM, YubiHSM, cloud KMS) is a v2 consideration for credentialing at scale. The schema is unaffected — public keys stay in `signing_keys`; the private key location is an infrastructure concern.

---

## End of specification

This document defines Studium's data layer in full at v1.2. Everything the code writes to and reads from Postgres is specified here. Where prior specs (v1.0, v1.1, patches, amendments) conflict with this document, this document is authoritative. Where the code has diverged from prior specs but agrees with this document, the divergence is closed. Where the code has diverged from this document, that is a defect this document's next revision must close.

The v1.1 → v1.2 redline document is delivered alongside for reviewer convenience; the redline is not authoritative — this document is.

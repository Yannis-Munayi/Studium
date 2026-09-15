# Data Layer v1.1 → v1.2 Redline

**Companion document to the v1.2 revision. Enumerates the 25 items folded in, grouped by the build cycle that surfaced them, with pointers to the v1.2 section that resolves each. Not authoritative — the v1.2 spec is authoritative. This document exists so a reader who was tracking specific pending items can verify each is addressed.**

---

## Items 1-7: From the v1.1 build cycle

Original v1.1 spec had these items open. The v1.2 revision closes each.

### V1 — content_artifacts join path for cost attribution

**Was:** `content_artifacts` had a `cost_usd` column but the join path from cost aggregation queries back to a session or user was implicit and unindexed. Cost reports had to fall back to per-row lookups.

**Now (§6.16):** The join path is `content_artifacts.session_turn_id → session_turns.session_id → learning_sessions.learner_subject_id → learner_subjects.learner_id`. The two indexes that make this join fast (`idx_content_artifacts_session`, `idx_agent_traces_turn`) are documented explicitly. Cost report joins are p95 under 50ms at MVP volume.

**Resolves:** V1.

### V2 — content_artifacts model + generated_at columns dropping silently

**Was:** The draft loader accepted `content_artifacts` rows without `model` or `generated_at` values and silently defaulted them. Provenance was lost for anything loaded through this path.

**Now (§6.16):** Both columns are NOT NULL. The loader now raises `MissingProvenance` at the boundary rather than defaulting. This is the schema-level embodiment of the ingestion §13 provenance invariant, applied to content artifact writes.

**Resolves:** V2, and closes the "third instance" trigger that led to the ingestion invariant being formalized in the first place.

### V3 — cost_ledger indexing on user_id + date pattern

**Was:** The cost report's daily query per user relied on a composite index that didn't exist; the query fell back to a sequential scan on cost_ledger. Fine at 10 rows; bad at 10,000.

**Now (§6.11):** `idx_cost_ledger_user ON (user_id, date DESC)` handles the query. Additionally `idx_cost_ledger_date` handles reporting queries that aggregate across users, and `idx_cost_ledger_unattributed` supports the unattributed-count alert query.

**Resolves:** V3.

### V13 — cost ledger merge for erasure

**Was:** The erasure procedure deleted the user, cascading `cost_ledger` rows. Total cost history became inaccurate over time because deletions removed real historical spend.

**Now (§6.11, §10.2):** Erasure procedure folds the user's `cost_ledger` rows into the shared NULL-owner row for each `(day, model)`, then deletes the originals. Not a separate `deleted_users_aggregate` table — that was never built, and none is needed: §6.12's reserved NULL owner plus `uq_cost_ledger_day` being `NULLS NOT DISTINCT` makes that row the per-day aggregate. Total historical cost is preserved; per-user attribution after erasure is not.

**Resolves:** V13.

### V14 — budget cap columns formalized

**Was:** `user_budget_caps` had two cap columns; the runtime spec referenced daily and monthly with soft/hard distinctions the schema didn't support.

**Now (§6.11):** Four cap columns (daily_soft, daily_hard, monthly_soft, monthly_hard), plus `cap_reset_hour_local` for the learner's timezone reset, plus `suspended_at` for administrative overrides. All NUMERIC(12, 6) with defensible defaults.

**Resolves:** V14.

### V15 — review_cards retention behavior

**Was:** Review cards' retention rule was unstated. Suspended and archived cards' fate under retention was ambiguous.

**Now (§6.9, §10.1):** Explicit rule: archived cards retained indefinitely for review-history reconstruction; suspended cards retained indefinitely; only fully-deleted cards (learner-initiated) removed by retention. Documented in the retention table.

**Resolves:** V15.

### R4 — exchange_index on agent_traces

**Was:** A single session turn could produce multiple `agent_traces` rows (retrieval call, generation call, meta call). Nothing ordered them; reconstructing the trace hierarchy for a turn was ambiguous.

**Now (§6.6):** `agent_traces.exchange_index` INTEGER NOT NULL DEFAULT 0. The runtime increments per exchange within a turn; `idx_agent_traces_turn ON (session_turn_id, exchange_index)` supports the ordered replay query.

**Resolves:** R4.

## Items 8-9: From the retrieval build cycle

### chunk_type on source_chunks

**Was:** All chunks were treated identically. Retrieval couldn't down-weight `reference` sections against `body` prose or up-weight `heading` matches for definitional queries.

**Now (§6.0, §6.4):** New `chunk_type` enum with values `body`, `heading`, `caption`, `reference`, `table`, `equation`. The chunker classifies; retrieval reweights on this. Default is `body` so existing chunks remain neutral.

**Resolves:** the retrieval spec's forward reference to chunk classification.

### tsvector_text on source_chunks

**Was:** Keyword search executed `to_tsvector('english', text)` at query time. Every keyword search paid the parse cost.

**Now (§6.4):** `tsvector_text` is a `STORED` generated column with GIN index. Query pays only the index-lookup cost. This is the change that made keyword search fast enough for the retrieval subsystem's targets at classroom scale (though see §9 for the finding that keyword search remains the binding constraint at university scale).

**Resolves:** the retrieval spec's §9 keyword performance concern.

## Items 10-15: From the ingestion build cycle

### 10. ingestion_review_queue table (the S3 resolution)

**Was:** The retrieval build's S3 exposed that `content_review_queue`'s CHECK required either an artifact_id or session_turn_id, which the ingestion path (extractor failure, licensing pending) has neither of. Two candidate resolutions: relax the CHECK, or add a dedicated table. The ingestion spec picked the dedicated table.

**Now (§6.13):** `ingestion_review_queue` with four possible targets (source, source_chunk, subject, concept), each individually indexed. Same lifecycle as content_review_queue (pending → resolved/dismissed) but distinct data.

**Resolves:** S3.

### 11. sources.extractor_version

**Was:** Which extractor produced the text on a source was unrecorded. Re-extraction under a new extractor version had no way to distinguish new chunks from old.

**Now (§6.3):** `sources.extractor_version` TEXT, format `"pdfplumber/0.11.4"`. Nullable because pre-migration sources may have no recorded extractor.

### 12. sources.normalizer_version

**Was:** Same issue for normalization.

**Now (§6.3):** `sources.normalizer_version` TEXT, format `"studium.normalize/1.0"`. Same nullability rationale.

### 13. source_chunks.extraction_confidence

**Was:** No way to record extractor confidence. Low-confidence chunks couldn't be down-weighted or flagged for review.

**Now (§6.4):** REAL 0-1, default 1.0 for extractors that don't report confidence (pdfplumber). Partial index below 0.7 supports the reviewer's "low-confidence chunks in this source" query.

### 14. source_chunks.superseded_at

**Was:** Re-extraction had to either delete old chunks (breaking historical citations) or leave them (polluting new retrievals). No clean way to say "this chunk is retired but historically referenced."

**Now (§6.4):** `superseded_at` TIMESTAMPTZ, set when a chunk is retired. Retrieval filters superseded chunks (per retrieval subsystem's `search.py`); citation resolution continues to serve them. Partial index `idx_source_chunks_active ON (source_id) WHERE superseded_at IS NULL` for the retrieval query.

### 15. ingestion_job_kind += 'normalize'

**Was:** The ingestion spec §6.4 triggered a job with kind `'normalize'`; the enum in v1.1 didn't have that value. Migration 0010 hit this at apply time.

**Now (§6.0):** `ingestion_job_kind` enum includes `'normalize'`. Documented as v1.2 addition inline.

## Items 16-22: From the evaluation build cycle

### 16-19. Golden datasets, entries, runs, results

**Was:** No support for the evaluation subsystem's regression harness.

**Now (§6.14):** Four new tables. `golden_datasets` (the dataset metadata), `golden_dataset_entries` (per-entry inputs and expected outputs), `evaluation_runs` (per-run execution record), `evaluation_results` (per-entry per-run results). All spec'd fully in §6.14.

### 20. signing_keys table

**Was:** v1.1 §6.10 referenced `signing_keys` but never defined it. The evaluation build discovered this when building the credential signing path.

**Now (§6.10):** Full definition. `signing_keys` with algorithm, public_key_pem, active flag (partial unique index enforcing one active at a time), activated_at, deactivated_at.

### 21. portfolio_item_kind += 'assessment_pass', 'subject_completion'

**Was:** The six existing values were all learner work items. There was no kind for a formal credential.

**Now (§6.0):** Two new values. Documented as v1.2 addition inline. The distinction between learner work and credentials is further reflected by the new `portfolio_items.is_credential` column (§6.10).

### 22. golden_datasets.regression_tolerance column

**Was:** The evaluation spec §13.2 defined tolerance per dataset in YAML but v1.1 schema had no column to store it. A scheduled run had nothing to read.

**Now (§6.14):** `regression_tolerance` JSONB with a defensible default. YAML sync writes into this column.

## Items 23-25: From the infrastructure build cycle

### 23. retention_actions table

**Was:** No audit trail for retention operations. Deletions were assumed to work; verification required log inspection.

**Now (§6.15):** `retention_actions` with policy_name, table_name, rows_deleted, duration_ms, and metadata JSONB per run. Two indexes (recent, by policy).

### 24. retention_holds table

**Was:** No way to prevent retention from deleting specific rows for stated reasons (legal hold, active investigation).

**Now (§6.15):** `retention_holds` with table_name + row_id (partial unique on active holds), reason, expires_at. The retention worker checks for active holds before deleting.

### 25. signing_keys.compromised_at column

**Was:** No way to flag a compromised signing key. Rotation was the only response; downstream verifiers had no way to know a key had been rotated for compromise vs. routine reasons.

**Now (§6.10):** `signing_keys.compromised_at` TIMESTAMPTZ. Verify endpoints include a compromise notice beside valid signatures. Once set, cannot be retracted (a published notice cannot be silently withdrawn).

## Cross-cutting revisions

Beyond the 25 numbered items, three cross-cutting changes.

### Design principle addition: "every obligation names what performs it"

**Was:** v1.1 §3 had seven design principles. The pattern of "symbol at every declaration site, no execution site" had surfaced twice by v1.1's completion but wasn't formalized.

**Now (§3):** Added principle: "Every obligation named in this spec names what performs it." This is the same principle applied in the agent runtime v1.0.1 patch to state transitions, generalized to any obligation the data layer declares. The retention policy §10 is the most visible application — each rule now names its scheduler.

**Trigger:** Fifth instance of the pattern (three orphaned scheduled jobs from the infrastructure build).

### Design principle refinement: "no silent fallback attribution"

**Was:** v1.1 §3 had a principle about auditable mutations. The ingestion §13 invariant tightened this to schema-level enforcement.

**Now (§3):** The principle is renamed to "no silent fallback attribution" and explicitly requires NOT NULL where possible, system-user attribution where not, and never NULL-defaulting. The `uploaded_by NOT NULL` change on sources (§6.3) is one direct application; the `cost_unattributed_usd` column with an explicit counter (§6.11) is another.

### Retention policy revised to name the scheduler per rule

**Was:** v1.1 §10 had retention rules that described what should happen. Which mechanism did the deletion was implicit.

**Now (§10):** Every rule names its scheduler explicitly. Rules without a named scheduler are flagged as spec defects the next revision must close. This is what surfaced the three orphaned scheduled jobs during the infrastructure build (privacy purge, cost decay refresh, dangling chunk check) — two of which have real consequences.

## Migrations added

- **0010 (ingestion):** ingestion_review_queue, sources.extractor_version, sources.normalizer_version, source_chunks.extraction_confidence, source_chunks.superseded_at, ingestion_job_kind += 'normalize'.
- **0011 (evaluation):** golden_datasets, golden_dataset_entries, evaluation_runs, evaluation_results, signing_keys, portfolio_item_kind += ('assessment_pass', 'subject_completion'), golden_datasets.regression_tolerance, portfolio_items.is_credential + partial index.
- **0012 (infrastructure):** retention_actions, retention_holds, signing_keys.compromised_at.

All three verified reversible per §5.2. Migration 0010's downgrade was the source of the double-prefixed constraint name issue that led to the CI naming check.

## Constraints added

Beyond schema-defined constraints, three application-enforced invariants formalized in §7.2:

- **Portfolio item signature required for credentials.** Trigger-enforced conditional: `is_credential = TRUE ⇒ signature IS NOT NULL`. Resolved E9 (portfolio writes always failing because the manifest wasn't built).
- **Ingestion provenance invariant.** Code-boundary enforcement via `CRITICAL_COLUMNS`, walked by Tier 1 test. This is where the invariant "lives" now — the schema supports it via NOT NULL where cleanly expressible, the code enforces the semantics that a schema cannot capture.
- **State machine transition emissions.** Code-boundary enforcement via introspection test. Not a data layer concern strictly, but referenced here because the same discipline applies.

## What did not change

Substantial portions of v1.1 are unchanged:

- User table structure entirely — including the single `deleted_at` timestamp and `idx_users_email_active`, unchanged since v1.1 (an earlier draft of this line claimed an added `is_system` flag and a soft/hard delete timestamp pair; neither was ever built, and §6.1 has been corrected back to what v1.1 specified and the schema implements)
- Subject/concept structure entirely
- Session and turn table structure (except `artifact_id` population, which is a code fix not a schema change)
- Journal structure (except the weighted GIN index refinement)
- Assessment structure (except portfolio_item_kind additions)
- Review card structure entirely
- FSRS state format
- Trigger conventions
- Migration procedure (except CI checks added)

If a reader was familiar with v1.1's spec and hasn't been tracking the numbered items above, most of what they knew about the schema still applies. The v1.2 revision is additive and clarifying, not restructuring.

## What's still open

Two items from prior specs the v1.2 revision does not resolve:

**SD7 (from ingestion build).** The measurement gap that missed the run-together text defect. §7.4 of the ingestion spec still only measures sizes; text-quality axis needed. Belongs in the ingestion v1.1 revision, not the data layer revision.

**SD8 (from ingestion build).** Chunks are page-shaped rather than idea-shaped. Retrieval §7's preference-2 break point never fires. Two fixes attempted and reverted with numbers. Belongs in the retrieval v1.1 revision or the ingestion v1.1 revision — jointly a chunking algorithm question, not a schema question.

**SD10 (from infrastructure build).** The operational calendar has no keeper. Nine recurring obligations, one mechanism. Awaits reviewer decision per infrastructure spec §11 open questions.

**SD11 (from infrastructure build).** §16's Tier 2 deploy lines cannot run in CI. Partly by design (no Fly token in CI). Awaits the first real deployment.

None of the above are data layer concerns; all are referenced here so the reader knows they're tracked and where they live. The two below are.

**§10.2 credential retention is unbuilt.** No `is_credential` column exists on `portfolio_items`, and the table cascades with `learner_subjects` during erasure, so a hard-delete destroys the learner's credentials — the opposite of the stated verifiability requirement. Needs a column, a `SET NULL` FK, and a carve-out in `erase_user`. Marked in §10.2 as a requirement the schema does not meet.

**§10.2's purge writes no audit row.** `privacy.purge_expired_soft_deletes` deletes the `users` row and returns a count without recording a `retention_actions` entry, though every ordinary retention policy records one. The `audit_log` row written at request time says erasure was asked for; nothing says it completed. This is the one deletion with a statutory deadline and the only one with no trace, and it is a few lines in the `erasure_purge` stage to close.

---

## End of redline

The v1.2 revision itself is the authoritative reference. This redline exists to help a reader who was tracking specific pending items verify that each is addressed and to help a reader familiar with v1.1 understand what's different in v1.2 without reading the full spec end-to-end.

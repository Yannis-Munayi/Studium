# Review — Data Layer Specification v1.0

Read against `spec/01-data-layer.md` (identical to `learning-system-plan.pdf`) and the existing draft in `tutor/`.

Verdict: the shape is right. Deterministic gating with an append-only evidence log, content versioning via `superseded_by`/`retired_at`, `ON DELETE RESTRICT` on citations, and separating `session_summaries` from the session row are all good calls that will hold up. What follows is what breaks if you build it exactly as written.

Findings are grouped by whether they block, whether they contradict something else in the document, or whether they're a design risk worth a decision.

---

## A. Blocking — these fail on first contact

### A1. `uuid_generate_v7()` emits 33 hex characters, so every INSERT fails

§6.0. The fallback function assembles: 12 (timestamp) + 4 (`'7'` + `substr(hex, 2, 3)`) + 2 (variant) + `substr(hex, 6)` → 15 = **33 characters**. A UUID needs 32. Every `INSERT` into every table raises `invalid input syntax for type uuid`.

Separately: **`uuid-ossp` does not provide `uuid_generate_v7`** (it ships v1/v3/v4/v5 only). §4 lists it as the source. The real options are the `pg_uuidv7` extension (which §6.0's comment already points at), Postgres 18's native `uuidv7()`, or this SQL fallback — and on your Postgres 16 floor it's one of the first two or this:

```sql
CREATE OR REPLACE FUNCTION uuid_generate_v7() RETURNS uuid AS $$
DECLARE
  v_time_ms bigint := (extract(epoch from clock_timestamp()) * 1000)::bigint;
  v_rand    bytea  := gen_random_bytes(10);
  v_hex     text   := encode(v_rand, 'hex');       -- 20 hex chars
BEGIN
  RETURN (
    lpad(to_hex(v_time_ms), 12, '0') ||            -- 12: 48-bit timestamp
    '7' || substr(v_hex, 1, 3) ||                  --  4: version + rand_a
    to_hex((get_byte(v_rand, 2) & 63) | 128) ||    --  2: variant + rand_b high bits
    substr(v_hex, 7, 14)                           -- 14: rand_b
  )::uuid;                                          -- = 32
END;
$$ LANGUAGE plpgsql VOLATILE;
```

`uuid-ossp` becomes unused once this is right — `gen_random_bytes` is pgcrypto. Drop it from §4 or keep it and say why.

### A2. The PIPEDA erasure procedure is incompatible with the FK topology

§10 step 2 deletes `learner_subjects`. Six tables cascade off it (`concept_mastery`, `learning_sessions`, `journal_entries`, `assessment_attempts`, `review_cards`, `portfolio_items`), and `learning_sessions` cascades onward to `session_turns`, `agent_traces`, `retrieval_checks`, `review_events`, `session_summaries`. Three consequences:

- Step 2 says to rewrite `session_turns` bodies to `{"redacted": true}`. Those rows are already gone by then — the step is unreachable.
- Step 3 says to **retain** `assessment_attempts` and `assessment_responses`. They were cascaded away by the `learner_subjects` delete in step 2.
- Step 3 says to anonymize `user_id` "to a UUID that references nothing." `assessment_attempts.user_id` and `cost_ledger.user_id` are both `NOT NULL REFERENCES users(id)`, so that write violates the FK. And step 4's hard delete of the `users` row cascades `cost_ledger` away regardless.

Net: of the four things §10 says to retain, only `audit_log` survives (its `actor_user_id` is `SET NULL`).

Pick one and spec it explicitly:

1. **Tombstone user.** Reserve a fixed `anonymized` user row; repoint retained rows to it before deleting anything. Simplest, preserves the FKs.
2. **Nullable + SET NULL** on `assessment_attempts.user_id`, `assessment_attempts.learner_subject_id`, and `cost_ledger.user_id`. Changes the NOT NULL guarantees other queries rely on.
3. **Reorder**: redact and re-parent everything you intend to keep *first*, delete parents last.

Whichever you choose, the erasure job needs an explicit, ordered step list — the current prose reads as a set, and order is the whole problem.

### A3. Two mutable tables have no `created_at`/`updated_at` and no trigger

`content_artifacts` (has `generated_at`, `deleted_at`, mutates `status`/`reviewed_by`/`superseded_by`) and `learning_sessions` (has `started_at`, mutates `ended_at`/`summary`/`total_cost_usd`). §5 says "`created_at` and `updated_at` on every mutable table." The §14 test "every mutable table has an `updated_at` trigger" fails on both, plus `subject_metadata`, `session_summaries`, `assessment_attempts`, and `assessment_responses`.

Related: §14's "every soft-delete table has a partial index excluding deleted rows" fails on `subjects`, which carries `deleted_at` and has **no index at all**.

Also note §9's optimistic-concurrency scheme ("the client submits the row's `updated_at` as an if-match condition") is specified for exactly the tables that lack the column — concept edits and artifact edits.

### A4. The `total_cost_usd` trigger can't be written as described

§6.6: "maintained by a trigger on `session_turns` insert." `session_turns` has no cost column — cost lives on `agent_traces`. The trigger belongs on `agent_traces` insert, walking `session_turn_id → session_turns.session_id`. Worth spelling out, because it also determines whether ingestion/artifact-generation cost ever reaches a session total (see C3).

### A5. §7's "every FK has an accompanying index" is false in ~20 places

The §14 test is auto-generated from `information_schema`, so this fails immediately. Missing outright:

`subjects.authored_by` · `content_artifacts.reviewed_by` · `content_artifacts.superseded_by` · `rubric_criteria.reviewed_by` · `learner_subjects.subject_id` · `learner_subjects.current_focus_concept_id` · `concept_mastery.concept_id` · `learning_sessions.focus_concept_id` · `session_turns.artifact_id` · `journal_entries.first_session_id` · `journal_events.session_id` · `assessment_attempts.scope_concept_id` · `assessment_attempts.session_id` · `review_cards.concept_id` · `review_events.session_id` · `review_events.artifact_id` · `portfolio_items.learner_subject_id` · `portfolio_items.parent_item_id` · `content_review_queue.artifact_id` · `content_review_queue.session_turn_id`

Plus a subtler class: **partial indexes do not serve cascade deletes of the excluded rows.** `content_artifacts.concept_id` is only indexed `WHERE status = 'active' AND retired_at IS NULL AND deleted_at IS NULL`, so deleting a concept sequential-scans every draft and retired artifact. Same pattern on `sources.subject_id`.

Either add the indexes or narrow the §7 claim and the §14 test to "every FK that participates in a cascade path."

While you're there, four indexes are redundant with a unique constraint that already indexes the same leading columns: `idx_session_turns_session_time`, `idx_content_citations_artifact`, `idx_assessment_responses_attempt`, and (near-redundantly) `idx_cost_ledger_user_day`.

---

## B. Dangling references and self-contradictions

| # | Location | Problem |
|---|---|---|
| B1 | §6.5 | "Defaults come from `concepts.metadata` at row creation" — `concepts` has no `metadata` column. |
| B2 | §6.8 | "per-subject overrides live in `subject_metadata`" — no threshold column there, and that table is defined as computed graph statistics. Wrong home; put it on `subjects`. |
| B3 | §6.2 | Adjacent sentences contradict: `subject_metadata` is "maintained by a trigger on `concepts` and `concept_edges`", then "**not** maintained by row-level triggers because bulk imports…". Keep the second. |
| B4 | §12 | Introduces `concepts.module_slug` via migration `0002` — a column absent from §6, in a document that claims "every column the system will use is here." Also: §12 and §6.5 name `.sql` migrations (`0002_module_slug.sql`, `0003_grants.sql`) while §13 mandates `NNNN_verb_object.py`. |
| B5 | §6.6 / §6.11 | `learning_sessions` carries `summary`, `key_points`, `open_threads` **and** `session_summaries` carries the same three. §6.11 justifies the separate table but never retires the columns. Two sources of truth; the §8 query reads the table. Drop the columns. |
| B6 | §12 | "Passed status \| derived (not stored); recomputed from `assessment_attempts.passed`" — self-contradictory, and `passed` *is* a stored column. (The draft stores it too: `progress.py:41`.) |
| B7 | §10 | "Every table has a defined retention policy" — the table omits `retrieval_checks`, `session_summaries`, `concept_mastery`, `learner_subjects`, `user_profiles`, `user_budget_caps`, `subjects`, `concepts`, `content_artifacts`, `source_chunks`. |
| B8 | §6.1 / §2 | `preferences.tutor_voice_id` / `lecturer_voice_id` reference a voice library. §2 promises the schema anticipates the multi-voice module "so no migration is needed when it ships," but no voice table exists. Either add it or drop the claim. |
| B9 | §5 vs §6 | "`TEXT` for everything. No `VARCHAR(n)`" — then `CHAR(64)` in four places. `CHAR` is blank-padded and compares by padded value; use `TEXT` + `CHECK (length(x) = 64)`, or `BYTEA`. Same class: §5 fixes money at `NUMERIC(10,4)`, then `agent_traces.cost_usd` is `(10,6)` and `cost_ledger.cost_usd` is `(12,4)`. Both deviations are defensible — state them as exceptions. |
| B10 | §4 vs §15 | §4 specifies PgBouncer in transaction mode and analyzes its constraints; §15 says PgBouncer isn't needed at MVP. Fine, but say which is the day-one posture. |

---

## C. Design risks worth a decision

### C1. Gating reads a value that a daily job refreshes

`p_known_decayed` is set equal to `p_known` on write (§8), recomputed "by a separate daily job," and is what the prerequisite check filters on. So unlocking decisions run against a value up to 24 hours stale — and stale in the *permissive* direction right after evidence, since write-time decay is zero.

This matters more than it looks: §3 makes deterministic gating a load-bearing invariant, and this is the one place where the gate reads a lazily-maintained cache. Either compute decay in the query (`p_known * exp(-λ · age)` as a SQL expression or a generated column), or state plainly that gating tolerates day-old decay.

Related: `idx_concept_mastery_learner` sorts on `p_known DESC`, but §7 says it serves "the mastery-sorted concept list," and every consumer reads `p_known_decayed`. The index doesn't serve its stated query.

### C2. The prerequisite check is a direct-parent test, not reachability

§3 says "unlocking a concept is a graph reachability test over `concept_edges`." The §8 query checks only edges where `to_concept_id = :concept_id` — immediate parents. Equivalent only if every concept's prerequisites are transitively complete in the authored graph, which nothing enforces. Pick one: keep the direct-parent check and reword §3, or make it a recursive CTE.

### C3. Cost tracking has four leaks and a missing join key

§3 calls cost tracking first-class and §6.12 defines `cost_ledger` as summing `agent_traces`. But:

- **`agent_traces` has no `user_id`.** The daily roll-up "grouped by `user_id`" needs `agent_traces → session_turns → learning_sessions → user_id`, two joins over a 90-day table, and so does the erasure job. Denormalize `user_id` onto `agent_traces`.
- **Four cost columns never reach the ledger**: `content_artifacts.cost_usd`, `ingestion_jobs.cost_usd`, `session_summaries.cost_usd`, `assessment_attempts.grading_cost_usd`. At MVP, content generation and ingestion are plausibly the *dominant* spend, and budget enforcement won't see any of it.
- **Cache-write pricing can't be reconstructed.** A single `cache_write_tokens` column can't distinguish a 5-minute-TTL write (billed 1.25×) from a 1-hour-TTL write (2×). If you use both TTLs, stored token counts no longer re-derive `cost_usd`. Split the column, or record the TTL.
- **Server-tool usage isn't captured.** Web search is billed per request, not per token, so a search-heavy trace's `cost_usd` can't be re-derived from the token columns at all. Add a `server_tool_use` JSONB or counter columns.

One semantic note to write down: the API's `input_tokens` is the **uncached remainder**, not total prompt size (total = `input_tokens + cache_creation + cache_read`). If `tokens_in` stores it verbatim and a dashboard reports it as "input tokens," the number will look wrong in exactly the cases where caching is working.

### C4. Cost reconciliation as specified isn't achievable

§9: "reconciled by a daily job that compares the trace count to invoice line items from Anthropic." Billing doesn't expose per-call line items — usage and cost are reported in aggregate (by day, model, API key, workspace). Reconcile **aggregate daily token/cost totals** against the usage reporting, not trace counts against line items. The check still works; the description doesn't.

Also in §9: the cost budget check query (§8) joins `cost_ledger` to `user_budget_caps` on `ON ubc.user_id = :user_id` — a constant, not a join predicate — so it cross-joins the whole ledger and is rescued only by the `WHERE`. It also compares against hard caps only, though §6.12 defines soft caps that "raise a warning." Rewrite with the user filter in the join and both cap tiers selected.

### C5. Rubric key points and Tracker hypotheses leak through learner-readable rows

§11 states twice that a learner "cannot read `rubric_criteria.key_points` at any time." But §11 also grants learners read on their own `assessment_responses`, and `criterion_snapshot` is defined as "the criterion's `key_points` at time of grading." The rule is defeated by the row that implements versioned grading.

Same shape in §6.7: `journal_entries.hypothesis` "should not be presented to the learner verbatim… it can be wrong, and reading it can be dispiriting" — while §11 grants learners read on their own `journal_entries`.

Both need **column-level** projection rules, not table-level ones. Worth adding a short "columns never serialized to a learner" list to §11, since it now spans three tables.

### C6. §11 grants learners write access to derived state

"A learner can read and write only their own … `concept_mastery`, … `assessment_responses` …". Mastery is BKT-derived and assessment grades are Evaluator-written; learner-writable versions of both defeat §3's first invariant. Split the rule into read-only and read-write sets.

### C7. `turn_index` from an in-memory counter

§9 relies on "session affinity in the load balancer" to guarantee one process per session. LB affinity pins to a *machine*, not a process — the moment you run more than one uvicorn worker, two processes can serve the same session and collide on `UNIQUE (session_id, turn_index)`. Deriving the index in-transaction (`SELECT COALESCE(MAX(turn_index), -1) + 1 … FOR UPDATE`, or a per-session sequence) removes the assumption entirely at MVP write volumes.

### C8. Append-only enforcement vs. the jobs that must delete

`mastery_events`, `review_events`, `agent_traces`, and `audit_log` have `UPDATE`/`DELETE` revoked from the application role. But §10's retention job hard-deletes from all four, and the erasure job deletes `agent_traces` outright. Those jobs need a separate privileged role — say so, and say where it's used. (Cascade deletes are unaffected: Postgres runs RI actions as the table owner.)

### C9. Cross-entity consistency isn't constrained

Several tables carry `user_id` *and* `learner_subject_id` (`learning_sessions`, `journal_entries`, `assessment_attempts`, `portfolio_items`). Nothing prevents them disagreeing — a session whose `user_id` isn't the enrollment's owner is insertable, and it would silently break both the §11 access checks and the erasure sweep. Same pattern: `concept_edges.subject_id` vs. the subjects of `from_concept_id`/`to_concept_id`, and `concept_mastery.concept_id` vs. the enrollment's subject.

Fix with composite FKs: add `UNIQUE (id, user_id)` to `learner_subjects` and reference `(learner_subject_id, user_id)` from the children. Cheap, and it makes a whole class of bug unrepresentable.

### C10. `subject_version` freezes a number, not a graph

§6.2/§6.5 promise that pinning `subject_version` means "a mid-course reshuffle does not disorient someone already partway through." But `concepts` and `concept_edges` have no version column and no historical rows — a learner pinned to v1 still reads the current graph. The integer records *which* version they enrolled at; it doesn't deliver the stated guarantee. Either version the nodes and edges, or restate the guarantee as "we can detect that the graph moved and offer a migration" (which the `graph_migration_needed` flag already does).

### C11. `segment_index` in JSONB is ordered on, which §3 forbids

§8's lecture retrieval does `ORDER BY (ca.metadata->>'segment_index')::INT`. §3's own rule: "if a query would ever want to `WHERE` on a value, it is a column, not JSONB." Promote it. That same query also multiplies artifact rows by their citations (`LEFT JOIN content_citations`) with no grouping, and doesn't exclude superseded artifacts.

Related gap: nothing prevents two `status = 'active'` lecture segments for the same `(concept_id, kind, stance, segment_index)`. A partial unique index would make the "current version" genuinely single-valued.

### C12. Soft-deleted users hold their email hostage

`users.email` is `CITEXT UNIQUE` with no partial predicate, and §10 keeps the row for 30 days after account close. A user who deletes and re-registers is blocked for a month with a unique-violation. Use `CREATE UNIQUE INDEX … ON users (email) WHERE deleted_at IS NULL` (which also subsumes the existing `idx_users_email_active`), and scramble the email on soft delete.

### C13. Naming collision: `reviewer`

`agent_identity.reviewer` is the FSRS scheduling agent. `users.role = 'reviewer'` is the human content reviewer. §6.13's `content_review_queue` and §6.9's `review_cards`/`review_events` then use "review" for two unrelated things, and `review_events` is spaced-repetition history while `content_review_queue` is human QA. Rename one side now — it's free today and expensive after seven specs reference it.

### C14. Smaller items

- **`retrieval_checks.based_on_session_id`** is `NOT NULL … ON DELETE CASCADE`. Under the 2-year `learning_sessions` retention, aging out an old session deletes the *newer* session's retrieval check. Make it nullable + `SET NULL`.
- **Portfolio hash chain has no total order.** §6.10 describes a per-learner chain, but the only ordering is `created_at`/UUIDv7; concurrent inserts fork it silently, and `parent_item_id ON DELETE SET NULL` breaks it without a trace. Add `chain_index INT` with `UNIQUE (learner_subject_id, chain_index)`.
- **PgBouncer + SQLAlchemy.** §4 says nothing in the query patterns relies on session-scoped features — true of the queries, but asyncpg/SQLAlchemy use implicit prepared statements by default and need `statement_cache_size=0` (or PgBouncer ≥1.21 configured for prepared statements). It's the standard trap; worth one line.
- **`review_cards` are never created.** §6.9 says "every concept the learner has any evidence for gets a `review_cards` row," but nothing specifies what creates them. Application responsibility is fine — name the trigger point.
- **Missing CHECKs** on `review_cards`: `difficulty` is commented "1..10" and `retrievability` is a probability; neither is constrained.
- **`assessment_attempts.passed` is a nullable BOOLEAN**, against §5's "a nullable boolean is nearly always a modeling mistake." Here it's justified (ungraded), so note the exception.
- **Grade → score aggregation is unspecified.** `assessment_responses.grade ∈ {0,1,2}`, `rubric_criteria.weight ∈ {1,2,3}`, `assessment_attempts.score ∈ [0,1]`. `compute_pass` needs the weighting rule and it isn't anywhere.
- **`pg_trgm` is created but unused** — §4 says it's "for trigram indexes on titles," §7 defers the only trigram index to v1.1. Harmless; just inconsistent.
- **`end_reason` CHECK** includes `OR end_reason IS NULL`, which is redundant (a CHECK evaluating to NULL passes).
- **`content_citations` UNIQUE (artifact_id, source_chunk_id)** prevents citing one chunk twice with different `quoted_span`s.
- **Table creation order** isn't stated and isn't inferable from §6's section order: `concept_sources` references `sources` (§6.3, later), `mastery_events` references `learning_sessions` (§6.6, later). No true cycles, but migration `0001` needs an explicit order.

---

## What I'd do before writing migration 0001

1. Fix `uuid_generate_v7()` (A1) — nothing works until this is right.
2. Rewrite §10's erasure procedure as an ordered step list that the FK graph can actually execute, and pick a strategy for the retained rows (A2).
3. Add the missing timestamp columns and triggers, and either add the ~20 FK indexes or narrow the §7/§14 claims (A3, A5).
4. Decide `p_known_decayed`-at-read vs. daily job before any gating code exists (C1) — this is the one that gets expensive to change later.
5. Resolve the key-points and hypothesis leaks by moving §11 to column-level rules (C5), and split learner read from learner write (C6).
6. Add `user_id` to `agent_traces` and route the four orphaned cost columns into `cost_ledger` (C3).

Everything in section B is a documentation fix, but B4 and B7 matter more than they look: six more specs are going to be written against this one, and a dangling `concepts.module_slug` or an unlisted retention policy is exactly the kind of thing that gets silently invented differently in two places.

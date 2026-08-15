# Divergences from Data Layer Specification v1.1

The implementation now targets `spec/Sub System 1 - Data Layer/01-data-layer-v1.1.md`.
This file records where it still differs, and why. It replaces `DEVIATIONS.md`,
which tracked v1.0 — most of what that document listed has since been ratified
into the spec and is no longer a divergence at all.

Finding IDs (A1–C14) refer to `01-data-layer-review.md`; V-numbers are new
divergences introduced against v1.1.

---

## Ratified — no longer divergences

v1.1 folded these in. The code was already correct and now matches the spec
text; the notes exist so nobody re-opens a settled question.

| ID | What v1.1 adopted |
|---|---|
| A1 | `uuid_generate_v7()` producing 32 characters; `pg_uuidv7` preferred, `uuid-ossp` correctly disclaimed. Migration 0006 adopts v1.1's exact body. |
| A2 | Nullable `SET NULL` owner columns on `assessment_attempts` and `cost_ledger`; anonymise-then-cascade erasure. |
| A3 (part) | `updated_at` on `session_summaries` and `retrieval_checks` — migration 0005. |
| A4 | Session-cost trigger moved to `agent_traces`, renamed `accumulate_session_cost()` — migration 0006. |
| A5 | Foreign-key indexes on all twenty columns; §7 rewritten to describe an invariant the schema meets. |
| B4 | `concepts.module_slug` via migration 0002. |
| C3 | The widened cost ledger — five categories, TTL-split cache writes. Migration 0004. |
| C10 | §6.2 now states the graph-versioning limitation plainly rather than promising immutability. |
| — | §14 reframed around the offline-first test tiering this build introduced. |

---

## Decision on V1–V3: no v1.2

**Decided 15 August 2026.** The three defects below are fixed in code and
remain in the specification. They will be folded into the next spec revision
rather than triggering one of their own — subsystem 2 will force a revision
soon enough, because agent runtime always surfaces schema needs.

The reasoning is severity, not convenience:

- **V1 and V3 fail loudly or not at all.** A subsystem-2 implementation written
  from the stale §6.6 emits a single `cache_write_tokens` and errors on the
  first insert against a column that does not exist. The system account is an
  internal detail of the roll-up that nothing outside the data layer touches.
- **V2 is the only silent one** — a content pipeline written from §6.4 never
  sets `generated_for_session_id`, so cost books to the system account with no
  error and a correct total. That is a code problem, and it has a code fix:
  `roll_up_day` returns `unattributed_content` so the misattribution is
  reported rather than absorbed. Watch that number.

Three items, two self-announcing, is not worth a revision and a re-ratification
cycle. Revisit when the next revision happens anyway.

---

## Open divergences

### V1 — `agent_traces` splits cache-write tokens; v1.1's table does not

**Spec:** §6.12 gives `cost_ledger` `cache_write_5m_tokens` and
`cache_write_1h_tokens`, and explains why: "pricing differs between 5-minute
and 1-hour writes; keeping them in one column would silently double-count or
under-count." But §6.6's `agent_traces` still has a single `cache_write_tokens`.

**Consequence:** the ledger's split columns cannot be populated. The roll-up has
one number and two places to put it.

**Built:** `agent_traces` carries the same two columns. The split has to exist
at the source or it cannot exist downstream.

**For the spec:** apply the §6.12 reasoning to §6.6.

### V2 — `content_artifacts.generated_for_session_id` added

**Spec:** §6.12 attributes content-generation cost to "the learner in whose
session the generation was triggered", falling back to the system account for
Curator pre-generation. §6.4's `content_artifacts` has no session or user
column — there is no join path, so the rule cannot be implemented.

**Built:** a nullable `generated_for_session_id` FK to `learning_sessions`,
`ON DELETE SET NULL` (an artifact outlives the session that prompted it).
NULL means pre-generation and routes the cost to the system account.
Migration 0007.

### V3 — the system synthetic user is defined

**Spec:** §6.12 refers to "the `system` synthetic user" three times without
defining one anywhere in §6.

**Consequence:** unattributable cost has nowhere to go. It cannot be booked to
NULL, because §6.12 also reserves that — "Rows with `user_id IS NULL` represent
post-erasure aggregates; the daily job never inserts a NULL owner directly."
Overloading NULL would make an erased learner's spend indistinguishable from
shared infrastructure cost.

**Built:** a reserved account at `00000000-0000-7000-8000-000000000001`, seeded
by migration 0008, with no password and no auth sessions so it cannot be logged
into. `studium.models.identity.SYSTEM_USER_ID`.

### V4 — `content_artifacts` and `learning_sessions` carry timestamps

**Spec:** v1.1 added `updated_at` to `session_summaries` and `retrieval_checks`
(A3) but left these two without `created_at`/`updated_at`.

**Consequence:** both are mutable — `status`, `reviewed_by`, `superseded_by`,
`ended_at`, `total_cost_usd` — so §14's "every mutable table has an `updated_at`
trigger" still fails on them, and §9's optimistic-concurrency scheme reads an
`updated_at` on `content_artifacts` that v1.1 does not define.

**Built:** both columns and both triggers, as in 0001. Removing them would break
two things the spec still asks for.

### V5 — `learning_sessions` has no summary columns

**Spec:** §6.6 keeps `summary`, `key_points` and `open_threads` on
`learning_sessions`, and §6.11 keeps the same three on `session_summaries`.

**Consequence:** two sources of truth for one value, with no stated precedence.

**Built:** they live only on `session_summaries` — which is what §8's
session-start query actually reads, and the only one of the two that can be
regenerated.

### V6 — the unique index on `users.email` is partial

**Spec:** §6.1 has `email CITEXT UNIQUE NOT NULL` plus a separate partial index.

**Consequence:** a soft-deleted account holds its address for the full 30-day
dispute window, so a learner who erases and changes their mind cannot
re-register with the same email until it expires.

**Built:** one partial unique index `WHERE deleted_at IS NULL`, which subsumes
the spec's `idx_users_email_active`. This is what lets `erase_user` leave the
address intact — the dispute window needs the identity to be recoverable — while
still freeing it immediately.

### V7 — hashes are `TEXT` with a length check, not `CHAR(64)`

**Spec:** §5 says "TEXT for everything. No `VARCHAR(n)`"; §6 then uses
`CHAR(64)` for four hash columns.

**Built:** `TEXT` + `CHECK (length(x) = 64)`. `CHAR` is blank-padded and
compares by the padded value, which is a live hazard for hash equality.

### V8 — gating recomputes decay rather than reading it

**Spec:** §8's unlock query filters on the stored `p_known_decayed`, refreshed
by a daily job; `idx_concept_mastery_learner` sorts on `p_known`.

**Consequence:** gating runs against a value up to 24 hours stale, and stale in
the permissive direction right after evidence lands.

**Built:** `studium.graph.unlock_status` computes decay in SQL from `p_known`
and `last_evidence_at`. The column remains for sorting and reporting, and the
index sorts on it because that is what every consumer reads.
`test_unlock_gating_uses_live_decay` fails if this regresses.

### V9 — turn indices come from the database

**Spec:** §9 assigns `turn_index` from an in-memory per-session counter,
"safe because a session is served by exactly one process at a time (session
affinity in the load balancer)".

**Consequence:** affinity pins a machine, not a process. Two workers on one
machine collide on `UNIQUE (session_id, turn_index)`.

**Built:** `queries.next_turn_index` locks the parent session row and derives
the index in-transaction. (Note it locks the *session*, not the turns: Postgres
rejects `FOR UPDATE` alongside an aggregate, and locking rows you are counting
would not block a concurrent inserter anyway.)

### V10 — smaller additions the spec neither has nor forbids

| What | Why |
|---|---|
| `concepts.metadata` | §6.5 says BKT defaults "come from `concepts.metadata`"; §6.2 does not define the column. |
| `subjects.assessment_threshold` | §6.8 puts per-subject overrides in `subject_metadata`, which holds computed graph statistics. Defaulted to 0.75, so inert until used. |
| Composite FKs `(learner_subject_id, user_id)` | Four tables denormalise both; nothing otherwise stops them disagreeing, which would break the §11 access checks and the erasure sweep. |
| `portfolio_items.chain_index` | §6.10 describes a per-enrollment hash chain with no ordering column; concurrent inserts would fork it silently. |
| `agent_traces.user_id` | The roll-up groups by user and the erasure sweep filters by it; without it both walk two joins over a 90-day table. |
| `retrieval_checks.based_on_session_id` nullable `SET NULL` | As `NOT NULL CASCADE`, ageing out an old session under the 2-year policy deletes the *newer* session's check. |
| `agent_traces` `CHECK (agent <> 'learner')` | §6.6 says the table is one-to-one with turns whose actor is an agent. |
| `review_cards` range checks | §6.9 comments the FSRS ranges (difficulty 1–10, retrievability 0–1) without constraining them. |
| Grade→score aggregation | §6.8 defines grades 0–2 and weights 1–3 but never how they combine, and `compute_pass` needs it. Weighted mean, normalised by weight × max-grade, in `studium.assessment`. |
| Column-level ACL projections | §11 forbids learners reading `rubric_criteria.key_points`, then grants them their own `assessment_responses` — whose `criterion_snapshot` is a copy of exactly that. Same shape for `journal_entries.hypothesis`. |
| `studium_owner` role | §10's retention and erasure jobs must DELETE from the tables 0003 makes append-only. |
| `tests/fixtures/lambda_calculus.py` | §14 names `lambda.py`; `lambda` is a keyword, so the module cannot be imported under that name. |

### V11 — unlock is a direct-parent check by default

§3 calls unlocking "a graph reachability test"; §8's query checks only immediate
parents. Both are implemented — `prerequisites(..., transitive=True)` walks the
full ancestor set — with direct parents as the default, which is correct
whenever the authored graph is transitively complete. §3's wording should be
narrowed to match §8.

### V12 — `segment_index` stays in JSONB

§3 says a value you order by should be a column, and §8 orders lecture segments
by `metadata->>'segment_index'`. Promoting it would pre-empt the Content
Ingestion spec, which owns the per-kind metadata shapes (§16). Left as JSONB,
with a partial unique index so "the current segment" is at least single-valued.

---

## Still open in v1.1, unchanged from the v1.0 review

Not divergences — the implementation follows the spec — but worth carrying
forward:

- **§6.2 `subject_metadata`** still says "maintained by a trigger on `concepts`
  and `concept_edges`" and then, one paragraph later, that it is *not* maintained
  by row triggers. The stored procedure is the operative reading (B3).
- **§9's reconciliation** still describes comparing "the trace count to invoice
  line items from Anthropic". Billing reports aggregate usage by day, model and
  key, not per call; `cost_rollup.reconcile` returns per-model daily totals,
  which is the comparison that can actually be made (C4).
- **§12** still names `.sql` migrations (`0002_module_slug.sql`,
  `0003_grants.sql`) while §13 mandates `NNNN_verb_object.py`.
- **§10's retention table** still omits `retrieval_checks`, `session_summaries`,
  `concept_mastery`, `learner_subjects`, `user_profiles` and `user_budget_caps`
  while claiming to cover every table. `studium.jobs.retention.POLICIES` covers
  them; a test asserts completeness (B7).

# Spec debt

Things the specifications do not yet account for, found by building against
them. Distinct from the `DIVERGENCES*.md` files: those record where the *code*
departs from a spec and why. This records where a *spec* needs to change, and
survives the build that found it.

Each entry names the trigger that should close it, so nothing sits here as a
permanent shrug.

---

## SD1 — the ingestion boundary has no provenance or failure model

**Status:** CLOSED by the subsystem 5 build, 26 August 2026.

`ingestion_review_queue` (migration 0010) is the surface this entry asked for.
Retrieval's `embedding_worker._flag_failures` no longer logs and gives up: it
writes a row targeting `source_chunk_id`, which is what the old table's
`has_target` CHECK could not accept. The general rule the three instances
implied is `studium.ingestion.provenance`, enforced by a Tier 1 test that walks
every registered writer — see ingestion §13 and `DIVERGENCES-INGESTION.md`.

The original entry follows, for the record.

**The pattern.** The schema declares somewhere for provenance to go, and no
writer path can populate it. No error, no constraint violation — a clean
database that is quietly incomplete.

| # | Where | How it showed up |
|---|---|---|
| 1 | `content_artifacts` (data layer V2) | No join path to the session that triggered generation, so §6.12's cost attribution rule could not be implemented as written. Every artifact would have booked to the system account with a correct-looking total. Mitigated by `roll_up_day` returning an `unattributed_content` count — see SD2. |
| 2 | `migrate_from_draft.py` | The v0.4 envelope's `model` and `ingested_at` were dropped, though `content_artifacts` has columns for both. All 76 artifacts would have landed with null attribution, silently. |
| 3 | `content_review_queue` (retrieval S3) | §8 asks that an unembeddable chunk be queued at severity 3. The table's `has_target` CHECK requires an artifact or a session turn, and an ingestion-time failure has neither *by nature*. |

**Why the third one is the useful one.** The first two fail silently — a green
test suite, a clean database, missing data. The third fails loudly, because the
CHECK rejects the row rather than accepting a null. Same root cause, opposite
symptom, and it is the one that makes the shape of the fix obvious:
`content_review_queue` models *content* review. "This source did not fully
ingest" is not content review, and relaxing the CHECK to admit it would make
the table mean two things.

**What should happen.** Subsystem 5 needs its own provenance and failure
surface — somewhere an ingestion run records what it read, what it produced,
what it could not process, and under what licence. Retrieval's
`embedding_worker` currently logs unembeddable chunks at ERROR as a stopgap
(S3); that stopgap should be replaced by a write to whatever subsystem 5
defines, not by a widened CHECK on a subsystem 1 table.

**Trigger to close:** subsystem 5's spec. It is that spec's decision to make.
Deciding it from retrieval would be designing another subsystem's schema from
outside it, which is how the first two instances happened.

---

## SD2 — `unattributed_content` is a counter nobody reads

**Status:** CLOSED by the subsystem 7 build, 26 August 2026.

It has two readers now, which is what the entry asked for:

- **`studium ops cost-report`** prints an "attribution health" line — orphans
  over total costed artifacts in the window — and says, when the ratio is high,
  that the per-user figures above it understate real learner spend.
- **`alerts.unattributed_content`**, a §8 condition on the *ratio* rather than
  the count. Curator pre-generation legitimately has no session and is expected
  to be a standing fraction; alerting on the absolute number would fire on a
  productive week of authoring. What is worth a look is the fraction moving,
  which is what a pipeline that stopped populating
  `content_artifacts.generated_for_session_id` looks like.

The threshold (20% over 7 days) is a first guess. It has never fired against
real data, because there is no real data yet.

The original entry follows, for the record.

**Status:** open, low severity, needs a home rather than a fix.

`studium.jobs.cost_rollup.roll_up_day` returns an `unattributed_content` count:
the number of `content_artifacts` rows whose cost booked to the system account
because no session could be attributed. It exists as the mitigation for SD1
instance 1 — the signal that the gap is being hit in practice rather than
merely being possible.

The only production caller — `session.lifecycle.close_session` — discarded the
returned dict entirely, and no threshold or alert was attached. A mitigation
that reports into a void is indistinguishable from no mitigation, and the
failure it guards against is exactly the kind that looks fine until someone
audits a bill. Its only reader was a single integrity test.

**Partly closed (19 August 2026).** `close_session` now logs a warning when the
count is non-zero. That is the floor: it makes the gap discoverable by anyone
reading logs, and no more than that.

**What should still happen.** Either a threshold that flags when the ratio of
unattributed to attributed content cost crosses some bound, or a line on
whatever operational dashboard subsystem 7 defines. A warning nobody greps for
is only marginally better than a discarded return value.

**Trigger to close:** subsystem 7 (infrastructure), which owns operational
visibility. Until then, anyone touching cost attribution should check this
figure by hand before believing a cost report.

---

## SD4 — §9's latency budgets were measured against the wrong bottleneck

**Status:** the defect it exposed is fixed. The spec text is still misleading.

§9 budgets vector search at "under 50ms with warm caches" for an MVP corpus and
attributes the cost to the index: "vector search remains sub-100ms courtesy of
HNSW". Measuring it at MVP scale (9,990 chunks) put vector search at **46ms
median** — nominally passing, with 8% headroom.

`EXPLAIN ANALYZE` reported 2.8ms of actual execution. The other 43ms was
psycopg sending the 1024-dimension query vector as a ~15KB text literal for
Postgres to parse on arrival. None of it was the index, and none of it scaled
with corpus size.

Registering pgvector's binary adapter (`studium.db._register_vector_type`) took
the same measurement to **3.3ms median**, a 14x improvement in the path a caller
actually experiences.

**Why this is spec debt and not just a fixed bug.** §9's numbers were set
against an implementation detail nobody had measured, so they encode the wrong
model of where retrieval spends time. Two consequences:

1. The MVP budget looked comfortable at 46/50ms when it was one refactor away
   from failing for a reason unrelated to retrieval.
2. **Keyword search is the binding constraint, not vector search** — and it is
   the half §9 treats as cheap.

Measured, after the transport fix:

| Corpus | Vector median / p95 | Keyword median / p95 | §9 budget |
|---|---|---|---|
| MVP — 10k rows, 10k in-subject | 3.3 / 3.8 ms | 25.2 / 48.3 ms | 50 ms |
| Classroom — 150k rows, 15k in-subject | 4.2 / 4.7 ms | 74.0 / 118.3 ms | 150 ms |

Both tiers pass. But across a 15x increase in table size, vector search moved
27% (HNSW doing exactly what §9 says it does) while keyword search tripled and
its p95 reached 79% of the budget. §9 only anticipates keyword trouble at
university tier — "may exceed 200ms and warrants partitioning by subject" — and
the trend here says that arrives sooner than the spec expects, not later.

**What should happen.** Three things in §9:

- Re-baseline the per-modality budgets against measured numbers.
- Drop the attribution of vector latency to HNSW at MVP scale. HNSW accounted
  for 6% of it; the rest was parameter marshalling, and now it is ~3ms total.
- Move the keyword-partitioning concern earlier than university tier, or state
  what the classroom-tier p95 is expected to be.

**Trigger to close:** the next revision of the retrieval spec. Re-run
`scripts/measure_retrieval.py --tier mvp` and `--tier classroom` for current
figures. University tier is defined in that script and has not been run: two
million 1024-dimension vectors is several gigabytes plus index.

---

## SD3 — §19 open question 1 cannot be answered before subsystem 5

**Status:** CLOSED, 26 August 2026. Measured against the real corpus through
pdfplumber. The numbers are in SD7, which is what the measurement actually
turned up; the short version is that retrieval's 400-token target and 600-token
maximum survive contact with the corpus — median 427, quartiles 344 and 501
over the 241-page Michaelson book, nothing at the ceiling.

The trigger named below ("subsystem 5's extractor") is met.

The original entry follows, for the record.

Retrieval §19 asks whether the 400-token chunk target and 600-token maximum
survive contact with real sources, and expects the answer from measuring a real
corpus.

Running the chunker over the Michaelson book (241 pages) produced a median of
441 tokens against the 400 target, with nothing over the maximum — which reads
like a clean answer, and is not one. Three successive extraction heuristics over
the *same* PDF, feeding the *same* chunker, produced medians of 440, 113 and
124. The chunk-size distribution is dominated by extraction quality, not by the
chunker's parameters.

So the honest reading is: the targets are not obviously wrong, and re-tuning
them now would be fitting to artefacts of `pypdf` plus a diagnostic script's
paragraph heuristics rather than to real text.

**Trigger to close:** subsystem 5's extractor. Re-run
`scripts/chunk_diagnostics.py` against its output — the script is written to be
re-pointed — and read the distribution then.

**What the measurement did settle**, independent of extraction quality:

- Two real chunker defects, both now fixed and pinned by regression test: the
  overlap escaped the size check (7.6% of chunks over the maximum), and
  typographic ligatures were silently removing text from the keyword index.
- Real extraction artefacts subsystem 5 will have to handle, measured over 241
  pages: 716 ligatures, 355 suspicious line joins, 39 bare page-number lines, 8
  hyphenated line breaks, and no blank lines anywhere — paragraph boundaries are
  not recoverable from whitespace in this source.

---

## SD5 — a generated artifact's id never reaches the client that renders it

**Status: closed, 22 August 2026.** The `end` chunk carries `artifact_id`.
Verified against real Postgres and real Anthropic
(`tests/online/test_provenance_paid.py`): the id on the chunk is the primary key
of a `content_artifacts` row, `session_turns.artifact_id` holds the same link,
and the id resolves through `GET /api/artifacts/{id}/citations`.

**What the fix was, and the one thing it changed beyond a field.** The ordering
this entry anticipated — "the `end` chunk cannot be emitted until [the effects]
have been" — became the load-bearing part. The Orchestrator now *holds* the
terminal chunk until `apply_effects` commits, then emits it with the id the
batch produced (`_handle_conversational`, `_handle_primitive`). That costs one
transaction of latency after the prose has finished arriving, and it is what
makes the id available at all. `apply_effects` returns an `AppliedEffects`
rather than a list of kind names, because the id had nowhere to travel.

`session_turns.artifact_id` is written too. That column has been in the schema
since data layer §6.6 and nothing had ever populated it — an SD1-shaped gap,
found while fixing this one. The chunk is delivered once; the row is what
survives a page reload.

**Still true, and now the binding constraint.** A resolvable id is not a
resolved citation. Whether the card has anything to show depends on the concept
being curated — `concept_sources` pointers, or embeddings once a vector provider
is configured. The endpoint answering with an empty list for an ungrounded
artifact is correct behaviour (retrieval §12), not a regression.

**Trigger the closure leaves behind:** `studium-web/e2e/tier3.real.spec.ts`'s
citation case now asserts the *resolved* card rather than the placeholder copy,
so a regression reports itself the same way the gap did.

---

## SD5 (original entry, for the record)

**Status:** superseded by the above. Found building subsystem 4.

Frontend §10 builds citation rendering on retrieval §12's endpoint,
`GET /api/artifacts/{artifact_id}/citations`. The endpoint works. **The client
has no way to learn the `artifact_id`**, so nothing can call it.

The gap is a consequence of a decision that is right on its own terms. Agent
runtime §6 has agents *return* side effects rather than apply them, and the
Orchestrator applies the batch after the call completes — which is what makes a
retry safe. So when the Lecturer emits its `record_content_artifact` ToolEffect,
the artifact row does not exist yet and has no id. By the time it does, the
chunk carrying it has already gone down the wire.

What the client receives:

| Chunk | Carries | Has an artifact id? |
|---|---|---|
| `tool_effect` | the effect's *payload* — concept, body, stance, model | No. The row is unwritten. |
| `end` (Lecturer) | `turn_id`, `segment_index`, `anchor` | No. |

**Why this is spec debt and not a frontend defect.** Every client-side half is
built and tested — marker parsing, range expansion, the hover card and its
delays, keyboard activation, the retired-source treatment, the full-passage
modal. `lib/api/schemas.ts` already parses `artifact_id` off the `end` payload,
so the day it arrives the cards populate with no frontend change. What is
missing is one field on one chunk, and deciding its shape from subsystem 4 would
be designing subsystem 2's contract from outside it — which is how the three
provenance gaps in SD1 happened.

**What should happen.** After the Orchestrator applies a segment's effects, the
resulting `content_artifacts.id` goes on the `end` chunk. Agent runtime §20's
chunk contract needs the field named, and §6's `StreamChunk` docstring should
say that `end` may carry it.

Worth noting the ordering constraint this implies: effects are applied *after*
the stream closes, so the `end` chunk cannot be emitted until they have been.
That is already true of the Lecturer's flow — effects are yielded before `end`
precisely so a cancelled stream never persists a half-written segment — but it
becomes load-bearing rather than incidental once something downstream depends on
it.

**Consequence while it is open.** Every citation marker in the product renders
and none of them resolves. The frontend says so plainly rather than spinning
(frontend `DIVERGENCES-FRONTEND.md` F3), and its Tier 3 case asserts *which*
message the card shows — so the day the runtime sends the id, that test fails
and reports the gap has closed.

**Trigger to close:** the next revision of the agent runtime spec, or any change
to the Lecturer's `end` payload. `studium-web/e2e/tier3.real.spec.ts` is the
check that notices.

---

## SD6 — §7's transition table is not the runtime's transition table

**Status:** open. Found closing SD5 and F15. Needs spec text, not code.

Three separate defects in one build traced to the same thing: §7's table is
treated as complete, and it is a sketch.

| # | What the table says | What the runtime needed |
|---|---|---|
| 1 | `primitive_invoked` fires from `TUTORIAL` | The palette is on the classroom, so it fires from `LECTURING` and `PAUSED_FOR_QUESTION` too (R13) |
| 2 | `OPENING --context_ready-->` the mode's state | Nothing named who fires `context_ready`, and nobody did (R14) |

The second is the instructive one. §7 defines the event and both of its rows and
never says *when* it happens — §16's opening sequence describes the work in prose
and never names the transition. A table row whose trigger is unstated reads as
implemented, because the row is there. It took a real browser against a real
runtime to notice that no session had ever left `OPENING`.

**Three events are still declared and never fired.** Measured, not assumed —
`grep -oE 'Event\.[A-Z_]+' studium/**/*.py` outside `state_machine.py` returns
eight of the eleven:

| Event | Row it would fire | Consequence of nobody firing it |
|---|---|---|
| `segment_complete` | `LECTURING` → next segment, or the Curator's choice | Sequencing is client-driven: the lecture advances because the client asks for `next`, not because the runtime decides a segment ended |
| `question_resolved` | `PAUSED_FOR_QUESTION` → the resumed state | **A session never leaves `PAUSED_FOR_QUESTION`.** After any interrupt, every later turn routes to `tutor.interruption_response` — including the one behind "Continue lecture" |
| `escalate` | `PAUSED_FOR_QUESTION` → `OFFICE_HOURS` | §8.3 step 7's escalation offer moves no state; the resumption card's "Move to office hours" sends an ordinary turn |

`question_resolved` is the one that most looks like R14 and should be treated
that way — a real interrupted lecture cannot resume as a lecture today. It was
found by the same inventory that produced this entry and is **not fixed here**:
it is outside what closing SD5 and F15 required, and guessing at the resume
trigger is what produced R14's absence in the first place.

`segment_complete` may be right as it stands — the learner's pace rather than
the Lecturer's — but §7 describes a runtime that decides and the code implements
one that responds. One of the two should change and it is not obvious which.

**What should happen.** §7's table needs a fourth column naming the caller that
fires each event, and §16's steps need to name the transitions they perform.
Where the runtime has no caller for a row, that is either a gap to fill or a row
to delete; from the spec alone the two are indistinguishable.

**Trigger to close:** the next revision of the agent runtime spec. Until then,
the grep above is the honest inventory of which rows can fire.

---

## SD7 — a healthy chunk-size distribution says nothing about text quality

**Status:** open as a measurement gap; the specific defect it caught is fixed.

**What happened.** Running the §7.4 measurement against the real corpus for the
first time reported, for the Michaelson book: median chunk size 409 tokens
against a 400-token target, quartiles 334 and 480, coverage 0.99, section
detection 1.00. Every number a reviewer would look at said the extractor was
working well.

The text was unusable. pdfplumber's default word-split tolerance of 3 points is
wider than the inter-word gaps in that book's typesetting, so extraction
returned `Itispossible,however,forboundvariablesindifferentfunctionstohave
thesamename.` — whole paragraphs as single tokens. Postgres `tsvector` indexes
that as one term, so the keyword half of hybrid search matches nothing a
learner would type, and the vector is an embedding of a string that occurs in
no training corpus.

**Why every check missed it.** All three §7.4 measurements are size measurements.
A run-together paragraph has the same token count as the spaced version, falls
in the same chunk-size band, and reports the same coverage ratio against any
token-based reference. Section detection was unaffected because headings are
detected from font size, not from text. The synthetic fixtures could not catch
it either: reportlab spaces its output normally at any tolerance, so the defect
does not exist in a generated PDF.

**Fixed:** `WORD_SPLIT_TOLERANCE = 2` in `studium.ingestion.extract`, which
takes the two lambda calculus sources from 0.087 and 0.066 spaces-per-character
to 0.144 and 0.132, and leaves the Turing and Clojure material unchanged. The
book's median moves to 427 with quartiles 344 and 501.

**Also added:** a `words_run_together` normalisation warning (§8.3 surface) at a
0.11 space ratio, so the next source with this defect reaches a reviewer
instead of a metric.

**What stays open.** The measurement harness still reports only sizes. A
text-quality axis — space density, dictionary-word rate, or a sample a reviewer
actually reads — belongs in it, and belongs in the extractor-comparison
workflow §7.4 describes, because the whole point of that section is choosing
between extractors and the current numbers cannot distinguish good text from
correctly-sized garbage.

**Trigger to close:** the next extractor comparison. If `marker` or
`unstructured` is ever evaluated against pdfplumber, this is the axis that
decides it, and running that comparison on size alone would repeat the mistake.

---

## SD8 — chunks are page-shaped, not idea-shaped

**Status:** open. Two fixes attempted and reverted, with measurements.

`page.extract_text()` joins lines with single newlines and never emits a blank
line — zero blank lines across 700-odd pages of the corpus. Retrieval §7's
preference-2 break point is a blank line, so it never fires: ingestion hands the
chunker one block per page, and chunk boundaries fall where a page happened to
end rather than where one idea stops and the next begins.

**Both available fixes were tried against the real corpus and both made it
worse**, which is why this is spec debt rather than a patch:

| Approach | Michaelson book |
|---|---|
| Page as one block (current) | median 427, q1 344, q3 501 |
| First-line indent marks a paragraph | 6,619 paragraphs, median 7 |
| Vertical gap > 1.35× modal line spacing | 817 chunks, median 27 |

Indentation fails because typeset academic material indents constantly —
centred equations, displayed quotations, list items, hanging indents. The gap
heuristic fails for the same underlying reason in a different guise: displayed
lambda expressions carry extra leading, so every one becomes its own paragraph.

A real fix has to tell a paragraph break from a display-math gap, which needs
font and indent context together rather than either alone.
`studium.ingestion.extract._page_text_with_paragraphs` is kept as a named seam
with one call site so a future attempt has somewhere to go, and so the
measurement harness compares like with like across the change.

**Trigger to close:** evidence that it matters. The current distribution is
close to target and the corpus is small; if retrieval quality work later traces
a ranking problem to chunk boundaries falling mid-argument, this is the cause.

---

## SD9 — evaluation runs share the learner's budget cap, and one run can exhaust it

**Status:** CLOSED by the subsystem 7 build, 26 August 2026 — and the collision
it described was not the one that was actually there.

**The premise was false, and had been since before the entry was written.**
SD9 reasoned from evaluation §15.3's reverted column and concluded that a
regression run "draws on the reviewer's ordinary daily budget". Measured
against the code, it does not. `eval.runner._system_user` books every trace of
a live run to the system account, and migration 0008 seeded that account with
$1,000 daily / $20,000 monthly caps. **Option 2 — "attribute scheduled and CI
runs to the system user" — was already the implementation.** It arrived while
fixing E14 (a run's traces need a real `learning_sessions` row, and the session
had to belong to *someone*), not as an answer to this question, which is why
nobody had connected the two.

Worth stating plainly: this entry was wrong for eleven days and every mitigation
it described was in place, so nothing was at risk in the meantime. It is
recorded rather than deleted because reasoning from a spec's text about what the
code does is how it happened, and that is a repeatable mistake.

**What was genuinely missing is enforcement, and that half was real.** Those
caps were a number nobody read. `budget_gate.pre_flight_check` is called only
from the Orchestrator's session path; an evaluation run invokes agents directly,
so **no cap of any kind constrained a run**. A human typing yes to a printed
estimate was the only limit — which is precisely what §13.3's CI job removes.

So the fix is a gate rather than a re-attribution:

- `eval.runner.budget_preflight` checks the harness account's daily cap against
  the estimated spend and raises `BudgetExceededError` before anything is spent.
- `studium eval run` and `studium eval gate` call it *before* the confirmation
  prompt, because `--yes` is how CI drives that path and the prompt is not a
  gate there.
- `.github/workflows/prompt-regression.yml` runs the same check as its own
  named step, so a blocked budget reads as a blocked budget rather than as a
  non-zero exit from a command that also does five other things.

**The trigger this entry named is met**: §13.3's affected-dataset gate is wired
(§10.2). The weekly full-suite run is deliberately *not* — see
DIVERGENCES-INFRASTRUCTURE (N10).

The original entry follows, for the record. Read its table as a description of
what the spec implies rather than of what the code did.

**Status:** open. Trigger met the moment §13.3's CI regression job is wired.

**The pattern.** A spec revises itself mid-section, reverts the revision, and
the revert takes something with it that the rest of the document still needs.

Evaluation §15.2 and §15.3 are visibly a train of thought in the shipped
document. A `cost_evaluation_usd` column on `cost_ledger` is added; the
addition count is corrected from four to five; a `daily_evaluation_usd_max`
column on `user_budget_caps` is added, making six; and then both are reverted —
"rather than adding two separate columns ... the evaluation cost can attribute
to `cost_agent_usd` since it's still agent calls ... Reverting: no new ledger
columns from this subsystem. Four additions total, not six."

**The ledger half of that revert is right.** Evaluation calls *are* agent
calls, and "is this evaluation or real learner work" is answerable from
`evaluation_runs` without a ledger column. That is what is implemented.

**The budget half does not survive the document's own numbers.**

| | |
|---|---|
| `user_budget_caps.daily_soft_usd` (default) | $5.00 |
| `user_budget_caps.daily_hard_usd` (default) | $8.00 |
| A full regression across all agents (§7.4) | $5–15 |
| Scheduled full-suite runs (§13.3) | weekly |
| Affected regressions (§13.3) | every push to `yannis` / `main` |

With no separate cap, a regression run draws on the reviewer's ordinary daily
budget. **A single full-suite run can cross the daily hard cap**, at which
point `budget_gate.pre_flight_check` raises `BudgetExceededError` and the
reviewer cannot open a learning session — for reasons that have nothing to do
with learning. The weekly scheduled run does this reliably; a busy day of
prompt changes does it faster.

**Why it is not fixed here.** §15.3 explicitly declined the column. Adding a
seventh addition to the v1.2 batch after the spec talked itself out of it would
be building around a decision rather than raising it, and the decision is a
real one with two defensible answers:

1. A separate `daily_evaluation_usd_max` cap, which is what §15.3 drafted
   before reverting. Costs a column; makes the two budgets independent.
2. Attribute scheduled and CI runs to the system user (§6.12) rather than to
   the triggering human, and give that account its own caps. Costs no schema
   change; §15.1 already routes scheduled runs there, so this is a narrowing of
   `evaluation_runs.triggered_by` semantics rather than an addition. It leaves
   a reviewer's *manual* runs on their own budget, which is arguably correct —
   those are their choice to spend.

Option 2 is cheaper and is probably right, but "probably" is not the standard
for a decision that determines whether the reviewer can work.

**What is in place meanwhile.** `studium eval run` and `eval gate` print the
entry count and an estimated cost before spending anything, and refuse a
non-interactive stdin without `--yes`. `.github/workflows/evaluation.yml`
deliberately does not wire §13.3's regression job and says why. So the collision
is reachable only by a human who was told the price and typed yes.

**Trigger to close:** wiring §13.3's CI regression job, which is the first
thing that spends this budget without a human present. That is subsystem 7's
work — it owns CI secrets and the deploy pipeline — so the decision lands
naturally in its scope. See DIVERGENCES-EVALUATION (E13).

---

## SD10 — the operational calendar has no keeper

**Status:** open. Found building subsystem 7. Needs a decision, not code.

The infrastructure spec puts nine recurring obligations on the operator, and
**exactly one of them has a mechanism**:

| Cadence | Task | § | Who remembers |
|---|---|---|---|
| Daily 02:00 UTC | Retention pass | §12.1 | the scheduler |
| Daily | Cost trend check | §13.3 | nobody |
| Weekly | Full evaluation regression | evaluation §13.3 | nobody |
| Monthly | Reconcile invoices against `cost-report` | §13.2, §16 | nobody |
| Quarterly | API key rotation | §6.3 | nobody |
| Quarterly | Restore drill | §9.3 | nobody |
| Quarterly | Alert-accuracy review | §16 Tier 3 | nobody |
| Annually | Signing key rotation | §11.2 | nobody |
| +90 days after a rotation | Destroy the retired private key | §11.2 step 6 | nobody |

§3 is unusually firm about the first column: "Rotation is scheduled, not
reactive. Signing keys rotate on a calendar (annually) whether or not there's a
reason. ... skipping rotation 'because nothing's wrong' is how the muscle
atrophies."

Nothing in the built system contradicts that and nothing supports it either.
Every row but the first depends on a human remembering, and the spec's own
answer to what happens when the operator's attention is the bottleneck is §18's
cross-cutting question 2 — which is about *alert delivery*, not about work
nobody was alerted to in the first place.

**Two rows are worse than the rest.**

- **The restore drill** (§9.3) is the one §3 says exists because "backup
  mechanisms decay silently and only exercise proves they still work". A drill
  nobody schedules is the state that entry describes.
- **Destroying the retired private key after 90 days** (§11.2 step 6) is a
  one-off follow-up to an event a year earlier. It is the single most
  forgettable item on the list and the one whose omission leaves a usable
  signing key in a password manager indefinitely.

**Partly mitigated.** `docs/ops/README.md` carries the calendar as a table, and
`studium ops keys list` flags a signing key past §11.2's annual interval as
OVERDUE — which is the only one of the eight that reports on itself, and only
if someone runs the command. `alerts.retention_worker_stale` covers the row
that *does* have a mechanism, which is the row least in need of it.

**What should happen.** One of:

1. Real calendar reminders, outside this system. Free, immediate, and exactly
   as reliable as the person who set them up.
2. A `studium ops calendar` command that reads the last occurrence of each task
   from the database — `retention_actions.ran_at`, `signing_keys.activated_at`,
   `evaluation_runs.completed_at`, the drill log's newest entry — and reports
   what is overdue. Two of the eight have no durable record at all (invoice
   reconciliation, the alert-accuracy review), so this needs somewhere to write
   "I did this on date X", which is a small table nobody has specified.
3. Accept that a single-operator MVP runs on memory, and say so in the spec so
   the next reader is not misled by §3's confidence.

Option 3 is the honest MVP answer and option 2 is the one that survives a
second operator. The choice depends on whether §8.3's "when a second operator
joins" is months or years away, which is not a question this build can answer.

**Trigger to close:** the next revision of the infrastructure spec, or the
first missed rotation — whichever comes first. If it is the second, the entry
will have earned its place.

---

## SD11 — §16's Tier 2 deploy lines cannot run in CI, and nothing else runs them

**Status:** open, and partly by design. Found building subsystem 7.

§16 puts five items in Tier 2, "requires a scratch environment":

| Item | Runs where |
|---|---|
| Full deploy pipeline: builds, migrates, deploys, runs smoke test | **nowhere** |
| Rollback: deploy, verify, roll back, verify previous version restored | **nowhere** |
| Backup restore drill (quarterly, §9.3) | nowhere yet — see SD10 |
| Secret rotation (quarterly, §6.3) | nowhere yet — see SD10 |
| Retention worker against a seeded database | `tests/online/test_ops_db.py` |

The first two cannot run in GitHub Actions as the project is configured, and
the reason is a decision rather than an oversight: §6.2 scopes the Fly
deployment token to authorised workstations, and a CI job that can deploy is a
CI job that can deploy *anything a compromised action can build*. Adding a
deploy token to the repository to satisfy a testing line would trade a real
security property for a green check.

So `.github/workflows/infrastructure.yml` builds both images and stops, and
says so in its header. What is untested as a result:

- that `flyctl deploy` succeeds against the real platform;
- that the release command's migration failure genuinely aborts the deploy and
  leaves the previous version serving (§10.3's central promise);
- that `flyctl releases rollback` restores the previous version in ~30 seconds
  (§10.4's number);
- that the volume mount, the private-network DNS name, and the health check
  behave as `fly.toml` describes.

Every one of those is asserted *as configuration* by
`tests/ops/test_deployment.py` and *as behaviour* by nobody.

**What should happen.** A scratch Fly app — `studium-backend-scratch` — and a
documented by-hand run of the deploy/rollback pair against it, recorded in
`docs/ops/drill-log.md` alongside the restore drills. That costs a few dollars
a month and one afternoon, and it is the only way §10.3's promise gets checked
before the day it matters.

**Trigger to close:** the first real deployment. Whoever runs it is performing
this test whether or not they record it; the only question is whether the
result is written down.

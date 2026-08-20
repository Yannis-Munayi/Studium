# Spec debt

Things the specifications do not yet account for, found by building against
them. Distinct from the `DIVERGENCES*.md` files: those record where the *code*
departs from a spec and why. This records where a *spec* needs to change, and
survives the build that found it.

Each entry names the trigger that should close it, so nothing sits here as a
permanent shrug.

---

## SD1 — the ingestion boundary has no provenance or failure model

**Status:** trigger met. Three instances. Ready to spec against subsystem 5.

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

**Status:** open. Answered as far as it currently can be.

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

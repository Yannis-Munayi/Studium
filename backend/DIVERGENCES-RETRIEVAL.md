# Divergences from Retrieval Specification v1.0

The implementation targets
`spec/Sub System 3 - Retrieval/03-retrieval-v1.0.md`.
This file records where it differs, and why. It is the third of its kind, after
`DIVERGENCES.md` (data layer) and `DIVERGENCES-RUNTIME.md` (agent runtime); the
S-series numbering keeps the three sets distinguishable in code comments.

Each entry says what the spec asks for, what the code does, and what would have
gone wrong the other way. Three of them (S1, S2, S3) are cases where following
the spec literally produces something that either cannot run or is quietly
wrong — those are marked **load-bearing**.

---

## Load-bearing divergences

### S1 — the concept-graph neighbourhood follows the data layer's edge direction

**Spec.** §10 item 2: "All concepts with a `prerequisite` edge *from* this
concept in `concept_edges` (things this concept depends on)." Item 3 says the
same for `dependency` edges: "*from* this concept (things this concept
enables)."

**Reality.** Under subsystem 1's direction those clauses describe opposite
sets. `concept_edges` stores the prerequisite as `from_concept_id` and the
dependent as `to_concept_id` — `studium.graph.prerequisites` reads
`WHERE to_concept_id = :concept_id` to find what a concept requires. So an edge
*from* concept A names something A **enables**, not something A depends on.
Item 2's preposition and its parenthetical cannot both be satisfied.

Worse, items 2 and 3 as written are the same query — "a prerequisite edge from
this concept" and "a dependency edge from this concept" differ only in `kind` —
so read literally the neighbourhood contains what the concept enables, twice,
and nothing it depends on.

**Code.** `search.concept_neighborhood` follows the parentheticals, which state
the intent: both directions are walked, so the neighbourhood contains what the
concept depends on *and* what it enables. Undirected kinds
(`generalization`, `related`, `application`) are walked both ways per item 4.

**If followed literally.** Retrieving for "beta-reduction" would pull curated
pointers from Church-Rosser and the Y-combinator — the advanced material built
*on* it — while excluding lambda syntax and alpha-equivalence, the material it
is built *from*. A learner meeting beta-reduction for the first time would be
grounded in the concepts they have not reached yet. It would look like working
retrieval, because it returns plausible same-subject passages.

---

### S2 — thin grounding is detected by retrieval and written by the caller

**Spec.** §13: "the retrieval result carries `thin_grounding=True` and a
`review_queue_id` pointing at a newly-created `content_review_queue` row", with
an `INSERT` whose first column is commented `session_turn_id -- if the
retrieval was invoked from an agent turn`.

**Reality.** `content_review_queue` has a CHECK named `has_target`:
`artifact_id IS NOT NULL OR session_turn_id IS NOT NULL`. At the moment the
Lecturer grounds a segment, neither exists — the artifact has not been
generated and the turn row is written by the client wrapper *after* the model
call, which has not happened yet. The insert §13 specifies would be rejected by
the schema on the most common path it describes, and the spec's own "if"
acknowledges the gap without resolving it.

**Code.** Retrieval detects and reports: `RetrievalResult.thin_grounding` plus
a `thin_grounding_reason` carrying the numbers, the concept, and the query.
The caller's effect batch writes the row —
`Lecturer._segment_effects` emits a `flag_for_review` effect, which lands in
the same transaction as the artifact and its citations (agent runtime §8's
all-or-nothing guarantee).

`service.flag_thin_grounding` covers the case §13's "if" contemplates: a caller
that *does* already have a turn passes `session_turn_id` and gets a
`review_queue_id` back.

**If followed literally.** Every thin-grounding event on the Lecturer path
would raise an `IntegrityError` inside retrieval. Since §6 promises
`retrieve_passages` never raises, that error would have to be swallowed — and
the flag §13 calls "a first-class failure" would be silently dropped in exactly
the case it exists for.

**Secondary effect worth knowing.** Only the Lecturer flags. The Tutor and
Reviewer read the same verdict and do not write a row, because one
under-curated concept should produce one queue item rather than one per
tutorial turn. §13's 15% v1.1 threshold should be read against segments, not
against retrieval calls.

---

### S3 — an unembeddable chunk is logged, not queued

**Spec.** §8: a chunk that fails embedding after the retry is "marked with a
`content_review_queue` entry at severity 3".

**Reality.** The same `has_target` CHECK. An embedding failure happens during
ingestion: there is no artifact and no session turn, and there cannot be — no
learner is involved. The row §8 asks for has no legal target.

**Code.** `embedding_worker._flag_failures` logs at ERROR with the chunk id and
the reason. The chunk stays unembedded and therefore invisible to vector
search, which §8 itself establishes is not an error condition — hybrid search's
keyword half still finds it.

**Data-layer v1.2 candidate.** `content_review_queue` models *content* review,
and an ingestion-time failure is not content review. Either the CHECK relaxes
to allow a `source_chunk_id` target, or ingestion (subsystem 5) grows its own
surface for "this source did not fully ingest". The second is more likely
right, and it is subsystem 5's call — recorded here rather than decided.

This is the **third** instance of the ingestion-side provenance gap: the schema
models something no writer can populate. A third instance was the standing
trigger for raising it as a spec concern rather than another per-site
workaround, so it is now recorded in [SPEC_DEBT.md](SPEC_DEBT.md) as **SD1**,
with the shape of the fix and the trigger that closes it. The ERROR log here is
a stopgap and is labelled as one.

---

## Ordinary divergences

### S4 — `retrieve_passages` returns `RetrievalResult`, not `list[Passage]`

The agent runtime shipped `retrieve_passages(...) -> list[Passage]`, because
subsystem 2 §25 treats retrieval as a black box returning ranked passages.
Retrieval §6 defines the return as `RetrievalResult`, and it has to: the
thin-grounding verdict, the query that was actually used, and the degradation
flag all have to reach the caller, and §13 makes *responding* to that verdict
the caller's job.

The three call sites (`Lecturer._ground`, `Tutor._prefix`, `Reviewer._prefix`)
take `.passages`. The Lecturer keeps the whole result, because it is the agent
that acts on the verdict.

`session.context.Passage` gained the §6 fields — `section_path`,
`source_authors`, `chunk_type`, `relevance_score`, `retrieval_reason` — rather
than a second `Passage` class being introduced. Two passage models would drift,
and the prompt builder renders one of them. The unused `score` field was
renamed to §6's `relevance_score`.

### S5 — stance ordering keys on `chunk_id`, not on relevance

Not a divergence from §6, which is explicit, but worth stating next to §11:
because `passages` is sorted by `chunk_id` and stance changes *which* passages
survive the `k` cut rather than what order they come back in, two stances
against the same concept produce different sets in the same sort order. That is
what agent runtime §17 needs for its `(concept_id, stance, grounding_version)`
cache key to be meaningful.

### S6 — atomic siblings are recovered from adjacency

§6 allows a result to exceed `k` by up to three so a code or math block split
across several `source_chunks` rows comes back whole. §7 defers the explicit
parent/child link (`chunk_relations`) to v1.1.

Without that table, `search.expand_atomic_siblings` recovers siblinghood from
adjacency: consecutive `chunk_index` values in the same source with the same
atomic `chunk_type`. This is exact for blocks the chunker split, because it
writes the pieces consecutively and nothing else produces a run of adjacent
same-type atomic chunks. It would produce false positives on a corpus where two
unrelated code blocks happened to be adjacent with no prose between them —
possible in a problem sheet, and the reason `chunk_relations` is still worth
building.

### S7 — the load-bearing fallback is reported as `expanded`

§10 item 5 puts every load-bearing concept in the subject into the
neighbourhood as a low-weight fallback, and §10's `retrieval_reason` field
exists so a reviewer can tell a curated passage from an inferred one.

Taken together these conflict: a concept with no curation of its own inherits
the subject's load-bearing pointers and — if those were labelled `curated` —
would look expertly grounded when nobody had curated anything for it.
`Neighborhood` splits `core` (reachable along real edges) from `fallback`
(load-bearing only), and chunks reaching the pool only through the fallback are
reported as `expanded`.

The consequence is that §16's "No chunks in the concept neighborhood" row
almost never fires inside a subject that has any load-bearing concept. The
signal a reviewer should watch is not an empty result but an all-`expanded`
one, which is §19's open question 2 arriving early.

### S8 — `chunk_type` and `tsvector_text` shipped ahead of the v1.2 batch

§5 asks that both additions be folded into data layer v1.2 "alongside the six
items already pending". They are in migration 0009 on their own instead:
retrieval cannot be built without either — §9's keyword half reads
`tsvector_text` on every query — and blocking subsystem 3 on six unrelated
items would have been a scheduling decision dressed as a schema one. Both are
additive and neither touches the pending six.

### S9 — one retry ladder per modality, not one for both

§8 gives embedding four attempts at 1s/4s/16s/64s. §16 gives reranking "one
retry with 2s backoff". The implementation honours both rather than unifying
them, because the two calls sit in different places: the embedding worker runs
in the background where a 64-second wait costs nobody anything, and the
reranker runs inside a turn a learner is watching, where it costs them a
visible stall.

### S10 — a degraded result is never cached

§14 caches retrieval results for five minutes and does not carve out
degradation. Caching a degraded result would hold a keyword-only or unreranked
answer for five minutes after the provider recovered, turning a thirty-second
Voyage blip into a five-minute quality drop for every learner on that concept.
`HybridRetriever` skips the cache write when `degraded` is set.

### S11 — a skipped reranker gets positional scores, not raw RRF

§16 says a rerank failure returns "the top-k of the fused hybrid-search
result". Fused scores are RRF values — around 0.03 — and §13's score threshold
reads `relevance_score` "on the normalized 0-1 scale from the reranker".
Passing the fusion score straight through would put every degraded retrieval
below 0.5 and flag all of them, flooding the review queue during precisely the
outage a reviewer can do nothing about. Degraded results get a positional proxy
(1.0 decaying by 0.05) and carry `degraded=True` instead.

### S12 — the shared fixture is extended by an opt-in function

§17 asks that the lambda-calculus fixture be extended to four sources with
twenty chunks each. `lambda_calculus.extend_corpus` does that, but it is called
explicitly by retrieval's Tier 2 tests rather than folded into `build`. Every
existing test asserts against the one-source, twenty-chunk corpus — passage
counts, prefix token budgets, `len(subject_concepts)` — and tripling it under
them would change what those tests measure without changing what they claim.

### S13 — the chunker folds typographic ligatures

§7 does not mention text normalisation, and §2 puts extraction in subsystem 5.
The chunker nevertheless folds the seven Latin ligatures (`ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ`) to
their ASCII equivalents before measuring or storing anything.

The reason is that a ligature is not a display concern here, it is a search
defect. Postgres tokenises `deﬁnition` to `'deﬁnit'` and `definition` to
`'definit'`, and the two do not match — so a chunk carrying the ligature is
invisible to the keyword half of hybrid search for a word it plainly contains.
The Michaelson book carries 688 instances of U+FB01 alone, which would have
removed most of its prose from keyword results for "definition", "first" and
"find". Verified directly against Postgres rather than assumed.

Only those seven are mapped. Full NFKC normalisation would also fold
superscripts, fractions and several mathematical symbols; in a lambda calculus
corpus the notation is the content, so the broad form is destructive. `λ`,
`β-reduction` and `M[x := N]` are pinned by test.

This sits at the chunking boundary rather than the extraction one because
chunking is the last owner of the text before it becomes a `source_chunks` row,
and because §7 gives retrieval ownership of chunking quality "which drives
retrieval quality". If subsystem 5's extractor normalises first, this becomes a
no-op rather than a conflict.

---

## Things the spec locks that were followed despite a reservation

### The 0.5 average-score threshold is uncalibrated

§13 sets the score condition at an average below 0.5 "on the normalized 0-1
scale from the reranker", and §17 pins tests at 0.49 and 0.51. Nothing in the
spec establishes what Voyage rerank-2 actually returns for a good match, so
whether 0.5 is a sensible floor or a number that flags everything is unknown
until it runs against the real provider.

Implemented exactly as specified, with a Tier 3 calibration check
(`test_relevant_chunks_score_above_the_thin_grounding_floor`) that fails loudly
with a message naming this as a §18 v1.1 candidate if hand-labelled relevant
passages score below the floor. That is the cheapest way to turn an unknown
into a signal rather than a silent flood of review-queue rows.

### Voyage-3 and rerank-2

§4 locks both. Both are current. `EmbeddingProvider` and `Reranker` are
protocols, so §19's open question 4 is a one-line change in
`service.default_retriever` — but the spec chose the provider and swapping it
silently would make the spec and the code disagree about the thing §4 is most
explicit about.

---

## Deferred to later subsystems, as the spec directs

- **PDF extraction and the ingestion pipeline (§2, subsystem 5).**
  `chunking.chunk_blocks` takes `Block` values — text plus structure — and
  never sees a binary. `blocks_from_markdown` covers the Markdown path for
  fixtures; the PDF outline extractor that produces the same shape is
  subsystem 5's.
- **`concept_sources` authoring (§2, subsystem 5).** Retrieval reads curated
  pointers and never writes them. The reviewer workflow §13 describes — working
  the queue by adding pointers — needs an authoring surface that does not exist
  yet.
- **Frontend rendering (§12, subsystem 4).** `GET
  /api/artifacts/{id}/citations` returns everything a hover card needs,
  including `source_deleted` for the retired-source case. How it renders,
  including whether thin-grounding warnings surface to the learner at all, is
  subsystem 4's.
- **Retrieval quality measurement (§19, subsystem 6).** Whether reranking beats
  raw fusion (open question 3), the curated-versus-expanded ratio (open
  question 2), and per-modality recall are all measurable from what this
  subsystem records — `retrieval_reason` is on every passage — but the golden
  datasets and the metrics live in subsystem 6.
- **Redis-backed result caching (§14, subsystem 7).** The in-memory cache is
  per-process and correct while the runtime is single-node. `ResultCache` is a
  narrow enough interface that the swap touches one file.
- **Voyage key management and provider failover (§19, subsystem 7).** The
  circuit breaker keeps a Voyage outage from cascading; choosing to fail over
  to Cohere is an infrastructure decision.

---

## Open questions this build can already answer partially

**§19 question 1 — real chunk-size distribution.** Measured against the
Michaelson book on 19 August 2026 via `scripts/chunk_diagnostics.py`: median
441 tokens against a 400 target, nothing over the 600 maximum.

That reads like an answer and is not one. Three successive extraction
heuristics over the *same* PDF, feeding the *same* chunker, produced medians of
440, 113 and 124 — the distribution is dominated by extraction quality, not by
the chunker's parameters. Re-tuning the targets now would fit them to artefacts
of `pypdf` plus a diagnostic script's paragraph heuristics. Held open as
[SPEC_DEBT.md](SPEC_DEBT.md) **SD3** until subsystem 5's extractor exists.

The run did settle two things independent of extraction quality, both now fixed
and pinned by regression test: the 15% overlap escaped the size check entirely
(7.6% of chunks exceeded the 600 maximum, since the overlap is prepended after
the body is sized), and typographic ligatures were silently removing text from
the keyword index (S13).

**§19 question 2 — curated-versus-expanded ratio.** Directly measurable now:
`retrieval_reason` is on every returned passage. S7 makes the measurement
sharper than the spec anticipated — the load-bearing fallback is counted as
expansion rather than curation, so a concept whose result set is all `expanded`
is exactly the "authoring is behind" signal §19 asks for.

**§19 question 3 — reranker improvement.** Not answerable here; it needs the
hand-labelled evaluation set that is subsystem 6's. The Tier 3 test asserts the
reranker surfaces six hand-labelled passages correctly, which is a smoke test,
not a measurement.

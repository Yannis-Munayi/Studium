# Divergences from Content Authoring and Ingestion Specification v1.0

The implementation targets
`spec/Sub System 5 - Content authoring and ingestion/05-ingestion-v1.0.md`.
This file records where it differs, and why. It is the fourth of its kind,
after `DIVERGENCES.md` (data layer), `DIVERGENCES-RUNTIME.md` (agent runtime)
and `DIVERGENCES-RETRIEVAL.md` (retrieval); the I-series numbering keeps the
four sets distinguishable in code comments.

Each entry says what the spec asks for, what the code does, and what would have
gone wrong the other way. Three of them (I1, I2, I3) are cases where following
the spec literally produces something that either cannot run or is quietly
wrong — those are marked **load-bearing**.

---

## Load-bearing divergences

### I1 — `ingestion_job_kind` needed a `normalize` value the spec never added

**Spec.** §6.4 triggers the normalisation stage from an `ingestion_jobs` row
with `kind = 'normalize'`. §6.1's stage diagram shows Normalize as one of the
five stages, each an async worker driven off that table. §5 lists the schema
additions this subsystem needs: four of them, none touching the enum.

**Reality.** `ingestion_job_kind` is `('extract_text', 'chunk', 'embed',
'suggest_concept_mapping')`. There is no `normalize`. The stage the spec
describes in four separate places cannot enqueue itself.

**Code.** Migration 0010 adds `normalize` after `extract_text`. `ADD VALUE` is
transactional from Postgres 12 on and nothing in the migration uses the new
value, so it is safe inside Alembic's transaction. The downgrade rebuilds the
type, which is the only way Postgres removes a value, and deletes any
`normalize` jobs first rather than relabelling them — a normalize job renamed
`chunk` would be picked up by the chunk worker and run against text that was
never normalised.

**If followed literally.** The pipeline would have to either fold normalisation
into the extract stage or into the chunk stage. Both lose something the spec
asks for elsewhere: §8.4 versions the normalizer independently
(`sources.normalizer_version`) so a reviewer can re-normalise one source without
re-extracting it, and §6.4 gives normalisation its own retry policy — one
attempt, against extraction's two. Neither is expressible if the stage has no
job row of its own. The cheaper-looking workaround, reusing `extract_text` for
both, would make the retry policy the *extractor's*, so a normalisation bug
would trigger a re-extraction of a 400-page book.

This is a fifth v1.2 addition from this subsystem, and the count in §17 and §18
is correspondingly wrong: §17 says four (naming five), §18 says five, and the
real number is six.

---

### I2 — header/footer stripping needs digit masking, scoped to short lines

**Spec.** §8.1 step 6: "if the first or last two lines of a page match the same
lines in ≥50% of the source's pages, they're headers/footers."

**Reality.** Taken as exact string matching, this strips almost nothing from a
real book. A running head virtually always carries the page number — "Chapter
3    47", or just "47" — so each page's edge lines are unique, every line
appears exactly once, and nothing ever clears 50%. The heuristic fires only on
documents whose page furniture is byte-identical across pages, which is a
property of synthetic fixtures rather than of typeset material.

**Code.** `_furniture_signature` masks digit runs before comparing — but only
for lines of at most 40 characters (`FURNITURE_MASK_CHARS`). Longer lines must
match exactly. Two further guards sit alongside it: a page must have more
non-blank lines than its own two edge windows cover
(`MIN_PAGE_LINES_FOR_FURNITURE`), and no page is ever stripped to nothing.

**Why the scoping.** Masking makes a signature *less* discriminating, which is
the point for "47"/"48" and the hazard for everything else. Without the length
bound, eight pages of "Body line 3 of page N with prose..." mask to one
signature, clear the threshold, and the body of every page is deleted as a
running head. That is not hypothetical — it is what the first implementation
did, caught by a fixture before it reached a corpus. Confining masking to short
lines keeps the page-number case and refuses the prose case, and §8.1's own
instruction settles the ambiguous middle: "errs on the side of keeping content
if the pattern is ambiguous."

**What it costs.** A long running head that carries a page number — "Michaelson
— An Introduction to Functional Programming    47", 58 characters — is *not*
stripped, because it exceeds the mask threshold and never matches exactly. That
is a deliberate miss in the safe direction: the cost is one repeated line
inside some chunks, against the cost of silently deleting the corpus. The
`normalizer_warning` queue is where real instances of this surface (§18 open
question 2), and `FURNITURE_MASK_CHARS` is the knob if they accumulate.

**If followed literally.** Every academic PDF in the corpus would carry its
running head and page number into every chunk. Concretely: 400 copies of the
book's title in the embedded text, each one pulling its chunk's vector slightly
toward a phrase that says nothing about the passage, and 400 page-number tokens
in the keyword index. Nothing fails; retrieval just gets quietly worse in a way
that looks like a ranking problem.

---

### I3 — the per-source extractor override rides on the job, not on `sources`

**Spec.** §7.3: "The reviewer can override per-source via
`sources.metadata.extractor` before triggering re-extraction." §7.2 says the
same for marker: "reviewer sets `sources.metadata.extractor = 'marker'`."

**Reality.** `sources` has no `metadata` column. The data layer gives one to
`concepts` (§6.2, added by this build as a divergence — see `DIVERGENCES.md`
B1) but not to `sources`, whose columns are fixed in §6.3.

**Code.** The override travels on the extract job's `payload`, which is JSONB
and already exists: `enqueue(session, source_id=..., kind='extract_text',
payload={'extractor': 'marker'})`. `get_extractor(name)` reads it, and
`run_extract` passes it through.

**Why this is better than adding the column**, rather than merely cheaper. Which
extractor to run is a property of a *particular extraction run*, not of the
source. A source re-extracted under marker has been extracted by pdfplumber
before, and both facts matter: `sources.extractor_version` records what
produced the text that is currently there, and the job payload records what was
requested for a given attempt. A single `metadata.extractor` column conflates
the request with the outcome, so a failed marker run would leave the source
claiming marker while holding pdfplumber's text.

**If followed literally.** A sixth schema addition, for a column that would then
disagree with `extractor_version` on every failed re-extraction.

---

## Ordinary divergences

### I4 — the review queue's targets are checked in Python before the CHECK

**Spec.** §5 addition 1 defines `ingestion_review_queue` with a CHECK requiring
at least one of `source_id`, `source_chunk_id`, `subject_id`, `concept_id`.

**Code.** The CHECK is there as specified, and `queue.flag` also raises
`MissingProvenance` when all four are absent. The database constraint reports
`ck_ingestion_review_queue_has_target` violated; the Python check names all four
columns and says a review item with no target cannot be actioned.

`flag` therefore has provenance parameters defaulting to `None`, which reads
like a §13 violation and is not: the invariant forbids a *silent fallback*, and
this raises. A queue row's target is genuinely one-of-four, so a signature
demanding all four would be unsatisfiable by every caller. The §13.4 test
encodes exactly this: a `None` default is permitted only for writers whose
source contains a `require(...)` call or a `MissingProvenance` raise.

---

### I5 — four indexes on the queue's targets, not one

**Spec.** §5 addition 1 gives `ingestion_review_queue` three indexes: pending,
assigned, and `source_id`.

**Code.** Four target indexes — one per FK column — plus the two others.

All four target columns are `ON DELETE CASCADE`, and Postgres does not index the
referencing side of a foreign key. With only `source_id` indexed, deleting one
subject or one chunk sequential-scans the whole queue. The data layer's §7 rule
("every FK in §6 has an accompanying index") already required this; the schema
convention test caught the omission.

Each is partial on `IS NOT NULL`, since at most one target is ever set: indexing
the nulls would index three-quarters of the table for nothing.

---

### I6 — retrieval learned to filter superseded chunks

**Spec.** §7.3 item 4: "Retrieval queries prefer non-superseded chunks;
superseded chunks remain for citation resolution." The retrieval spec, written
earlier, knows nothing about `superseded_at`.

**Code.** `studium/retrieval/search.py` gained `AND sc.superseded_at IS NULL` on
its curated, vector, keyword and sibling-expansion paths.
`studium/retrieval/citations.py` deliberately did **not**: that is the path
`content_citations` resolves through, and it is the reason superseded rows are
kept at all.

Sibling expansion matters more than it looks. It recovers atomic-block
adjacency from consecutive `chunk_index` values, and after a re-extraction the
index space holds two generations — so an unfiltered seed's "neighbour" could be
a chunk from the superseded pass with unrelated text.

This is a change to subsystem 3 made from subsystem 5, which §18 says it did not
expect ("no changes expected to retrieval based on this spec"). It is
unavoidable: a column retrieval does not read cannot affect retrieval.

---

### I7 — re-chunking reports curated pointers it orphaned

**Spec.** §7.3 describes re-extraction as a four-step workflow and does not say
what happens to `concept_sources` rows pointing at the superseded chunks.

**Code.** `_flag_orphaned_curation` writes a `concept_source_conflict` row for
every concept whose curated pointers are now entirely superseded.

`concept_sources.chunk_ids` names specific chunks a domain expert chose. The new
generation has new ids and new boundaries, and which of them corresponds to the
passage the author picked is a judgement the pipeline cannot repeat. Left alone
this fails silently and expensively: with I6's filter in place the row survives,
resolves to nothing, and the Lecturer falls back to subject-wide search — the
"curated" path degrading to the "expanded" one with no signal anywhere. §13's
invariant is the reason this is a queue row instead of a guess.

---

### I8 — `ExtractorUnavailable` is not a review queue item

**Spec.** §6.3 step 4 and §14 send extraction failures to the queue as
`extractor_failure` at severity 3.

**Code.** A document that will not extract does exactly that. A *missing
extractor* — pdfplumber not installed — propagates instead.

The distinction is who can act. An `extractor_failure` row asks a reviewer to
look at a source; a missing library is an operator problem, the source is fine,
and a retry will not help. Filing it as `extractor_failure` would put one
identical row in front of a reviewer per source in the batch — hundreds of them,
burying the real failures, all describing something only whoever deployed the
process can fix.

---

### I9 — chunk-type ambiguity is defined, because the chunker has no "unsure"

**Spec.** §6.5 step 4: "For any chunk the chunker flags as ambiguous type,
insert `chunk_ambiguous_type` in the ingestion review queue."

**Reality.** Retrieval's `classify_block` always returns a type. There is no
ambiguity flag to read.

**Code.** `_flag_ambiguous` defines the condition: a `body` chunk whose text is
at least 35% non-alphanumeric, non-whitespace characters. That is the signature
of mathematics pdfplumber could not lay out — which §7.1 documents as a known
weakness — arriving typed as prose, where retrieval will return it as evidence
for a claim it does not support.

Severity 1 per §12.1. This is a heuristic over a heuristic and will have false
positives; a severity-3 flag for something usually harmless would train
reviewers to dismiss the queue.

---

### I10 — the publish gate requires a license *determination*, not a value

**Spec.** §14: "License classification incomplete → publish blocked with message
naming unclassified sources." §11.1 makes `permission_granted` the default.

**Reality.** `permission_granted` is both the untouched default and a
legitimate outcome (§11.5: a source awaiting rights-holder confirmation stays
there). The license value alone cannot say whether a human was involved.

**Code.** `LicenseState.is_classified` treats `permission_granted` as classified
only when `license_notes` is non-empty. The note is the evidence a reviewer
looked, which is the entire content of §11.2 step 4.

**If followed literally.** Either every source is publishable the moment it
uploads (if the default counts), or a source legitimately resting at
`permission_granted` with a documented basis can never be published (if it does
not). Both are wrong; the note is what separates them.

---

### I11 — concept removal is reported, never applied

**Spec.** §9.2 step 6 asks the import to determine "additions, modifications,
removals", and step 8 to "apply changes in one transaction".

**Code.** Additions and modifications are applied. Removals are reported in the
diff and left in the database.

`concepts` cascades to `concept_mastery`, so deleting one erases every learner's
recorded progress on it. A slug typo'd out of a YAML file is not consent for
that, and the YAML file is the only evidence the import has. Retiring a concept
is a deliberate act that deserves its own workflow.

Edges are the exception and are replaced wholesale: nothing references
`concept_edges`, so they carry no learner state, and a delete-and-reinsert is
the only way a *removed* edge actually goes away.

---

### I12 — one ligature table, imported from retrieval

**Spec.** §8.1: "The mapping table lives in
`studium.ingestion.normalize.LIGATURES`."

**Code.** `studium.ingestion.normalize` imports `LIGATURES` from
`studium.retrieval.chunking`, where it already existed from the Michaelson fix.

Two tables would be free to drift, and the failure mode is specific: chunk text
and the `tsvector` generated from it would disagree about the same source, and
only for words containing a ligature — which is exactly the class of bug the
original fix addressed. The retrieval table already covers precisely
U+FB00–U+FB06, which is what §8.1 asks for.

The name in the spec still resolves: `from studium.ingestion.normalize import
LIGATURES` works, because the import binds it into that module.

---

### I13 — form feed and vertical tab are converted, not stripped

**Spec.** §8.1 step 4 normalises them to space; step 7 strips control
characters. Both describe the same two codepoints, and the spec does not say
which wins.

**Code.** Step 4 wins. `\x0b` and `\x0c` are exempted from the control-character
strip and converted to spaces by the whitespace pass.

They are separators — a form feed is where a column or a page ended — so
deleting one joins the last word before it to the first word after. "the
endBeginning" is a token that exists in no dictionary and matches no query,
which is a worse artefact than the control character it replaced.

---

### I14 — pdfplumber's word-split tolerance is tuned to 2, not left at 3

**Spec.** §7.1 selects pdfplumber and says nothing about its extraction
parameters.

**Reality.** The default `x_tolerance` of 3 points is wider than the inter-word
gaps in the Michaelson book's typesetting, so `extract_text()` returned
`Itispossible,however,forboundvariablesindifferentfunctions` — whole paragraphs
as single tokens.

**Code.** `WORD_SPLIT_TOLERANCE = 2`, applied to every extraction call
including the fallback paths. Measured across all three subjects: the two
lambda calculus sources roughly double their space ratio (0.087 → 0.144, 0.066
→ 0.132), the Turing and Clojure material is unchanged, and going below 2 gains
nothing further. A strict improvement rather than a trade.

**Why it is here rather than only in the code.** This is the most important
thing the build found and the least visible. Every §7.4 measurement is a *size*
measurement, and a run-together paragraph has the same token count as a spaced
one — so the book reported a 409-token median against a 400-token target, 0.99
coverage, and 100% section detection while being invisible to keyword search.
The synthetic fixtures could not have caught it either, because reportlab
spaces its output normally at any tolerance. See SPEC_DEBT SD7; a
`words_run_together` normalisation warning now exists so the next instance
reaches a reviewer rather than a metric.

---

## What the corpus measurement settled

§18 open question 1 asked whether pdfplumber holds up against the real corpus,
and SD3 has been waiting on the same answer since the retrieval build. Both are
now closed. `scripts/measure_extraction.py --corpus ../material` over all 23
sources, after the I14 fix:

| Source | Pages | Median | Q1 | Q3 | Coverage |
|---|---|---|---|---|---|
| Michaelson (`gjm_lambda_book`) | 241 | 427 | 344 | 501 | 1.00 |
| lambda calculus parts (10 files) | 9–55 | 347–511 | — | — | 0.99–1.00 |
| Clojure parts (10 files) | 15–33 | 335–420 | — | — | 0.96–0.98 |
| Turing machines (2 files) | 46–65 | 9–271 | — | — | 1.00 |

Retrieval's 400-token target and 600-token maximum survive: the Michaelson
median lands at 427, the quartiles sit inside the 350–450 target window's
neighbourhood, and nothing reaches the ceiling. Section detection is 1.00 on
every source.

The Turing machine files are the outlier and are not a defect: they are lecture
slides at roughly 100 tokens per page, so their chunks are small because their
pages are. It is worth knowing before anyone reads the pooled median as a
corpus-wide statement.

---

## Deferred, not diverged

Recorded so the gaps are visible rather than assumed closed.

- **§7.2's alternative extractors.** `marker-pdf`, `unstructured.io` and
  `pdfminer.six` are not registered. The Protocol and the registry are in place;
  adding one means implementing `Extractor` and nothing else. They are
  deliberately *not* stubbed — a registry entry that raises on use reads as
  supported at the call site and fails at ingestion time, after the upload, in
  a worker.
- **§12.2's frontend admin page.** CLI only. The spec marks the admin page a
  subsystem 4 v1.1 candidate.
- **§10.3's interactive authoring.** `studium browse chunks` is implemented and
  ranks by keyword against the concept's own description rather than by vector
  similarity — the vector path costs an embedding call per invocation, which is
  the wrong trade for a command run dozens of times in an afternoon. The web UI
  is v1.1.
- **§15's cost accounting.** Every stage in this build is zero-cost: pdfplumber
  is local, and embedding cost is booked by retrieval's worker, which already
  attributes to `sources.uploaded_by`. The `attributable_to` parameter appears
  in `CRITICAL_COLUMNS` against the hosted extractors that would need it.
  Budget pre-checks on upload are not implemented — nothing in this build can
  spend money.
- **OCR.** §7.1 and §18 both put it in v2. Scanned PDFs are rejected cleanly by
  §6.3's token floor, with `extractor_failure` naming OCR as the reason.

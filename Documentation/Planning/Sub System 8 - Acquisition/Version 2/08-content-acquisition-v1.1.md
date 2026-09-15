# Studium — Subsystem 8: Continuous Content Acquisition

**Version 1.1 — 9 September 2026**

**A specification for the subsystem that turns a week's course materials into pedagogical structure: concepts, prerequisite edges, source links, and rubric criteria. Proposed by agents running as ingestion jobs, reviewed in proportion to consequence, materialized into the tables the Curator and Evaluator already consume. Includes the calibration loop that compares Studium's mastery estimates against real grades, which is the only part of the system that receives a signal from outside itself.**

*Supersedes v1.0 of 9 September 2026 in §0, §4, §5, §6, §7, §8, §10, §11, §12, §13, and §14. v1.0 was written without access to the schema or the CLI and marked every reference to an existing object as unverified. The artifacts arrived; §0.2 records what they corrected. Appendix A records what they revealed about the hand-authored lambda calculus content, which has candidate defects against the same tables.*

---

## 0. Verification

### 0.1 What is now verified

This revision was written against four artifacts supplied by the developer on 9 September 2026: the three CLI parsers walked programmatically, the ingestion pipeline's job-kind enum and dispatch, `\d+` output for seven tables, and the `ingestion_review_queue` schema with its complete writer inventory.

Every reference to an existing object in this document is now either verified against those artifacts or explicitly marked **[STILL OPEN]** with what would resolve it. There are five of the latter, listed in §14. They are narrower than v1.0's fourteen.

### 0.2 What v1.0 got wrong

Eleven corrections. Each is a case where v1.0 described the system as I modeled it rather than as it is.

| v1.0 claimed | Actually | Corrected in |
|---|---|---|
| A `studium` binary with one subcommand tree | No binary. Three argparse CLIs invoked as `python -m studium.{ops,ingestion,eval}.cli`, each setting `prog="studium"` so help prints as one tool. Each rejects the others' subcommands. | §11.3 |
| A `rubrics` table with `rubric_criteria` beneath it | No `rubrics` table anywhere: not in schema, migrations, or models. `rubric_criteria` hangs directly off `concepts`, unique on `(concept_id, slug)`. A rubric is the set of criteria for a concept. | §6.4, §10, §11.2 |
| Edge strength named `strength` | `concept_edges.weight`, `real DEFAULT 1.0`, `CHECK (weight > 0 AND weight <= 1)` | §6.2, §10 |
| Edge rationale as a new field | `concept_edges.note` already exists for this | §6.2 |
| Proposal staging needed for all object kinds | `rubric_criteria` already has `status artifact_status DEFAULT 'draft'`, `reviewed_by`, `reviewed_at`, `retired_at`. A native review lifecycle exists for criteria and should be used rather than duplicated. `concepts` and `concept_edges` have no status column, so those do need staging. | §7.2, §10 |
| Concept-source links carry a weight | `concept_sources` has no weight column. It has `chunk_ids uuid[]`, `role`, `note`, and `UNIQUE (concept_id, source_id, role)` | §6.3, §10, Appendix A |
| Pipeline stage named `extract` | The job kind is `extract_text`. And `upload` is not a stage at all: `upload_pdf` is a synchronous entry point that inserts the sources row, stores the file, calls `flag_pending`, and enqueues the first job. | §5 |
| Agent hosting an open design question | `ingestion_job_kind` already declares `suggest_concept_mapping`, with no enqueuer and no dispatch branch. The pipeline is the intended host and someone anticipated this. | §6.0, §14 |
| A new review surface would be needed for validation failures | `ingestion_review_queue` exists with nine `flag_source` values, all with live writers, including `graph_validation_error` (sev 3) and `rubric_validation_error` (sev 3). Validation failures route there through the existing `flag()`. | §8.2 |
| One review queue | Two. `studium queue` is ingestion's (`ingestion_review_queue`); `studium review` is evaluation's (`content_review_queue`). `studium review upstream` escalates from the latter to the former, which is precedent for cross-queue flow. | §7.6 |
| Concepts addressed by `module` | `concepts.module_slug`, with a regex CHECK | Appendix A |

### 0.3 One anti-pattern instance found in the artifacts

`suggest_concept_mapping` is declared in migration 0001, in `models/base.py:269`, and asserted in `tests/schema/test_enums.py:139`. Nothing enqueues it. `run_worker_once` has no branch for it; a row carrying that kind falls through to the else and is cancelled as an unhandled kind. The schema test asserts the value is present in the enum, which is true, and passes whether or not anything can execute it.

Declaration sites: three. Execution sites: zero. That is catalog entry 3, "symbol at every declaration site, no execution site," which the project has now hit at least four times. It is recorded here because §6.0 proposes reviving the value rather than deleting it, and reviving it should close the pattern rather than extend it: the test that proves the revival must exercise the dispatch branch, not the enum membership.

---

## 1. Purpose

Subsystem 5 converts documents into chunks and embeddings. That produces retrieval substrate: material a Lecturer can cite. It does not produce the structure the rest of the system runs on. The Curator sequences by prerequisite edges. The Evaluator grades against rubric criteria. Mastery accumulates per concept. None of those objects come out of the ingestion pipeline, and for the lambda calculus subject all of them were authored by hand.

Hand-authoring worked for one subject drawn from one book. It does not survive five concurrent courses delivering material weekly. It also makes the system's central claim false: a learning system that can only teach what a human wrote for it is a content management system with a good frontend.

Subsystem 8 closes that gap. Each week the learner uploads what the course produced. The subsystem proposes the concepts that material introduces, the edges connecting them to what came before, the links from concepts to specific chunks, and draft rubric criteria for the concepts that carry weight. The learner reviews a diff rather than a blank page. Accepted proposals materialize into the same tables hand-authoring wrote to, and everything downstream is unchanged.

The subsystem also does something hand-authoring never could. When the learner uploads graded work, the grade is a measurement of what the learner actually knows, taken by someone outside the system. Comparing it against Studium's own estimate is the only true evaluation the system can perform on itself. §9 specifies that loop; §3 explains why the design would be unsound without it.

## 2. Scope

**In scope.** Artifact intake and role assignment. Proposal agents for concepts, edges, source links, and rubric criteria. The review surface and its accept/reject/edit semantics. Materialization into existing tables. Extraction of pedagogical signal from graded work. The calibration loop. Rejection memory. Health metrics on the review process itself.

**Out of scope.** Chunking, embedding, and retrieval, which belong to subsystems 5 and 3 and are consumed unchanged. Session delivery (subsystem 2). Assessment grading at attempt time (subsystem 6). Audio transcription, treated as preprocessing with a named interface.

**Assumed deployment context.** Single-learner private use for one semester. Multi-learner concerns are deliberately unaddressed; §14 records this as the largest open question for any future version.

## 3. The closed-loop hazard

Unchanged from v1.0 and reproduced because later sections depend on it.

### 3.1 The failure

Lecture notes are ingested. An agent reads them and proposes concepts. Another agent reads the same notes and drafts criteria for those concepts. The learner studies from those notes, answers a criterion's prompt, and the Evaluator grades against key points derived from the notes.

Every step draws on one source. If the proposer misreads the material, the misreading becomes a concept. The criterion tests the misreading. The learner, having studied the same source, answers in the misreading's terms. The Evaluator awards full marks. The mastery estimate comes out high and confident.

Nothing in that sequence can detect the error, because no step consults anything the error did not already contaminate.

### 3.2 Why this is familiar

Sixth instance of a shape the project has now recorded five times, where checker and checked share an origin so the check cannot fail:

1. Mock agrees with mock while both drift from the server.
2. Fake whose correct behaviour equals the buggy behaviour.
3. Symbol present at every declaration site, absent from every execution site.
4. Test passes when a fake returns a default that matches the bug.
5. Test whose subject shares vocabulary with the code that fixes it.
6. **Content authored from source S, graded by criteria derived from S, answered by a learner who studied S.**

Entry 6 should be added to the catalog on adoption, before it is observed rather than after. It is the most consequential of the six: the others produce a green test, this one produces a confident learner who is wrong.

### 3.3 Five defences, in order of strength

**Exogenous signal from graded work.** The prompt was written by an instructor; the grade was assigned by an instructor. Neither passed through this system. §9 builds on it. Only this defence can *detect* an error that already occurred; the rest lower its probability.

**Instructor feedback as criterion key points.** Feedback saying the learner conflated third normal form with Boyce-Codd is a key point with external provenance, naming a failure mode observed in the actual course. §6.5 extracts these; §6.4 requires their use.

**Multi-source attestation.** A concept appearing in both the notes and the textbook has survived two independent expositions. §6.1 records attesting artifacts; §7.1 routes single-source concepts to mandatory review.

**Human review of structure.** The learner is attending the course and knows whether the professor treats a topic as foundational. §7 spends the budget where this knowledge is decisive and the agents are weakest.

**Learner submissions never ground retrieval.** §4.3, enforced by predicate and mutation-tested per §13.1.

## 4. Artifact taxonomy

Seven roles in two families. The distinction is load-bearing and enforced at the retrieval layer.

### 4.1 Teaching sources

May ground retrieval. Content treated as a claim about the subject.

| Role | Description | Retrieval weight |
|---|---|---|
| `lecture_notes` | Instructor-produced slides, handouts, posted notes. Reflects this course's emphasis. | Highest |
| `textbook_chapter` | Published material. Authoritative on the subject, not necessarily aligned to this course. | High |
| `lecture_recording` | Transcript of a recorded session. See §4.4. | Low |

### 4.2 Graded work components

One assignment produces up to four artifacts. They remain distinct records because they play four incompatible roles.

| Role | Description | May ground retrieval? |
|---|---|---|
| `assessment_prompt` | What was asked. An authentic item written by someone who teaches the course. | No — but see §6.5 |
| `learner_submission` | What the learner produced. Evidence about the learner, not about the subject. | **Never** |
| `instructor_feedback` | The instructor's assessment of the submission. Names real failure modes. | No — but see §6.5 |
| `grade` | Scalar or structured score with its scale. | No |

### 4.3 The submission invariant

**A `learner_submission` artifact's chunks must never be returned by retrieval as grounding for instruction.**

If the learner's answer was wrong and is later retrieved as source material for a lecture on the same topic, the system teaches the learner their own error, cited as authority. The learner cannot detect this, because the error is already in the shape they think in.

Enforced at the retrieval query by predicate on artifact role, not by omitting the chunks. The submission is chunked and embedded like anything else, because §6.5 reasons over it and §9 needs it for attribution.

Note that `source_chunks` has no artifact-role column. The predicate resolves through `source_chunks.source_id → sources.id → acquisition_artifacts.source_id → role`. This is a join, and the retrieval path must carry it. An implementation that filters in application code after retrieval returns rows is acceptable only if the guard in §13.1 exercises the actual path.

### 4.4 Recordings

Transcripts are low-density: fifty minutes yields perhaps six thousand words, much of it administrative or digressive. Weighting them with the notes dilutes retrieval.

Two consequences. Lowest retrieval weight among teaching sources. And one class of moment extracted separately because it exists nowhere else: the instructor signalling assessment relevance. "This will be on the exam," "you should be comfortable with this," "I always ask something like this" is the highest-value few seconds in a recording, appears in no slide deck, and is direct evidence about which concepts are load-bearing. §6.6 specifies the extractor.

Audio-to-text is preprocessing outside this subsystem. The interface: a recording artifact arrives with its transcript attached, timestamped at least at paragraph granularity.

### 4.5 Rights and classification

Every artifact is classified on intake using the existing workflow: `studium sources classify <source_id> --license X --note X`, writing `sources.license` (type `license_kind`) and `sources.license_notes`.

**[STILL OPEN]** The `license_kind` enum values are unknown. `public_domain` is confirmed in use. Whether a value suitable for private-use-only course material exists, or must be added by migration, needs the enum definition.

The learner's instruction that appropriate consent exists is taken at face value. The classification requirement is not a consent check. It records each source's posture at intake so a later decision about sharing has a record to consult rather than a reconstruction to perform.

Note that `licensing.py:213` already writes a `license_pending` flag (severity 2) to `ingestion_review_queue` on every upload. Subsystem 8 uploads will generate one queue row per artifact. At five subjects and perhaps six artifacts weekly, that is thirty pending rows a week against an alert threshold of twenty. §12.7 addresses this.

## 5. The weekly cycle

Six stages. Stage 3 runs on the existing ingestion job queue.

**Stage 1 — Batch open.** The learner opens a batch against a subject with a week or unit label. A batch is the unit of review and of provenance.

**Stage 2 — Artifact intake.** Files added with an explicit role from §4. Role assignment is a declaration, not an inference: misfiling a submission as lecture notes defeats §4.3. Intake warns on unusual role combinations, such as `instructor_feedback` with no `learner_submission` in the same batch.

**Stage 3 — Ingest and propose.** Artifacts pass through the existing pipeline. Verified stage sequence:

| Stage | Job kind | Entry point | Retries | Enqueues |
|---|---|---|---|---|
| upload | *(not a job)* | `upload_pdf` | — | `extract_text` |
| extract | `extract_text` | `run_extract` | 3 | `normalize` |
| normalize | `normalize` | `run_normalize` | 2 | `chunk` |
| chunk | `chunk` | `run_chunk` | 2 | `embed` |
| embed | `embed` | `run_embed` | 1 | — |

Proposal jobs are enqueued after `embed` completes for every artifact in the batch. They are not per-artifact: the Concept Proposer needs the batch's chunks together, and the Edge Proposer needs the batch's accepted concepts. §6.0 specifies the job kinds.

**Stage 4 — Review.** §7. Target budget fifteen minutes per subject per week.

**Stage 5 — Materialize.** Accepted proposals written to `concepts`, `concept_edges`, `concept_sources`, `rubric_criteria`. Rejections recorded in rejection memory (§7.5). Validation (§8.2) runs before any write.

**Stage 6 — Calibrate.** If the batch contained graded work, §9 runs.

Stage 5 is atomic. A prerequisite edge referencing a concept that failed to insert leaves the graph in a state the Curator will traverse into and fail on.

## 6. Proposal agents

### 6.0 Hosting: ingestion jobs, not session agents

v1.0 left agent hosting open. The artifacts answer it. `ingestion_job_kind` already declares `suggest_concept_mapping` with no enqueuer and no dispatch branch (§0.3). The pipeline is the intended host: it has a job queue, per-kind retry counts, stage dispatch through `run_worker_once`, and `StageResult` carrying the kind. Proposal agents are batch jobs with retry semantics, not session agents emitting effects into a turn.

Proposed job kinds, following the existing naming:

| Agent | Job kind | Retries | Rationale for retry count |
|---|---|---|---|
| Concept Proposer | `suggest_concept_mapping` *(revive)* | 2 | Model call, transient failure plausible |
| Edge Proposer | `suggest_concept_edges` | 2 | Same |
| Source Link Proposer | `suggest_source_links` | 2 | Same |
| Rubric Drafter | `draft_rubric_criteria` | 2 | Same |
| Graded Work Extractor | `extract_graded_work` | 2 | Same |
| Exam Signal Extractor | `extract_exam_signal` | 1 | Lowest value; failure is tolerable |

Reviving `suggest_concept_mapping` must close the anti-pattern rather than extend it. The test proving the revival exercises the dispatch branch and observes a proposal row appear. A test asserting the enum contains the value passes today, with nothing implemented.

Model assignment per agent is **[STILL OPEN]** pending cost measurement (§14).

### 6.1 Concept Proposer

**Input.** The batch's teaching-source chunks. The existing concept list for the subject: `slug`, `title`, `module_slug`, `depth`, bounded to keep context growth linear rather than quadratic as a subject accumulates.

**Output.** Proposals whose payload matches the `concepts` insert shape: `slug` (must satisfy the schema's regex CHECK), `title`, `short_description`, `long_description`, `depth` (smallint 1–5, CHECK-enforced), `is_load_bearing`, `estimated_minutes`, `module_slug` (regex CHECK), `position`, `metadata`. Plus proposal-level fields: confidence, supporting chunk references, attesting artifacts.

**Requirements.**

Deduplication against the existing graph by slug and by semantic similarity of title and description. `UNIQUE (subject_id, slug)` will reject an exact collision at insert, which is a materialization failure rather than a review outcome. A subject accumulating `normalization`, `database-normalization`, and `normal-forms` across three weeks passes the constraint and produces a graph the Curator cannot sequence.

Granularity consistency, calibrated against the existing concept set. Without it one week yields three broad concepts and the next fifteen narrow ones, and mastery becomes incomparable across the subject. §12.2 gives the detection metric.

Attestation recording, so §7.1 can route single-source concepts.

### 6.2 Edge Proposer

The most important agent and the one with the hardest job.

**Input.** Accepted concepts from this batch. The **full** existing graph for the subject. Retrieval over prior weeks' chunks.

**Output.** Proposals matching the `concept_edges` insert shape: `subject_id`, `from_concept_id`, `to_concept_id`, `kind` (`concept_edge_kind`), `weight` (`real`, `CHECK (weight > 0 AND weight <= 1)`), `note`.

**Weight is a real in (0,1], not an ordinal.** The hand-authored graph used integers 3–5 on a `strength` key that does not exist; Appendix A records the correction. The proposer emits normalized confidences directly.

**`note` carries the rationale.** It is not decoration: it is what the learner reads during review, and reviewing an edge without knowing why it was proposed is guessing rather than judging. An edge whose note restates the edge should be treated as low-confidence.

**Requirements.**

Actively seek edges into the existing graph. This is why the agent gets the full graph. Week nine's B-trees depend on week three's disk access cost model and nothing in week nine's slides mentions it. Only something holding both weeks at once can propose it.

Respect `UNIQUE (from_concept_id, to_concept_id, kind)` and the no-self-edge CHECK. Two proposals for the same triple within a batch is an internal collision that materialization will reject; the proposer should merge rather than emit both.

Cycle safety is guaranteed by §8.2's detection, not by the agent's care.

### 6.3 Source Link Proposer

**Input.** Accepted concepts and the batch's teaching-source chunks.

**Output.** Proposals matching `concept_sources`: `concept_id`, `source_id`, `chunk_ids` (`uuid[]`), `role` (`concept_source_role`), `note`.

**One proposal per `(concept, source, role)` triple, carrying an array of chunk ids.** The table is `UNIQUE (concept_id, source_id, role)`, so a proposer emitting one row per chunk produces collisions. This is the shape the hand-authored content got wrong in four places (Appendix A.2).

There is no weight column. Ordering among links of the same role is expressed by chunk order within the array, or not at all.

`chunk_ids` is an array with no foreign key. Postgres cannot constrain array elements, which is the dangling-reference hole `studium ops nightly` scans for. A proposal referencing a chunk that is later superseded produces exactly the row that scan is designed to find. Materialization must therefore validate chunk existence at write time rather than relying on the database.

Auto-accepted per §7.3.

### 6.4 Rubric Drafter

**A rubric is not an entity.** There is no `rubrics` table. `rubric_criteria` hangs off `concepts`, `UNIQUE (concept_id, slug)`. The drafter produces criteria, and the set of criteria for a concept is what the rest of the system means by that concept's rubric.

**Input.** An accepted load-bearing concept, its source chunks, and any instructor feedback in the batch touching it.

**Output.** Proposals matching `rubric_criteria`: `concept_id`, `slug`, `prompt`, `key_points` (`jsonb`, CHECK enforces a JSON array), `weight` (`smallint`, **CHECK `weight IN (1,2,3)`**), `min_words` (default 20), `status` (`artifact_status`, default `draft`).

**The weight constraint is narrow and absolute.** Three permitted values. The hand-authored rubrics used 1, 1.5, 2, and 3; the four criteria at 1.5 would be rejected at insert (Appendix A.3).

**Structure.** One criterion is one prompt plus the key points its answer should contain. The natural mapping from the hand-authored two-level form is: each authored prompt becomes one `rubric_criteria` row, and that prompt's authored criteria become entries in `key_points`. Under that mapping the twenty authored files yield 77 criteria rows rather than 233.

**Requirements.**

Where instructor feedback touching this concept exists, the drafter must incorporate the failure modes it names as key points. This is §3.3's injection point for external provenance and the single most valuable thing this agent does. A key point reading "does not conflate 3NF with BCNF, which the instructor's feedback on lab 4 identified as commonly missed" is grounded in something no part of this system produced.

Prompt kind variation across a concept's criteria — procedural, conceptual, application, analytical. There is no column for kind, so this is a property of the drafted set rather than a stored attribute. **[STILL OPEN]** whether `rubric_criteria` should gain a kind column, or whether kind belongs in the criterion's slug convention, or nowhere.

**Materialization uses the native lifecycle.** Criteria accepted at review materialize with `status` active and `reviewed_by`/`reviewed_at` set. Criteria that were spot-check-eligible but not sampled materialize as `status = 'draft'` with `reviewed_by` null, and the spot-check backlog is exactly `WHERE status = 'draft' AND reviewed_by IS NULL`. No new state is needed. **[STILL OPEN]** whether the Evaluator serves draft criteria; if it does not, unsampled criteria are inert until reviewed and §7.2's proportional review does not achieve what it intends.

Note that `assessment_responses` references `rubric_criteria` `ON DELETE RESTRICT`. A criterion that has been answered cannot be deleted. Editing an accepted criterion that carries responses changes what past responses were graded against, so edits after first use should retire (`retired_at`) and supersede rather than mutate.

### 6.5 Graded Work Extractor

Operates on §4.2's four components. Privileged status: its outputs derive from exogenous signal, and §7.4 treats them accordingly.

**Outputs.**

*Concept attribution.* Which concepts the work exercises and in what proportion, each with a confidence. §9 consumes this and §12.3 is honest about its difficulty.

*Failure modes.* Extracted from `instructor_feedback`, becoming candidate key points per §6.4. Highest-value output of the subsystem.

*Assessment items.* The `assessment_prompt` is an authentic item. **[STILL OPEN]** where it lands. `studium eval` is the system-evaluation CLI — datasets, gates, A/B tests — not learner assessment, and no learner-assessment table appeared in the artifacts. `assessment_responses` referencing `rubric_criteria` suggests a learner assessment may be a set of criteria rather than a separate entity with a problem pool. This needs the assessment-side schema before it can be specified.

*Calibration input.* Grade, scale, and submission date, passed to §9.

**The submission's role.** The extractor reads `learner_submission` to understand what the feedback responds to. It informs reasoning; it does not become teaching material. §4.3 holds.

### 6.6 Exam Signal Extractor

**Input.** Recording transcripts with timestamps.

**Output.** Timestamped moments where the instructor signals assessment relevance, the concept each refers to, and a strength estimate. Feeds §6.1's load-bearing proposal.

Low volume, high value, lowest retry count. An instructor saying "you will see something like this on the midterm" is better evidence of what matters than any structural inference from the material.

## 7. Proportional review

Review everything and the subsystem has relocated the authoring burden. Review nothing and §3's loop is fully closed.

**Budget: fifteen minutes per subject per week.** Five subjects is seventy-five minutes weekly. If actual review time substantially exceeds it the design has failed and should be revised rather than endured; §12.5 explains what happens otherwise.

### 7.1 Always reviewed

**Every proposed edge touching an existing concept.** Cross-week structural claims compound: an edge accepted in week four shapes the Curator through week thirteen. The learner has decisive knowledge the agent lacks.

**Every load-bearing flag.** Drives rubric authoring priority and mastery gating. False negative means no criteria for something the exam tests; false positive means effort on the peripheral.

**Every single-source concept.** Per §3.3.

**Anything the proposer marked low-confidence.**

### 7.2 Spot-checked

**Rubric criteria.** A sample per batch: lowest-confidence drafts plus a random selection. Unsampled criteria materialize as `status = 'draft'` per §6.4 and form a standing backlog. Full review of every criterion is the largest single cost and the least defensible, since criterion errors surface naturally when the learner answers a prompt that does not make sense.

**Multi-source concepts with high confidence.** Collapsed list; the learner scans titles and expands anything surprising.

### 7.3 Auto-accepted

**Concept-source links.** Low stakes, natural correction path through visibly bad citations.

**Edges among only-new concepts.** These compound less than cross-week edges, since a future week attaching to them surfaces them again in a reviewed context. Shown in a collapsed "also proposed" list.

### 7.4 Never auto-accepted

**Anything contradicting a prior rejection** (§7.5).

**Anything derived from graded work that would change an existing accepted object.** Graded-work-derived proposals carry external provenance and should generally be trusted, but a proposal rewriting an accepted criterion claims a prior decision was wrong, and that is the learner's call. Compounded by §6.4's `ON DELETE RESTRICT`: if the criterion has responses, the change is a supersession with history, not an edit.

### 7.5 Rejection memory

Every rejection recorded with enough shape to recognise a re-proposal: proposal kind, content signature, optional reason.

Before surfacing a proposal the review builder checks rejection memory. A match is suppressed, or if materially different, surfaced with the prior rejection shown alongside.

Without this the same wrong edge is proposed every week the topic recurs, rejected every week, and the review budget is consumed by a conversation the system cannot remember.

### 7.6 Relationship to the two existing queues

`ingestion_review_queue` (`studium queue`) holds ingestion failures. `content_review_queue` (`studium review`) holds evaluation-side content problems, with a CHECK requiring `artifact_id` or `session_turn_id`. `studium review upstream` escalates from the latter to the former.

Proposals belong in neither. A queue row means "something is wrong here"; a proposal means "here is what I think, yes or no." Proposals carry a payload to materialize and accept/reject/edit semantics that queue rows do not have.

Subsystem 8 *writes to* `ingestion_review_queue` when validation fails (§8.2), through the existing `flag()` in `queue.py:86`, which is wrapped in the `@ingestion_writer` provenance guard. That guard raises `MissingProvenance` if all four target columns are null, so every subsystem 8 flag must carry at least one of `source_id`, `source_chunk_id`, `subject_id`, `concept_id`. A batch-level failure carries `subject_id`, which satisfies it.

## 8. Incremental graph growth

### 8.1 The cross-week problem

Course material arrives in temporal order; conceptual dependencies do not respect it. A week-nine topic may depend on a week-three cost model that week nine treats as assumed background and never names.

Three mechanisms, none sufficient alone: the Edge Proposer holds the full graph (§6.2), has retrieval over prior chunks, and the learner reviews every cross-week edge (§7.1).

### 8.2 Validation before materialization

**Cycle detection.** Prerequisite edges must form a DAG. A cycle makes the Curator's sequencing non-terminating or arbitrary. Detection runs over the union of the existing graph and the batch's accepted edges. Failure blocks the whole batch per §5.

On failure, write `graph_validation_error` (severity 3) to `ingestion_review_queue` with `subject_id` set. This flag source already exists with writers at `authoring.py:542` and `publish.py:114`; subsystem 8 joins them rather than adding a value.

**Constraint pre-checks.** `UNIQUE (subject_id, slug)` on concepts, `UNIQUE (from, to, kind)` on edges, `UNIQUE (concept_id, source_id, role)` on concept sources, `UNIQUE (concept_id, slug)` on criteria, plus the CHECKs on `depth`, `weight`, criterion `weight`, and the slug regexes. Checking in application code before the transaction gives a reviewable error; letting the database reject gives an integrity error mid-batch.

**Chunk existence.** `concept_sources.chunk_ids` has no FK (§6.3). Validate at write.

**Orphan detection.** A concept with no edges either direction is suspicious: occasionally genuine, usually the Edge Proposer failed to connect it. Surfaced in the batch report; does not block.

**Depth consistency.** A concept whose depth is far from its prerequisites' suggests a granularity error. Warning.

### 8.3 Graph growth metrics

Recorded per batch in `acquisition_batches.metrics`:

- Concepts added, edges added, cross-week edges as a fraction of total.
- Mean concepts per artifact, tracked across batches within a subject.
- Orphan count.
- Maximum prerequisite chain depth.

A subject whose cross-week edge fraction trends toward zero is accumulating disconnected weekly islands rather than a graph.

## 9. The calibration loop

### 9.1 Mechanism

1. Graded work enters with grade, scale, and submission date.
2. §6.5 attributes concepts with weights and confidences.
3. For each concept above the attribution confidence threshold, the mastery estimate **as of the submission date** is retrieved. As-of matters: today's estimate measures nothing, since the learner has studied since.
4. Grade normalized to [0,1].
5. Disagreement computed per concept.
6. Where disagreement exceeds threshold and attribution confidence suffices, an observation is recorded.

**[STILL OPEN]** Step 3 requires mastery history at sufficient granularity to reconstruct a past estimate. The supplied artifacts did not cover the mastery tables. If history is not retained, this requires either a snapshot written at submission time or a change to mastery retention. §14 names the artifact that would resolve it.

### 9.2 Direction matters

**Overconfident** (estimate above grade) is the dangerous direction: Studium said the learner knew it, the instructor's measurement says otherwise. This is the failure §3.1's loop produces and the one that costs exam marks.

**Underconfident** is less costly but informative: an over-strict criterion, or a mastery model failing to credit evidence.

### 9.3 Response

- Single observation: recorded, no action.
- Repeated overconfidence on one concept: that concept's criteria flagged for review. Criteria awarding marks the instructor does not are measuring something else.
- Systematic overconfidence across a subject: the subject's mastery parameters flagged. **[STILL OPEN]** whether BKT parameters are per-subject configurable.
- Consistently low attribution confidence: §6.5's extractor is failing on this material, itself a finding.

### 9.4 The semester as an evaluation

At semester end the accumulated observations are the evaluation dataset: not an impression of whether Studium helped, but a per-concept record of where its confidence matched an external measurement.

This is the strongest evidence the project can produce about whether the thesis holds. It should be treated as the semester's primary output, and the record designed to survive and export rather than existing as an operational side effect.

## 10. Data model

New tables. Conventions verified against the supplied `\d+` output: `uuid_generate_v7()` primary keys, `TIMESTAMPTZ NOT NULL DEFAULT NOW()` timestamps, `set_updated_at` trigger where `updated_at` exists.

```sql
CREATE TYPE acquisition_artifact_role AS ENUM (
  'lecture_notes', 'textbook_chapter', 'lecture_recording',
  'assessment_prompt', 'learner_submission',
  'instructor_feedback', 'grade'
);

CREATE TYPE proposal_kind AS ENUM (
  'concept', 'edge', 'source_link', 'rubric_criterion', 'assessment_item'
);
-- v1.0 also declared 'rubric'. There is no rubric entity; removed.

CREATE TYPE proposal_status AS ENUM (
  'pending', 'accepted', 'rejected', 'edited', 'superseded', 'suppressed'
);

CREATE TABLE acquisition_batches (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  subject_id      UUID NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
  label           TEXT NOT NULL,
  opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  proposed_at     TIMESTAMPTZ,
  reviewed_at     TIMESTAMPTZ,
  materialized_at TIMESTAMPTZ,
  metrics         JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE acquisition_artifacts (
  id             UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  batch_id       UUID NOT NULL REFERENCES acquisition_batches(id) ON DELETE CASCADE,
  role           acquisition_artifact_role NOT NULL,
  source_id      UUID REFERENCES sources(id) ON DELETE SET NULL,
  filename       TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
  transcript_of  UUID REFERENCES acquisition_artifacts(id) ON DELETE SET NULL,
  metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- CHECK mirrors sources.content_sha256's length constraint.

CREATE INDEX idx_acquisition_artifacts_batch ON acquisition_artifacts(batch_id);
CREATE INDEX idx_acquisition_artifacts_role ON acquisition_artifacts(role);
CREATE INDEX idx_acquisition_artifacts_source ON acquisition_artifacts(source_id);
-- The source index is not optional: §4.3's retrieval predicate joins on it.

CREATE TABLE content_proposals (
  id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  batch_id            UUID NOT NULL REFERENCES acquisition_batches(id) ON DELETE CASCADE,
  kind                proposal_kind NOT NULL,
  status              proposal_status NOT NULL DEFAULT 'pending',
  payload             JSONB NOT NULL,
  confidence          NUMERIC(3,2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  agent_name          TEXT NOT NULL,
  job_kind            TEXT NOT NULL,
  attesting_artifacts UUID[] NOT NULL DEFAULT '{}',
  content_signature   TEXT NOT NULL,
  review_required     BOOLEAN NOT NULL,
  materialized_id     UUID,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_content_proposals_batch_status ON content_proposals(batch_id, status);
CREATE INDEX idx_content_proposals_signature ON content_proposals(content_signature);

CREATE TABLE proposal_decisions (
  id             UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  proposal_id    UUID NOT NULL REFERENCES content_proposals(id) ON DELETE CASCADE,
  decision       proposal_status NOT NULL,
  edited_payload JSONB,
  reason         TEXT,
  decided_by     UUID NOT NULL REFERENCES users(id),
  decided_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  seconds_spent  INTEGER
);
-- seconds_spent feeds §12.5's review-fatigue metric.

CREATE TABLE graded_work (
  id                     UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  batch_id               UUID NOT NULL REFERENCES acquisition_batches(id) ON DELETE CASCADE,
  prompt_artifact_id     UUID REFERENCES acquisition_artifacts(id) ON DELETE SET NULL,
  submission_artifact_id UUID REFERENCES acquisition_artifacts(id) ON DELETE SET NULL,
  feedback_artifact_id   UUID REFERENCES acquisition_artifacts(id) ON DELETE SET NULL,
  grade_value            NUMERIC,
  grade_scale_max        NUMERIC,
  grade_raw              TEXT,
  submitted_at           DATE NOT NULL,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE graded_work_concepts (
  id                     UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  graded_work_id         UUID NOT NULL REFERENCES graded_work(id) ON DELETE CASCADE,
  concept_id             UUID NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  attribution_weight     NUMERIC(3,2) NOT NULL CHECK (attribution_weight BETWEEN 0 AND 1),
  attribution_confidence NUMERIC(3,2) NOT NULL CHECK (attribution_confidence BETWEEN 0 AND 1),
  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (graded_work_id, concept_id)
);

CREATE TABLE calibration_observations (
  id                     UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  graded_work_id         UUID NOT NULL REFERENCES graded_work(id) ON DELETE CASCADE,
  concept_id             UUID NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  estimate_at_submission NUMERIC(4,3) NOT NULL,
  normalized_grade       NUMERIC(4,3) NOT NULL,
  disagreement           NUMERIC(4,3) NOT NULL,
  direction              TEXT NOT NULL CHECK (direction IN ('overconfident','underconfident')),
  attribution_confidence NUMERIC(3,2) NOT NULL,
  observed_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_calibration_concept ON calibration_observations(concept_id);
CREATE INDEX idx_calibration_direction ON calibration_observations(direction);
```

### 10.1 Payload shapes

`content_proposals.payload` is JSONB because proposals stage into properly-typed rows on acceptance. The shape per kind mirrors its target table and is validated on write against the constraints in §8.2. A payload that does not validate is a bug in agent output handling, not a proposal to review.

| Kind | Target | Payload keys |
|---|---|---|
| `concept` | `concepts` | slug, title, short_description, long_description, depth, is_load_bearing, estimated_minutes, module_slug, position, metadata |
| `edge` | `concept_edges` | from_slug, to_slug, kind, weight, note |
| `source_link` | `concept_sources` | concept_slug, source_id, chunk_ids, role, note |
| `rubric_criterion` | `rubric_criteria` | concept_slug, slug, prompt, key_points, weight, min_words |
| `assessment_item` | **[STILL OPEN]** | pending assessment-side schema, §6.5 |

Edge and source-link payloads carry concept *slugs* rather than ids, because a batch's edges reference concepts that do not have ids until materialization. Resolution to ids happens at stage 5.

## 11. Interfaces

### 11.1 Consumed

- The ingestion job queue and worker dispatch, as host for §6.0's agents.
- Subsystem 3's retrieval, for the Edge Proposer's search over prior weeks.
- `queue.flag()` in `queue.py:86` for validation failures (§8.2), satisfying the `@ingestion_writer` provenance guard.
- Mastery history for §9.1 step 3. **[STILL OPEN]**

### 11.2 Produced

Rows in `concepts`, `concept_edges`, `concept_sources`, `rubric_criteria`, identical in shape to what hand-authoring produces. Nothing downstream distinguishes a proposed-and-accepted concept from a hand-authored one, which is the point.

Plus calibration observations, consumed by nothing yet except the semester review.

### 11.3 CLI

**There is no `studium` binary.** `pyproject.toml` declares no `[project.scripts]`. Three argparse CLIs are invoked as `python -m studium.ops.cli`, `python -m studium.ingestion.cli`, `python -m studium.eval.cli`, each setting `prog="studium"` so help output reads as one tool while each rejects the others' subcommands.

This is friction rather than a defect, and it is not subsystem 8's to fix. It is recorded because a fourth surface would deepen it, and because any documentation naming a bare `studium` command is describing something that cannot be typed.

**Acquisition commands belong in `studium.ingestion.cli`.** Concept, edge, source-link, and criterion authoring already live there: `ingest graph`, `ingest rubrics`, `ingest sources`, `publish subject`, with `authoring.py` and `publish.py` behind them. Acquisition is automated authoring and joins its neighbours.

**Calibration belongs in `studium.eval.cli`**, which owns assessment-adjacent surfaces.

Proposed, following each module's observed `studium <group> <verb>` shape:

```
# python -m studium.ingestion.cli
studium acquire batch     <subject-slug> --label "Week 4"
studium acquire add       <batch-id> --role lecture_notes --file <path>
studium acquire propose   <batch-id> [--dry-run]
studium acquire review    <batch-id>
studium acquire apply     <batch-id> --yes
studium acquire status    <batch-id>

# python -m studium.eval.cli
studium calibration list   [--subject X] [--direction overconfident] [--limit N]
studium calibration show   <observation-id>
studium calibration export [--subject X] --format csv
```

`apply` rather than `materialize` matches the terser verbs in use (`sync`, `run`, `gate`, `resolve`). The `--yes` flag on the mutating command matches `ingest graph --yes`, `eval sync --yes`, `ops erase-user --yes`.

Exit codes follow the convention the Phase A report identifies as established: `apply` exits non-zero on validation failure, `calibration list` exits non-zero when high-severity observations exist.

## 12. Failure modes

### 12.1 The closed loop
§3. Defences §3.3. Detection §9.

### 12.2 Granularity drift
Inconsistent concept granularity across weeks makes mastery incomparable and the Curator's remaining-work estimates meaningless. *Detection:* §8.3's mean-concepts-per-artifact across batches; a step change is the signal. *Mitigation:* §6.1's calibration context.

### 12.3 Attribution fantasy
The extractor confidently attributes a grade to concepts the work did not test. A lab that was mostly boilerplate with one interesting question gets spread evenly across five concepts, and four spurious observations follow. *Detection:* hard, which is why §9.1 gates on attribution confidence. *Mitigation:* prefer itemized feedback, which attributes far better than a single grade.

### 12.4 Rejection amnesia
§7.5. Recorded here because the symptom is not an error but a slow rise in review time as recurring topics re-propose rejected edges.

### 12.5 Review fatigue
The most consequential failure mode and the one the design cannot prevent by construction.

If the learner bulk-accepts without reading, every review-based defence in §3.3 silently evaporates while the system continues reporting proposals as reviewed. The loop is now fully closed and nothing indicates it.

*Detection.* `proposal_decisions.seconds_spent` and accept-without-edit rate, per subject, across batches. Thirty proposals decided in ninety seconds were not reviewed. *Surfacing.* To the learner, not merely logged: the person best placed to act on "you have stopped reading these" is the person who stopped.

*Mitigation.* Keep the budget sustainable (§7). If review time trends up, cut what is reviewed rather than asking for more minutes.

### 12.6 Submission contamination
§4.3, tested per §13.1. Note the join path: the predicate is not local to `source_chunks`, which has no role column.

### 12.7 Queue flooding from license flags
`licensing.py:213` writes a `license_pending` flag on every upload, severity 2. Five subjects at roughly six artifacts weekly is thirty pending rows a week, against `ingestion_review_queue_depth` firing above twenty pending-or-in-review rows.

Two aggravating facts. Retention deletes resolved rows after one year keyed on `resolved_at`, so pending rows are never aged out. And the alert is a daily email digest with a 48-hour response expectation, so a permanently-firing depth alert trains the operator to ignore it, which is the alert that matters going quiet.

*Resolution required before first real batch.* Either classify at intake so `license_pending` resolves immediately, or batch-resolve after each acquisition run, or revisit the threshold for a deployment whose normal operation includes bulk uploads. This is a decision, not a build task.

### 12.8 Materialization collision
Two proposals in one batch targeting the same unique key: two `source_link` proposals for the same `(concept, source, role)`, two edges for the same `(from, to, kind)`, two criteria for the same `(concept, slug)`. §8.2's pre-check catches these before the transaction, but the proposers should merge rather than emit collisions (§6.2, §6.3).

## 13. Testing

Tier convention verified from the Phase A report: `-m "not postgres"`, `-m postgres`, `-m anthropic`.

**Tier 1.** Payload validation per kind against §10.1 and §8.2's constraint list, including criterion `weight IN (1,2,3)` and edge `weight` in (0,1]. Cycle detection over synthetic graphs. Attribution normalization. Review-routing: given confidence and attestation, is `review_required` correct per §7. Content signature stability. Slug regex conformance.

**Tier 2.** Batch lifecycle through all six stages. Materialization equivalence: an accepted concept proposal produces a row indistinguishable from hand-authored. Atomicity. Calibration creation and as-of lookup. Rejection suppression across two batches. Collision pre-check (§12.8). `flag()` provenance satisfaction for batch-level failures.

**Tier 3.** Each agent against a small pinned corpus of real chunks.

### 13.1 Guards requiring mutation

Per the project's standing requirement that a guard shown only to pass is not evidence:

| Guard | Mutation | Expected |
|---|---|---|
| §4.3 submission invariant | Remove the role predicate from grounding retrieval | Test asserting no submission chunk in grounding results goes red |
| §6.0 job revival | Remove the `suggest_concept_mapping` dispatch branch | Test goes red. A test asserting enum membership passes today with nothing implemented and is worthless |
| §8.2 cycle detection | Accept a batch introducing a cycle | Materialization refuses; graph unchanged |
| §5 stage 5 atomicity | Make one proposal in a batch fail to insert | No proposal from that batch present afterward |
| §7.5 rejection memory | Skip the memory check when building review | Previously rejected proposal reappears; test goes red |
| §9.1 as-of lookup | Use current estimate instead of submission-date estimate | Test goes red — **fixture must include an estimate that moved between the two dates**, or the test passes under both implementations |

The last is the one most likely to be written badly, and its failure mode is precisely catalog entry 4.

## 14. Open questions

Five remain, down from fourteen. Each names the artifact that resolves it.

**Ingest file formats.** `ingest graph`, `ingest rubrics`, `ingest sources` each take a directory. The expected YAML shape is unknown, and it may transform (section addresses to `chunk_ids` almost certainly does). This blocks both Appendix A's corrections and §10.1's payload validation. *Resolves with:* the schema module or example fixtures behind those three commands.

**Assessment-side schema.** §6.5's assessment items have no known target. `studium eval` is system evaluation, not learner assessment. *Resolves with:* `\d+` for whatever holds learner assessments, or confirmation that an assessment is a set of `rubric_criteria`.

**Mastery history granularity.** §9.1 step 3 needs an as-of estimate. *Resolves with:* `\d+` for the mastery tables and whether estimate history is retained or only current state.

**`license_kind` values.** §4.5. *Resolves with:* the enum definition.

**Whether draft criteria are served.** §6.4, §7.2. If the Evaluator filters `status = 'active'`, unsampled criteria are inert and proportional review does not achieve what it intends. *Resolves with:* the Evaluator's criterion query.

Plus two that are decisions rather than lookups: **cost per batch** (six agents, five subjects, fifteen weeks — needs measurement on one real batch before model assignment can be set) and **threshold values** (attribution confidence, disagreement, review routing — should be set from the first weeks' data rather than guessed).

And one deferred: **multi-learner.** Who reviews when the learner is not the operator? Are subjects shared? Does one learner's graded work calibrate another's model? v2 questions; this spec should not be read as having anticipated them.

---

## Appendix A: candidate defects in the hand-authored lambda calculus content

The 25 files delivered on 4 September were authored against the same tables this subsystem materializes into. The schema artifacts reveal mismatches. These are **candidates**, not confirmed defects: the ingest commands may transform between file shape and table shape, and §14's first open question is exactly that. Ranked by likelihood of being real.

Counts corrected while checking: the content has **37 concepts, 21 load-bearing, 59 edges, 20 rubric files** — not the 34 / 18 / ~55 / 18 reported at delivery.

### A.1 Edge weight — almost certainly real

All 59 edges use `strength: N` with N in {3,4,5}. The column is `concept_edges.weight`, `real`, `CHECK (weight > 0 AND weight <= 1)`. Both the key and the range are wrong. Every edge would be rejected.

Correction requires a mapping decision: 5→1.0, 4→0.8, 3→0.6 is the obvious one but is a judgment about what the authored ordinals meant.

### A.2 Concept-source structure — almost certainly real

- **69 links carry `weight: N`.** `concept_sources` has no weight column.
- **Three unique-constraint collisions** against `UNIQUE (concept_id, source_id, role)`: `lambda-arithmetic` has four `primary_exposition` links, `boolean-operators` two, `computability-context` two `history`. Each must become one row with multiple `chunk_ids`.
- **Links address `section: "2.14"`**, while the column is `chunk_ids uuid[]`. This is the one most likely to be a legitimate ingest transform, since sections are the only sane authoring unit.

### A.3 Rubric weight — certainly real

Four criteria carry `weight: 1.5` (`pair-construction.yml` lines 53 and 61, `argument-selection.yml` lines 47 and 56). The CHECK is `weight IN (1,2,3)`. These four rows are rejected at insert.

### A.4 Rubric structure — real, and larger than a rename

The files use a two-level `prompts:` → `criteria:` nesting. The table is flat: one `rubric_criteria` row is one prompt plus its `key_points` array.

Under §6.4's mapping the 20 files yield **77 criteria rows**, each with a `key_points` array drawn from that prompt's 233 nested criteria. Consequences:

- `max_score` per prompt has no column. The 1/2/3 `weight` replaces it, which loses resolution: prompts currently scored 3 through 6.
- `kind` per prompt (24 conceptual, 21 procedural, 17 analytical, 15 application) has no column. §14 records the open question.
- `slug` is required and `UNIQUE (concept_id, slug)`. The files use `id:` on prompts, which is close but not the same key.
- `min_words` is unset everywhere; the default of 20 would apply.
- `reference_sections`, `prerequisites`, and `grading_kind` have no columns.

### A.5 Concept keys — likely real

37 concepts use `module:`. The column is `module_slug`, with a regex CHECK. `position` is never set.

### A.6 Assessments — unknown

The three assessment files have no known target table (§14). They may need restructuring, a new table, or reinterpretation as criteria sets.

### A.7 One coverage gap, unrelated to schema

`names-and-values` is flagged load-bearing and has no rubric file. 21 load-bearing concepts, 20 rubric files.

---

## 15. Version history

**v1.1 — 9 September 2026.** Revised against the four schema and CLI artifacts supplied by the developer. Eleven corrections recorded in §0.2. Resolves v1.0's agent-hosting question by reviving the dead `suggest_concept_mapping` job kind (§6.0). Corrects the rubric model to criteria-hanging-off-concepts with no parent entity (§6.4). Adds §12.7 on license-flag queue flooding and §12.8 on materialization collisions. Adds Appendix A on the hand-authored content's candidate defects. Open questions reduced from fourteen to five.

**v1.0 — 9 September 2026.** Initial specification, written without schema or CLI access, with every reference to an existing object marked unverified. Superseded in §0, §4, §5, §6, §7, §8, §10, §11, §12, §13, §14.

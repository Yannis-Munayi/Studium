# Studium — Subsystem 8: Continuous Content Acquisition

**Version 1.0 — 9 September 2026**

**A specification for the subsystem that turns a week's course materials into pedagogical structure: concepts, prerequisite edges, source links, and rubrics. Proposed by agents, reviewed in proportion to consequence, materialized into the graph the Curator and Evaluator already consume. Includes the calibration loop that compares Studium's mastery estimates against real grades, which is the only part of the system that receives a signal from outside itself.**

---

## 0. A note on verification status

Three consecutive documents in this project have specified columns, enum values, and CLI commands that do not exist. The cause each time was the same: writing about an interface from a model of it rather than from the artifact. The corrections are recorded in the v1.2.1 amendment §2 and in the Phase A build report §9.1.

This spec was written without access to the schema dumps or the CLI tree. Rather than repeat the error, every reference to an existing object is marked:

- **[VERIFIED]** — confirmed by a build report or a prior corrected spec, with the source named.
- **[UNVERIFIED]** — described by role because the actual name and shape are unknown. The developer should treat these as requirements to be satisfied against the real schema, not as names to be matched literally.

New objects introduced by this subsystem are named normally, since nothing constrains them yet. §11 lists exactly what artifacts would let the unverified references be resolved, and §14 records what remains open until they are.

---

## 1. Purpose

Subsystem 5 converts documents into chunks and embeddings. That produces retrieval substrate: the material a Lecturer can cite. It does not produce the structure the rest of the system runs on. The Curator sequences by prerequisite edges. The Evaluator grades against rubric criteria. Mastery accumulates per concept. None of those objects come out of the ingestion pipeline, and for the lambda calculus subject all of them were authored by hand.

Hand-authoring worked for one subject drawn from one book. It does not survive contact with five concurrent courses delivering material weekly. It also makes the system's central claim false. A learning system that can only teach what a human wrote for it is a content management system with a good frontend.

Subsystem 8 closes that gap. Each week, the learner uploads what the course produced. The subsystem proposes the concepts that material introduces, the edges connecting them to what came before, the links from concepts to specific passages, and draft rubrics for the concepts that carry weight. The learner reviews a diff rather than a blank page. Accepted proposals materialize into the same tables hand-authoring wrote to, and everything downstream is unchanged.

The subsystem also does something hand-authoring never could. When the learner uploads graded work, the grade is a measurement of what the learner actually knows, taken by someone outside the system. Comparing that against Studium's own estimate is the only true evaluation the system can perform on itself. §9 specifies that loop, and §3 explains why the design would be unsound without it.

## 2. Scope

**In scope.** Artifact intake and role assignment. Proposal agents for concepts, edges, source links, and rubrics. The review surface and its accept/reject/edit semantics. Materialization into existing graph tables. Extraction of pedagogical signal from graded work. The calibration loop. Rejection memory. Health metrics on the review process itself.

**Out of scope.** Chunking, embedding, and retrieval, which belong to subsystems 5 and 3 and are consumed here unchanged. Session delivery, which is subsystem 2. Assessment grading at attempt time, which is subsystem 6. Transcription of audio to text, which is treated as a preprocessing step with a named interface rather than a component of this subsystem.

**Assumed deployment context.** Single-learner private use for one semester. Multi-learner concerns (proposal review by someone other than the learner, shared subjects across learners, review authority) are deliberately unaddressed. §14 records this as the largest open question for any future version.

## 3. The closed-loop hazard

This is the conceptual core of the subsystem and the reason several later sections are shaped the way they are.

### 3.1 The failure

Consider the naive design. Lecture notes are ingested. An agent reads them and proposes concepts. Another agent reads the same notes and drafts rubrics for those concepts. The learner studies from those notes, answers a rubric prompt, and the Evaluator grades the answer against criteria derived from the notes.

Every step draws on one source. If the proposer misreads the material, the misreading becomes a concept. The rubric tests the misreading. The learner, having studied the same source, answers in the misreading's terms. The Evaluator, applying the misread criteria, awards full marks. The mastery estimate comes out high and confident.

Nothing in that sequence can detect the error, because no step consults anything the error did not already contaminate.

### 3.2 Why this is familiar

The project's anti-pattern catalog records five entries, and this is the sixth instance of the same shape. Every prior entry is a case where the checker and the checked share an origin, so the check cannot fail:

1. Mock agrees with mock while both drift from the server.
2. Fake whose correct behaviour equals the buggy behaviour.
3. Symbol present at every declaration site, absent from every execution site.
4. Test passes when a fake returns a default that matches the bug.
5. Test whose subject shares vocabulary with the code that fixes it.
6. **Content authored from source S, graded by rubrics derived from S, answered by a learner who studied S.**

Entry 6 should be added to the catalog on adoption of this spec, before it is observed rather than after. It is the most consequential of the six, because the other five produce a green test while entry 6 produces a confident learner who is wrong.

### 3.3 Five defences, in order of strength

**Exogenous signal from graded work.** The assessment prompt was written by an instructor. The grade was assigned by an instructor. Neither passed through any part of this system. This is the only genuinely external measurement available and §9 builds the calibration loop on it. Everything else on this list reduces the probability of error; only this one can detect error that has already occurred.

**Instructor feedback as rubric criteria.** When feedback on a submission says the learner conflated third normal form with Boyce-Codd, that sentence is a rubric criterion with external provenance, naming a failure mode observed in the actual course. §6.5 specifies extracting these directly. A rubric built partly from instructor feedback is partly outside the loop.

**Multi-source attestation.** A concept appearing in both the lecture notes and the textbook has survived two independent expositions. A concept appearing in one artifact has not. §6.1 requires the proposer to record which artifacts attest each concept, and §7 routes single-source concepts to mandatory review.

**Human review of structure.** The learner is attending the course. They know whether the professor treats a topic as foundational. §7 spends the review budget on the judgments where this knowledge is decisive and the agents are weakest.

**Learner submissions never ground retrieval.** §4.3 makes this a hard invariant. It is listed last because it prevents a specific severe case rather than the general one, but it is the easiest to enforce and the easiest to test.

## 4. Artifact taxonomy

Seven roles in two families. The distinction between families is load-bearing and is enforced at the retrieval layer, not by convention.

### 4.1 Teaching sources

These may ground retrieval. A Lecturer may cite them. Their content is treated as a claim about the subject.

| Role | Description | Retrieval weight |
|---|---|---|
| `lecture_notes` | Instructor-produced slides, handouts, posted notes. Reflects what this course emphasises. | Highest |
| `textbook_chapter` | Published material. Authoritative on the subject but not necessarily aligned to this course's emphasis. | High |
| `lecture_recording` | Transcript of a recorded session. See §4.4. | Low |

### 4.2 Graded work components

A single assignment produces up to four artifacts. They must remain distinct records rather than being flattened into one document, because they play four incompatible roles.

| Role | Description | May ground retrieval? |
|---|---|---|
| `assessment_prompt` | What was asked. An authentic assessment item written by someone who teaches the course. | No — but see §6.5 |
| `learner_submission` | What the learner produced. Evidence about the learner, not about the subject. | **Never** |
| `instructor_feedback` | The instructor's assessment of the submission. Names real failure modes. | No — but see §6.5 |
| `grade` | Scalar or structured score, with the scale it was awarded on. | No |

### 4.3 The submission invariant

**A `learner_submission` artifact's chunks must never be returned by retrieval as grounding for instruction.**

The reason is direct. If the learner's answer was wrong, and that answer is later retrieved as source material for a lecture on the same topic, the system teaches the learner their own error, cited as though it were authority. The learner has no way to detect this, because the error is already in the shape they think in.

This is enforced at the retrieval query, not by omitting the chunks. The submission is chunked and embedded like anything else, because §6.5 needs to reason over it and §9 needs it for attribution. It is excluded from grounding retrieval by an explicit predicate on artifact role.

The guard on this invariant must be mutation-tested per §13: remove the predicate, and a test asserting that a submission chunk cannot appear in grounding results must go red. A test that only asserts correct results on a corpus containing no submissions cannot detect the deletion.

### 4.4 Recordings

Transcripts are low-density. A fifty-minute lecture yields perhaps six thousand words, of which a substantial fraction is administrative, digressive, or repetitive. Weighting a transcript equally with the notes would dilute retrieval with material that says less per token.

Two consequences. Transcripts receive the lowest retrieval weight of the teaching sources. And one class of moment is extracted separately, because it exists nowhere else: the instructor signalling assessment relevance. Some version of "this will be on the exam", "you should be comfortable with this", "I always ask something like this" is the highest-value few seconds in a recording, appears in no slide deck, and is a direct signal about which concepts are load-bearing. §6.6 specifies the extractor.

Audio-to-text conversion is a preprocessing step outside this subsystem. The interface is: a recording artifact arrives with its transcript already attached, with timestamps at least at paragraph granularity. How the transcript was produced is not this subsystem's concern.

### 4.5 Rights and classification

Every artifact is classified on intake. For the private single-learner deployment, course materials are classified as private-use-only: usable for instruction within this deployment, not publishable, not redistributable.

The learner's instruction that appropriate consent exists for materials they upload is taken at face value. The classification requirement is not a consent check. It records the posture of each source at the time it entered the system, so that a later decision about whether anything can be shared has a record to consult instead of a reconstruction to perform. The existing source classification workflow is reused. **[UNVERIFIED — the classification vocabulary and the command that applies it need to match subsystem 5's actual implementation; the Phase A build report §9.1 establishes that at least one classification value, `public_domain`, is in use.]**

## 5. The weekly cycle

Six stages. Stages 1 through 3 are mechanical. Stage 4 is where the learner spends time. Stages 5 and 6 are mechanical again.

**Stage 1 — Batch open.** The learner opens a batch against a subject, tagged with a week or unit label. A batch is the unit of review and the unit of provenance: every proposal traces to the batch that produced it.

**Stage 2 — Artifact intake.** Files are added to the batch with an explicit role from §4. Role assignment is the learner's declaration, not an inference. Misfiling a submission as lecture notes defeats §4.3, so the intake path should make role explicit and should warn when a role is unusual for the file (a four-file upload with no `assessment_prompt` among them is fine; an `instructor_feedback` with no `learner_submission` in the same batch is probably a mistake).

**Stage 3 — Ingest and propose.** Artifacts pass through the existing subsystem 5 pipeline to become chunks and embeddings. **[UNVERIFIED — the Phase A build report §9.1 establishes the commands are `studium ingest graph`, `ingest rubrics`, `ingest sources`; whether an artifact-level ingest entry point exists is unknown.]** The proposal agents of §6 then run in sequence, writing to a staging table rather than to the live graph.

**Stage 4 — Review.** The learner works through the review surface of §7. Target budget: fifteen minutes per subject per week.

**Stage 5 — Materialize.** Accepted proposals are written into the concept, edge, source-link, and rubric tables. Rejections are recorded in rejection memory (§7.5). Cycle detection and orphan detection run before any write, and a batch failing cycle detection is not partially materialized.

**Stage 6 — Calibrate.** If the batch contained graded work, §9's comparison runs and any calibration observations are recorded.

A batch is atomic at stage 5. Either all accepted proposals materialize or none do, because a prerequisite edge referencing a concept that failed to insert leaves the graph in a state the Curator will traverse into and fail on.

## 6. Proposal agents

Six agents. They are separate rather than one agent with stages, because separate prompts have separate failure modes that can be separately evaluated, and because the review surface treats their outputs differently.

Model assignment is left open pending cost measurement (§14). The reasoning-heavy agents (edge proposal, rubric drafting, graded work extraction) plausibly require the strongest available model; the extraction-heavy ones (source links, exam signal) may not.

### 6.1 Concept Proposer

**Input.** The batch's teaching-source chunks. The existing concept list for the subject, slugs and titles only, to bound context growth as a subject accumulates.

**Output.** Proposed concepts, each with slug, title, short and long description, proposed depth, proposed load-bearing flag, a confidence value, the chunk references that support it, and the set of artifacts attesting it.

**Requirements.**

Deduplication against the existing graph, by slug and by semantic similarity of title and description. A subject that accumulates `normalization`, `database-normalization`, and `normal-forms` as three concepts across three weeks has a graph the Curator cannot sequence.

Granularity consistency. The proposer must be given the subject's existing concepts as calibration for how finely to cut. Without it, one week yields three broad concepts and the next yields fifteen narrow ones, and the mastery model becomes incomparable across weeks. §12.2 records this as an expected failure mode with a detection metric.

Attestation recording. Which artifacts support this concept, so §7 can route single-source concepts to mandatory review.

### 6.2 Edge Proposer

The most important agent and the one with the hardest job.

**Input.** The accepted concepts from this batch. The **full** existing graph for the subject, concepts and edges. Retrieval access over prior weeks' chunks.

**Output.** Proposed edges with from-concept, to-concept, kind, strength, and a required rationale.

**Requirements.**

The proposer must actively seek edges into the existing graph. This is the whole reason the agent gets the full graph as context. Week nine's B-trees depend on week three's disk access cost model, and nothing in week nine's slides mentions it. The dependency is real, the Curator needs it, and only something holding both weeks at once can propose it.

The rationale field is not decoration. It is what the learner reads during review, and reviewing an edge without knowing why it was proposed is guessing rather than judging. An edge whose rationale amounts to restating the edge should be treated as low-confidence.

Cycle safety. The proposer should not knowingly propose a cycle, but the guarantee lives in stage 5's detection rather than in the agent's care.

### 6.3 Source Link Proposer

**Input.** Accepted concepts and the batch's teaching-source chunks.

**Output.** Concept-to-chunk links with a role tag and a weight.

The role vocabulary established during lambda calculus authoring: `canonical_definition`, `primary_exposition`, `worked_example`, `contrast`, `application`, `history`, `reference`. **[UNVERIFIED — this vocabulary was used in the authored content files; whether the ingest path enforces it as an enum or accepts free text is unknown.]**

This agent's output is auto-accepted (§7.3). The stakes are low and the correction path is natural: bad links produce visibly bad citations, and the learner notices during a session rather than during review.

### 6.4 Rubric Drafter

**Input.** An accepted load-bearing concept, its source chunks, and any instructor feedback in the batch that touches the concept.

**Output.** A rubric with three or four prompts of mixed kinds and per-criterion grading, matching the structure of the hand-authored lambda calculus rubrics.

**Requirements.**

Where instructor feedback touching this concept exists, the drafter must incorporate the failure modes it names as criteria. This is the injection point for external provenance identified in §3.3 and it is the single most valuable thing this agent does. A criterion reading "does not conflate 3NF with BCNF, a distinction the instructor's feedback on lab 4 identified as commonly missed" is grounded in something no part of this system produced.

Prompt kind mixing, following the established pattern: procedural, conceptual, application, analytical. A rubric of four conceptual prompts tests one face of a concept four times.

### 6.5 Graded Work Extractor

Operates on the four-component structure of §4.2. Privileged status: its outputs derive from exogenous signal and §7.4 treats them accordingly.

**Outputs, four kinds.**

*Concept attribution.* Which concepts does this work exercise, and in what proportion? Each attribution carries a confidence. §9 consumes this and is honest about its difficulty: a lab covering five topics and receiving one grade does not cleanly attribute.

*Failure modes.* Extracted from `instructor_feedback`. These become candidate rubric criteria per §6.4 and are the highest-value output of the subsystem.

*Assessment items.* The `assessment_prompt` is an authentic assessment item. It becomes a candidate problem for the subject's assessment pool, subject to the obvious constraint that a prompt the learner has already seen and received feedback on is not a fresh assessment of that learner.

*Calibration input.* The grade, its scale, and the submission date, passed to §9.

**The submission's role.** The extractor reads `learner_submission` to understand what the feedback is responding to. The submission informs the extractor's reasoning; it does not become teaching material. §4.3 holds.

### 6.6 Exam Signal Extractor

**Input.** Recording transcripts with timestamps.

**Output.** Timestamped moments where the instructor signals assessment relevance, with the concept each moment refers to and a strength estimate.

Low volume, high value. Feeds the load-bearing flag proposal in §6.1 and the assessment weighting in subsystem 6. An instructor saying "you will see something like this on the midterm" is better evidence of what matters than any structural inference from the material.

## 7. Proportional review

Review everything and the subsystem has relocated the authoring burden rather than removed it. Review nothing and §3's closed loop is fully closed. The design question is which judgments are worth the learner's minutes.

**Budget: fifteen minutes per subject per week.** Five subjects is seventy-five minutes weekly. That is real but survivable. If actual review time substantially exceeds it, the design has failed and should be revised rather than endured, because §12.5 explains what happens when review becomes a chore.

### 7.1 Always reviewed

**Every proposed edge touching an existing concept.** These are cross-week structural claims. They compound: an edge accepted in week four shapes what the Curator does in weeks five through thirteen. The learner is attending the course and has decisive knowledge the agent lacks. This is where the budget goes.

**Every load-bearing flag.** The flag drives rubric authoring priority and mastery gating. Getting it wrong in either direction is expensive: a false negative means no rubric for something the exam tests, a false positive means effort spent on something peripheral.

**Every single-source concept.** Per §3.3, a concept attested by only one artifact has survived one exposition.

**Anything the proposer marked low-confidence.** The agents' own uncertainty is a usable signal and should be routed rather than averaged away.

### 7.2 Spot-checked

**Rubric prompts.** A sample per batch: the lowest-confidence drafts plus a random selection. Full review of every rubric is the largest single cost and the least defensible, since rubric errors surface naturally when the learner answers a prompt that does not make sense.

**Multi-source concepts with high confidence.** Presented as a collapsed list. The learner scans titles and expands anything surprising.

### 7.3 Auto-accepted

**Concept-source links.** Low stakes, natural correction path.

**Edges among only-new concepts.** These compound less than cross-week edges, because a future week attaching to them will surface them again in a reviewed context. Presented in a collapsed "also proposed" list that can be expanded but does not require action.

### 7.4 Never auto-accepted, regardless of confidence

**Anything contradicting a prior rejection** (§7.5).

**Anything derived from graded work that would change an existing accepted object.** Graded-work-derived proposals carry external provenance and should generally be trusted, but a proposal that rewrites an already-accepted rubric criterion is a claim that a prior decision was wrong, and that is the learner's call.

### 7.5 Rejection memory

Every rejection is recorded with enough shape to recognise a re-proposal: the proposal kind, its content signature, and optionally the learner's reason.

Before surfacing a proposal, the review builder checks rejection memory. A proposal matching a prior rejection is either suppressed or, if it differs materially, surfaced with the prior rejection shown alongside it.

Without this, the same wrong edge is proposed every week the topic recurs, and the learner rejects it every week, and the review budget is consumed by a conversation the system is incapable of remembering.

## 8. Incremental graph growth

### 8.1 The cross-week problem

Stated once more because it is the substantive difficulty. Course material arrives in temporal order. Conceptual dependencies do not respect it. A topic introduced in week nine may depend on a cost model from week three that week nine's material treats as assumed background and never names.

Three mechanisms, none sufficient alone. The Edge Proposer holds the full existing graph (§6.2). It has retrieval over prior weeks' chunks to find candidate prerequisite material. And the learner, who has attended both weeks, reviews every cross-week edge (§7.1).

### 8.2 Validation before materialization

**Cycle detection.** Prerequisite edges must form a directed acyclic graph. A cycle makes the Curator's sequencing non-terminating or arbitrary depending on its traversal. Detection runs over the union of the existing graph and the batch's accepted edges. A batch introducing a cycle fails materialization whole; §5 stage 5 is atomic for this reason.

**Orphan detection.** A concept with no edges in either direction is suspicious. Occasionally genuine; usually the Edge Proposer failed to connect something. Orphans do not block materialization but are surfaced in the batch report.

**Depth consistency.** A concept whose proposed depth is far from its prerequisites' depths suggests a granularity error. Warning, not a block.

### 8.3 Graph growth metrics

Recorded per batch, because trends here are early warning for §12.2:

- Concepts added, edges added, edges into existing concepts as a fraction of total.
- Mean concepts per artifact, tracked across batches within a subject.
- Orphan count.
- Maximum prerequisite chain depth.

A subject where the cross-week edge fraction trends toward zero is accumulating disconnected weekly islands rather than a graph.

## 9. The calibration loop

The only part of the system that receives a signal from outside itself.

### 9.1 Mechanism

1. Graded work enters with a grade, a scale, and a submission date.
2. §6.5 attributes concepts with weights and confidences.
3. For each concept above the attribution confidence threshold, the mastery estimate **as of the submission date** is retrieved. As-of matters: comparing against today's estimate measures nothing, since the learner has studied since. **[UNVERIFIED — whether mastery history is retained at sufficient granularity to reconstruct a past estimate is unknown; if not, this requires either a mastery snapshot at submission time or a change to mastery record retention.]**
4. The grade is normalized to [0,1] against its scale.
5. Disagreement is computed per concept.
6. Where disagreement exceeds threshold and attribution confidence is sufficient, a calibration observation is recorded.

### 9.2 Direction matters

**Overconfident** (estimate above grade) is the dangerous direction. Studium said the learner knew it; the instructor's measurement says otherwise. This is the failure that costs exam marks, and it is exactly the outcome §3.1's closed loop produces.

**Underconfident** (estimate below grade) is less costly but still informative. It suggests an over-strict rubric, or evidence the mastery model is failing to credit.

### 9.3 Response

- Single observation: recorded, no action.
- Repeated overconfidence on one concept: the concept's rubric is flagged for review. A rubric that awards marks the instructor does not is measuring something other than the concept.
- Systematic overconfidence across a subject: the subject's mastery model parameters are flagged. **[UNVERIFIED — whether BKT parameters are per-subject configurable or global is unknown.]**
- Attribution confidence consistently low across a subject: §6.5's extractor is failing on this material, which is itself a finding.

### 9.4 The semester as an evaluation

At semester end, the accumulated calibration observations are the evaluation dataset. Not an anecdotal impression of whether Studium helped, but a per-concept record of where its confidence matched an external measurement and where it did not.

This is the strongest evidence the project can produce about whether the thesis holds. It should be treated as the semester's primary output, and the observation record should be designed to survive and be exportable rather than being an operational side effect.

## 10. Data model

New tables. Column conventions follow what the corrected v1.2 data layer spec established **[VERIFIED — `uuid_generate_v7()` primary keys and `created_at`/`updated_at` timestamp columns appear throughout the corrected v1.2 DDL]**, but every foreign key into an existing table is marked because the referenced column names require confirmation.

```sql
CREATE TYPE acquisition_artifact_role AS ENUM (
  'lecture_notes', 'textbook_chapter', 'lecture_recording',
  'assessment_prompt', 'learner_submission',
  'instructor_feedback', 'grade'
);

CREATE TYPE proposal_kind AS ENUM (
  'concept', 'edge', 'source_link', 'rubric',
  'assessment_item', 'rubric_criterion'
);

CREATE TYPE proposal_status AS ENUM (
  'pending', 'accepted', 'rejected', 'edited', 'superseded', 'suppressed'
);

CREATE TABLE acquisition_batches (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  subject_id      UUID NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
  -- [UNVERIFIED] subjects PK column name
  label           TEXT NOT NULL,          -- 'Week 4', 'Unit 2'
  opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  proposed_at     TIMESTAMPTZ,
  reviewed_at     TIMESTAMPTZ,
  materialized_at TIMESTAMPTZ,
  metrics         JSONB NOT NULL DEFAULT '{}'::jsonb,   -- §8.3
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE acquisition_artifacts (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  batch_id      UUID NOT NULL REFERENCES acquisition_batches(id) ON DELETE CASCADE,
  role          acquisition_artifact_role NOT NULL,
  source_id     UUID REFERENCES sources(id),
  -- [UNVERIFIED] set once subsystem 5 ingestion produces a source row
  filename      TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  transcript_of UUID REFERENCES acquisition_artifacts(id),   -- recordings
  metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_acquisition_artifacts_batch ON acquisition_artifacts(batch_id);
CREATE INDEX idx_acquisition_artifacts_role ON acquisition_artifacts(role);

CREATE TABLE content_proposals (
  id              UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  batch_id        UUID NOT NULL REFERENCES acquisition_batches(id) ON DELETE CASCADE,
  kind            proposal_kind NOT NULL,
  status          proposal_status NOT NULL DEFAULT 'pending',
  payload         JSONB NOT NULL,        -- shape per kind, §10.1
  confidence      NUMERIC(3,2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  rationale       TEXT,
  agent_name      TEXT NOT NULL,
  attesting_artifacts UUID[] NOT NULL DEFAULT '{}',
  content_signature TEXT NOT NULL,       -- for rejection matching, §7.5
  review_required BOOLEAN NOT NULL,      -- computed per §7
  materialized_id UUID,                  -- the row created on accept
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_content_proposals_batch_status ON content_proposals(batch_id, status);
CREATE INDEX idx_content_proposals_signature ON content_proposals(content_signature);

CREATE TABLE proposal_decisions (
  id            UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  proposal_id   UUID NOT NULL REFERENCES content_proposals(id) ON DELETE CASCADE,
  decision      proposal_status NOT NULL,
  edited_payload JSONB,                  -- present when decision = 'edited'
  reason        TEXT,
  decided_by    UUID NOT NULL REFERENCES users(id),
  -- [VERIFIED] users.id; the owner column on portfolio_items is user_id,
  -- per Phase A build report §3.2
  decided_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE graded_work (
  id                  UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  batch_id            UUID NOT NULL REFERENCES acquisition_batches(id) ON DELETE CASCADE,
  prompt_artifact_id     UUID REFERENCES acquisition_artifacts(id),
  submission_artifact_id UUID REFERENCES acquisition_artifacts(id),
  feedback_artifact_id   UUID REFERENCES acquisition_artifacts(id),
  grade_value         NUMERIC,
  grade_scale_max     NUMERIC,
  grade_raw           TEXT,              -- 'B+', 'Satisfactory'
  submitted_at        DATE NOT NULL,     -- as-of date for §9.1 step 3
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE graded_work_concepts (
  id             UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  graded_work_id UUID NOT NULL REFERENCES graded_work(id) ON DELETE CASCADE,
  concept_id     UUID NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  -- [UNVERIFIED] concepts PK column name
  attribution_weight     NUMERIC(3,2) NOT NULL CHECK (attribution_weight BETWEEN 0 AND 1),
  attribution_confidence NUMERIC(3,2) NOT NULL CHECK (attribution_confidence BETWEEN 0 AND 1),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (graded_work_id, concept_id)
);

CREATE TABLE calibration_observations (
  id                 UUID PRIMARY KEY DEFAULT uuid_generate_v7(),
  graded_work_id     UUID NOT NULL REFERENCES graded_work(id) ON DELETE CASCADE,
  concept_id         UUID NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
  estimate_at_submission NUMERIC(4,3) NOT NULL,
  normalized_grade   NUMERIC(4,3) NOT NULL,
  disagreement       NUMERIC(4,3) NOT NULL,   -- estimate - grade, signed
  direction          TEXT NOT NULL CHECK (direction IN ('overconfident','underconfident')),
  attribution_confidence NUMERIC(3,2) NOT NULL,
  observed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_calibration_concept ON calibration_observations(concept_id);
CREATE INDEX idx_calibration_direction ON calibration_observations(direction);
```

### 10.1 Proposal payload shapes

`content_proposals.payload` is JSONB because proposals are staging records that materialize into properly-typed rows on acceptance. The shape per kind mirrors the target table and is validated on write. A proposal whose payload does not validate against its kind's schema is a bug in the agent's output handling, not a proposal to be reviewed.

Payload shapes are omitted here rather than guessed, since each mirrors an existing table whose columns are unverified. They should be written against the real schema during implementation and recorded in the build report.

## 11. Interfaces

### 11.1 What this subsystem consumes

- Subsystem 5's ingestion pipeline, to turn artifacts into chunks and embeddings. **[UNVERIFIED — entry point unknown.]**
- Subsystem 3's retrieval, for the Edge Proposer's search over prior weeks.
- Subsystem 2's agent runtime, to host the six proposal agents. **[UNVERIFIED — whether the runtime's effect system accommodates agents that write to a staging table rather than emitting session effects is an open design question; see §14.]**
- Mastery history, for §9.1 step 3. **[UNVERIFIED, and flagged as a possible schema change.]**

### 11.2 What this subsystem produces

Rows in the concept, edge, source-link, and rubric tables, identical in shape to what hand-authoring produced. Nothing downstream distinguishes a proposed-and-accepted concept from a hand-authored one, which is the point.

Plus calibration observations, which are new and consumed by nothing yet except the semester review.

### 11.3 CLI surface

**[PROPOSED — not verified against the actual command tree. The Phase A build report §9.1 establishes that `studium ingest graph|rubrics|sources` and `studium publish subject <slug>` exist and that `studium content sync`, `studium subject activate`, and `studium assessment activate` do not. The commands below follow the observed `studium <group> <verb>` shape but the group name, verb names, and flag names should be set by the developer to match existing conventions rather than adopted from this document.]**

```
studium acquire batch new <subject-slug> --label "Week 4"
studium acquire artifact add <batch-id> --role lecture_notes --file <path>
studium acquire propose <batch-id>
studium acquire review <batch-id>
studium acquire materialize <batch-id>
studium acquire status <batch-id>
studium acquire calibration list [--subject <slug>] [--direction overconfident]
studium acquire calibration export [--subject <slug>] --format csv
```

Exit code convention: following the convention the Phase A report identifies as established in this CLI, `materialize` exits non-zero when validation fails, and `calibration list` exits non-zero when high-severity observations exist.

## 12. Failure modes

### 12.1 The closed loop

§3. Defences in §3.3. Detection is §9.

### 12.2 Granularity drift

The Concept Proposer cuts at inconsistent granularity across weeks. Three fat concepts one week, fifteen thin ones the next. Mastery becomes incomparable across the subject and the Curator's estimates of remaining work become meaningless.

*Detection.* §8.3's mean-concepts-per-artifact metric, tracked across batches. A step change is the signal.

*Mitigation.* §6.1 requires the existing concept set as calibration context.

### 12.3 Attribution fantasy

The Graded Work Extractor confidently attributes a grade to concepts the work did not meaningfully test. A lab that was mostly boilerplate with one interesting question gets attributed evenly across five concepts, and four spurious calibration observations follow.

*Detection.* Hard, which is why §9.1 gates on attribution confidence rather than trusting it. Low-confidence attributions do not produce observations.

*Mitigation.* Prefer itemized instructor feedback when available, since per-item feedback attributes far better than a single grade.

### 12.4 Rejection amnesia

Covered by §7.5. Recorded here because without the memory the symptom is not an error message but a slow rise in review time as recurring topics re-propose the same rejected edges.

### 12.5 Review fatigue

The most consequential failure mode, and the one the design cannot prevent by construction.

If the learner begins bulk-accepting without reading, every review-based defence in §3.3 silently evaporates. The system continues reporting that proposals were reviewed. The loop is now fully closed and nothing indicates it.

*Detection.* Track accept-without-edit rate and mean seconds per reviewed proposal, per subject, across batches. A learner who reviewed thirty proposals in ninety seconds did not review them. These are health metrics on the review process itself and should be surfaced to the learner rather than merely logged, because the person best placed to act on "you have stopped reading these" is the person who stopped.

*Mitigation.* Keep the budget small enough to be sustainable (§7). If review time is trending up, cut what is reviewed rather than asking for more minutes.

### 12.6 Submission contamination

A `learner_submission` reaches grounding retrieval, and the system teaches the learner their own error with a citation. Prevented by §4.3, tested per §13.

## 13. Testing requirements

Following the project's tier convention **[VERIFIED — Phase A build report §7 uses markers `-m "not postgres"`, `-m postgres`, `-m anthropic`]**.

**Tier 1 (no database).** Proposal payload validation per kind. Cycle detection over synthetic graphs. Attribution weight normalization. Review-routing logic: given a proposal with known confidence and attestation, is `review_required` computed correctly per §7. Content signature stability for rejection matching.

**Tier 2 (real Postgres).** Batch lifecycle through all six stages. Materialization correctness: an accepted concept proposal produces a concept row equivalent to what hand-authoring would have written. Atomicity: a batch containing one invalid proposal materializes nothing. Calibration observation creation and its as-of estimate lookup. Rejection memory suppression across two batches.

**Tier 3 (real model calls).** Each proposal agent against real chunks from real material. Expensive, so a small pinned corpus rather than the full subject.

### 13.1 Guards that must be mutation-tested

Per the project's standing requirement that a guard shown only to pass is not evidence, each of these must be broken deliberately and observed to go red:

| Guard | Mutation | Expected |
|---|---|---|
| §4.3 submission invariant | Remove the artifact-role predicate from the grounding retrieval query | A test asserting no submission chunk appears in grounding results goes red |
| §8.2 cycle detection | Accept a batch introducing a cycle | Materialization refuses; graph unchanged |
| §5 stage 5 atomicity | Make one proposal in a batch fail to insert | No proposal from that batch is present afterward |
| §7.5 rejection memory | Skip the rejection-memory check when building review | A previously rejected proposal reappears; test goes red |
| §9.1 as-of lookup | Use the current estimate instead of the submission-date estimate | A test where the learner's estimate changed after submission goes red |

The fifth is the one most likely to be written badly. A test where the estimate did not change between submission and observation passes under both the correct and mutated implementation, which makes it worthless. The fixture must include an estimate that moved.

## 14. Open questions

**Schema and CLI verification.** Everything marked [UNVERIFIED] in this document. Resolvable with: the `studium --help` subcommand tree; `\d+` for `sources`, `source_chunks`, `concepts`, `concept_edges`, `concept_sources`, `rubrics`, `rubric_criteria`, `subjects`; the `ingestion_review_queue` schema and its current writers; and whatever governs mastery history retention.

**Agent hosting.** Whether subsystem 2's runtime can host agents that write to a staging table rather than emitting session effects, or whether these run outside the session machinery entirely. This is a genuine design question, not a lookup, and it should be settled before implementation rather than during.

**Cost per batch.** Six agents over a week of material, five subjects, fifteen weeks. Needs measurement on one real batch before the shape of the semester's spend is known. Model assignment per agent (§6) depends on the answer.

**Threshold values.** Attribution confidence, disagreement, review-routing confidence. All currently unset. They should be set from the first few weeks' data rather than guessed now, and this spec deliberately does not invent numbers for them.

**Review budget in practice.** Fifteen minutes per subject per week is a target, not a measurement. If week one takes an hour, §7's allocation is wrong and should be revised immediately rather than after a semester of overrun.

**Multi-learner.** Entirely unaddressed. Who reviews proposals when the learner is not the operator? Are subjects shared or per-learner? Does one learner's graded work calibrate another's mastery model? These are v2 questions and this spec should not be read as having anticipated them.

**The retained-credential parallel.** The Phase A report notes that a retained credential permanently names a learner id in its signed payload. Graded work carries an analogous property: instructor feedback on a submission is a third party's assessment of the learner, stored durably. For private single-learner use this is unremarkable. It should be revisited before any deployment where the learner is not the operator.

## 15. Version history

**v1.0 — 9 September 2026.** Initial specification. Written without access to the schema dumps and CLI tree; all references to existing objects marked [VERIFIED] or [UNVERIFIED] per §0. Introduces anti-pattern 6 (self-authored content graded by self-authored rubrics from the same source) and the calibration loop as its primary defence.

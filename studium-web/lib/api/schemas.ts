/**
 * The API boundary (spec §4 "Schema validation", §5.3).
 *
 * Every response the backend gives us is parsed here before any component sees
 * it. Inside the app, a `Citation` is a `Citation`; at the boundary it is
 * untrusted JSON. That is the whole rule.
 *
 * These schemas are written against the *shipped* FastAPI surface
 * (`backend/studium/api/app.py`), not against the spec's sketch of it. Where
 * the two disagree the divergence is recorded in DIVERGENCES-FRONTEND.md and
 * the schema follows the code -- a schema that matches a document rather than
 * the server it parses is a validation layer that fails on every real response.
 */
import { z } from "zod";

export const uuid = z.string().uuid();
export type UUID = z.infer<typeof uuid>;

// --- session lifecycle -----------------------------------------------------

/**
 * The runtime states from `orchestration/state_machine.py`.
 *
 * Enumerated rather than left as `string` because §9.2 keys the contextual
 * primitive buttons off this value: a state the frontend does not know about
 * must fail loudly at the boundary, not silently render an empty toolbar.
 */
export const sessionState = z.enum([
  "IDLE",
  "OPENING",
  "LECTURING",
  "TUTORIAL",
  "LAB",
  "REVIEW",
  "INTERRUPTED",
  "PAUSED_FOR_QUESTION",
  "OFFICE_HOURS",
  "SUMMATIVE_ASSESSMENT",
  "CLOSING",
]);
export type SessionState = z.infer<typeof sessionState>;

/** The persisted `session_mode` values (data layer §6.0). */
export const sessionMode = z.enum([
  "lecture",
  "tutorial",
  "lab",
  "review",
  "office_hours",
  "summative_assessment",
  "orientation",
]);
export type SessionMode = z.infer<typeof sessionMode>;

export const startSessionResponse = z.object({
  session_id: uuid,
  state: sessionState,
  /**
   * The mode the session actually has, which is not always the one requested.
   *
   * The runtime allows one active session per learner (§20), so opening a
   * second returns the first — whatever mode *it* was started in. Navigating
   * with the requested mode instead produced a classroom that believed it was
   * lecturing while the runtime was running a tutorial, and the two disagreed
   * about which agent every turn should go to. See DIVERGENCES-FRONTEND.md F22.
   *
   * Defaulted rather than required so a backend that predates the field does
   * not fail the parse; the caller falls back to what it asked for.
   */
  mode: sessionMode.optional(),
  /** True when an existing active session was returned instead of a new one. */
  resumed: z.boolean().default(false),
});
export type StartSessionResponse = z.infer<typeof startSessionResponse>;

export const interruptResponse = z.object({
  accepted: z.boolean(),
  // Not `sessionState`: the backend answers "unknown" when no Orchestrator is
  // resident for this session, which is a real answer and not a state.
  state: z.union([sessionState, z.literal("unknown")]),
  detail: z.string(),
});
export type InterruptResponse = z.infer<typeof interruptResponse>;

export const closeResponse = z.object({
  session_id: uuid,
  ended: z.boolean(),
  summarised: z.boolean(),
  errors: z.array(z.string()).default([]),
});
export type CloseResponse = z.infer<typeof closeResponse>;

export const stateTransition = z.object({
  from: z.string(),
  event: z.string(),
  to: z.string(),
  effect: z.string(),
  at: z.string(),
});

export const sessionStateResponse = z.object({
  session_id: uuid,
  state: sessionState,
  persisted_mode: z.string().nullable(),
  exchange_index: z.number().int(),
  interruptible: z.boolean(),
  transitions: z.array(stateTransition).default([]),
});
export type SessionStateResponse = z.infer<typeof sessionStateResponse>;

// --- citations (retrieval §12) ---------------------------------------------

/**
 * One resolved `[Pn]` marker.
 *
 * `source_deleted` is the retired-source flag §10.3 renders against. The
 * backend withholds `excerpt` for those rows rather than trusting the client to
 * hide it, so a retired source cannot leak through a rendering bug -- which
 * means `excerpt` is nullable here and the component must handle it.
 */
export const citation = z.object({
  marker: z.string(),
  chunk_id: uuid,
  source_id: uuid,
  source_title: z.string(),
  source_authors: z.array(z.string()).default([]),
  page_start: z.number().int().nullable(),
  page_end: z.number().int().nullable(),
  section_path: z.string().nullable(),
  excerpt: z.string().nullable(),
  excerpt_start_offset: z.number().int().nullable(),
  excerpt_end_offset: z.number().int().nullable(),
  source_deleted: z.boolean(),
});
export type Citation = z.infer<typeof citation>;

export const artifactCitationsResponse = z.object({
  artifact_id: uuid,
  citations: z.array(citation).default([]),
});
export type ArtifactCitationsResponse = z.infer<typeof artifactCitationsResponse>;

// --- streaming (agent runtime §6, §20) -------------------------------------

/**
 * Chunk kinds, from `agents/base.py::StreamChunk`.
 *
 * **`degraded` is in this list and is not in spec §8.1.** It is the chunk that
 * carries learner-visible copy when a call failed past its retries, and a
 * consumer built to §8.1's four kinds would drop it on the floor -- the exact
 * silent failure §3 forbids. See DIVERGENCES-FRONTEND.md F2.
 */
export const chunkKind = z.enum(["text", "tool_effect", "trace", "end", "degraded"]);
export type ChunkKind = z.infer<typeof chunkKind>;

export const streamChunk = z.object({
  kind: chunkKind,
  payload: z.record(z.unknown()).default({}),
});
export type StreamChunk = z.infer<typeof streamChunk>;

/**
 * The practice problem `let_me_try_one` puts on the bench (§6.3, §9.3).
 *
 * **The model answer is not in this shape and must not be added to it.** The
 * runtime splits `PracticeProblem` before the chunk leaves the process: the
 * Orchestrator keeps `model_answer` and `expected_key_points` for the Evaluator
 * that grades the next turn, and sends the client the question. §11.2 withholds
 * the answer until the learner has attempted, and the bench having no component
 * that draws it is not the same as the browser never receiving it.
 */
export const practiceProblem = z.object({
  prompt: z.string(),
  difficulty: z.number().int().min(1).max(5).default(3),
  hint: z.string().default(""),
});
export type PracticeProblem = z.infer<typeof practiceProblem>;

/** The `end` payload shapes the runtime actually emits, all fields optional. */
export const endPayload = z.object({
  turn_id: z.string().nullable().optional(),
  segment_index: z.number().int().optional(),
  anchor: z.string().optional(),
  next_state: z.string().optional(),
  primitive: z.string().optional(),
  stance: z.string().optional(),
  verdict: z.string().optional(),
  refocus_concept_id: z.string().nullable().optional(),
  /**
   * Parsed leniently: a `catch` rather than a hard failure, because the problem
   * is one field of a chunk whose other fields drive the transcript. A shape
   * the bench cannot render should cost the bench, not the turn.
   */
  problem: practiceProblem.nullable().optional().catch(null),
  note: z.string().optional(),
  /**
   * The artifact this turn produced, which is what makes §10's hover cards
   * resolvable — `GET /api/artifacts/{id}/citations` has nothing to ask about
   * without it.
   *
   * Optional because most turns have none: the Tutor writes no artifact, and
   * a Lecturer turn whose effect batch rolled back has none to name. Absent
   * means "no source is linked to this segment", which the card says plainly
   * rather than spinning. Closed SD5; see DIVERGENCES-FRONTEND.md F3.
   */
  artifact_id: z.string().nullable().optional(),
  /**
   * The other four rows a turn can produce (agent runtime v1.0.1 §4).
   *
   * Same reason as `artifact_id`, generalised: the effect batch commits before
   * the end chunk is sent, so by the time the client reads these the rows
   * exist and are fetchable. Without them the client's only way to find what
   * a turn wrote is to re-poll a list endpoint and diff it, which is a race
   * and looks like one — a journal entry that appears a second late, or not
   * until the next navigation.
   *
   * All optional, and for the same reason `artifact_id` is: most turns write
   * none of these. Absent means "this turn produced no such row", which is the
   * ordinary case and not an error state.
   */
  journal_entry_id: z.string().nullable().optional(),
  portfolio_item_id: z.string().nullable().optional(),
  review_card_id: z.string().nullable().optional(),
  queue_item_id: z.string().nullable().optional(),
  /**
   * Which turn this was, for correlating the end chunk with the persisted row.
   *
   * Optional rather than required even though §4.2 writes it: a degraded turn
   * emits an end chunk having written no turn row at all, and a required field
   * would make the client reject the one chunk that tells it the turn failed.
   */
  turn_index: z.number().nullable().optional(),
});
export type EndPayload = z.infer<typeof endPayload>;

export const degradedPayload = z.object({
  text: z.string(),
  reason: z.string(),
});
export type DegradedPayload = z.infer<typeof degradedPayload>;

// --- errors ----------------------------------------------------------------

/**
 * The 402 body from the budget gate (agent runtime §19).
 *
 * `reset_at` is an ISO timestamp the UI renders in the learner's timezone;
 * `message` is already learner-facing copy from `studium/copy/degradation.py`,
 * so the frontend shows it verbatim rather than writing a second version of the
 * same sentence in a different tone (§15.4).
 */
export const budgetExceededDetail = z.object({
  message: z.string(),
  scope: z.string(),
  reset_at: z.string(),
});
export type BudgetExceededDetail = z.infer<typeof budgetExceededDetail>;

// --- the desk, the journal and the session summary -------------------------

/**
 * Journal, desk and mastery shapes (spec §6.1, §6.4, §12).
 *
 * These describe the data layer's own columns (`journal_entries`,
 * `session_summaries`, `concept_mastery`), and are now served by
 * `backend/studium/api/reads.py`. They were written before that existed, which
 * is why wiring the endpoints was one client function each — and why two of
 * them were wrong in ways only a real response could reveal. Both corrections
 * are marked below.
 */
export const journalStatus = z.enum(["open", "partial", "resolved", "archived"]);
export type JournalStatus = z.infer<typeof journalStatus>;

export const journalEntry = z.object({
  id: uuid,
  learner_subject_id: uuid,
  concept_id: uuid.nullable(),
  concept_name: z.string().nullable(),
  status: journalStatus,
  summary: z.string(),
  /**
   * §12.1's "written by" attribution. Derived server-side from
   * `journal_entries.origin`: only `learner_flagged` means the learner wrote
   * the summary themselves.
   */
  summary_author: z.enum(["learner", "tutor"]).default("tutor"),
  /**
   * The Confusion-Tracker's inference. **Always null today.** Data layer §11
   * says it is never serialised to a learner; frontend §6.4 asks for it with a
   * "this is the system's guess" framing. The projection that already shipped
   * wins — see DIVERGENCES-FRONTEND.md F16 and `reads.SERVE_HYPOTHESIS_TO_LEARNER`.
   * Kept in the schema because that decision is one constant away from
   * reversing, and the surfaces already render it `if (entry.hypothesis)`.
   */
  hypothesis: z.string().nullable(),
  learner_note: z.string().nullable(),
  first_seen_at: z.string(),
  last_touched_at: z.string(),
});
export type JournalEntry = z.infer<typeof journalEntry>;

/**
 * The `journal_event_kind` enum, all eight values (data layer §6.7).
 *
 * This list had six and two of them were wrong. `addressed` is not a value the
 * database has — it is `partially_addressed` — and `hypothesis_updated` and
 * `learner_note_added` were missing entirely. Since a schema mismatch is a hard
 * failure at the boundary by design, the first revised hypothesis on any entry
 * would have taken the whole detail view down with a "malformed response".
 * Found by serving the endpoint. See DIVERGENCES-FRONTEND.md F17.
 */
export const journalEventKind = z.enum([
  "created",
  "revisited",
  "partially_addressed",
  "resolved",
  "reopened",
  "archived",
  "hypothesis_updated",
  "learner_note_added",
]);
export type JournalEventKind = z.infer<typeof journalEventKind>;

export const journalEvent = z.object({
  id: uuid,
  kind: journalEventKind,
  at: z.string(),
  session_id: uuid.nullable(),
});
export type JournalEvent = z.infer<typeof journalEvent>;

export const journalEntryDetail = journalEntry.extend({
  history: z.array(journalEvent).default([]),
});
export type JournalEntryDetail = z.infer<typeof journalEntryDetail>;

export const masteryDelta = z.object({
  concept_id: uuid,
  concept_name: z.string(),
  before: z.number(),
  after: z.number(),
});
export type MasteryDelta = z.infer<typeof masteryDelta>;

export const sessionSummary = z.object({
  session_id: uuid,
  summary: z.string(),
  concepts_touched: z.array(masteryDelta).default([]),
  open_threads: z.array(z.string()).default([]),
  next_focus_concept_id: uuid.nullable(),
  next_focus_concept_name: z.string().nullable(),
  duration_minutes: z.number(),
  cost_usd: z.number().nullable(),
});
export type SessionSummary = z.infer<typeof sessionSummary>;

export const recentSession = z.object({
  id: uuid,
  mode: sessionMode,
  started_at: z.string(),
  ended_at: z.string().nullable(),
  duration_minutes: z.number().nullable(),
  concepts_touched: z.array(z.string()).default([]),
});
export type RecentSession = z.infer<typeof recentSession>;

export const conceptMastery = z.object({
  concept_id: uuid,
  concept_name: z.string(),
  /** The raw BKT posterior after the most recent evidence. */
  p_known: z.number().min(0).max(1),
  /**
   * The same value under the forgetting curve, recomputed server-side at read
   * time. **This is the one to render.** The Curator, the unlock gate and
   * `suggest_next_unlocked` all read the decayed value (data layer C1), so a
   * desk showing the raw one would summarise a state no decision was made
   * against — a learner would see 0.9 on a concept the system had already
   * decided to revisit. See DIVERGENCES-FRONTEND.md F18.
   */
  p_known_decayed: z.number().min(0).max(1),
});
export type ConceptMastery = z.infer<typeof conceptMastery>;

export const learnerProfile = z.object({
  id: uuid,
  display_name: z.string(),
  timezone: z.string().default("America/Toronto"),
  default_session_minutes: z.number().int().default(90),
  show_cost: z.boolean().default(false),
});
export type LearnerProfile = z.infer<typeof learnerProfile>;

export const deskResponse = z.object({
  learner: learnerProfile,
  open_session: recentSession.nullable(),
  recent_sessions: z.array(recentSession).default([]),
  syllabus_next: z.array(z.object({ id: uuid, name: z.string() })).default([]),
  open_journal_entries: z.array(journalEntry).default([]),
  mastery: z.array(conceptMastery).default([]),
});
export type DeskResponse = z.infer<typeof deskResponse>;

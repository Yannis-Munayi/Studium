import { describe, expect, it } from "vitest";
import {
  artifactCitationsResponse,
  budgetExceededDetail,
  chunkKind,
  citation,
  closeResponse,
  degradedPayload,
  endPayload,
  deskResponse,
  interruptResponse,
  journalEntry,
  journalEntryDetail,
  practiceProblem,
  sessionStateResponse,
  sessionSummary,
  startSessionResponse,
  streamChunk,
} from "@/lib/api/schemas";

/**
 * §17 Tier 1: "Zod schema validation: API response fixtures validate cleanly;
 * malformed fixtures fail cleanly."
 *
 * The fixtures are copied from the shapes `backend/studium/api/app.py` actually
 * returns, not from the spec's description of them. A schema that validates the
 * document and rejects the server is worse than no schema.
 */

const SESSION_ID = "0f8fad5b-d9cb-469f-a165-70867728950e";
const ARTIFACT_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7";
const CHUNK_ID = "16fd2706-8baf-433b-82eb-8c7fada847da";

describe("session lifecycle schemas", () => {
  it("accepts the 201 body from POST /api/session", () => {
    const parsed = startSessionResponse.parse({ session_id: SESSION_ID, state: "TUTORIAL" });
    expect(parsed.state).toBe("TUTORIAL");
  });

  it("rejects a state the runtime does not have", () => {
    // §9.2 keys the contextual buttons off this value, so an unknown state must
    // fail loudly rather than render an empty toolbar.
    const result = startSessionResponse.safeParse({ session_id: SESSION_ID, state: "DAYDREAMING" });
    expect(result.success).toBe(false);
  });

  it("accepts the interrupt body in both its forms", () => {
    expect(
      interruptResponse.parse({
        accepted: true,
        state: "LECTURING",
        detail: "interrupt signalled; the current sentence will finish",
      }).accepted,
    ).toBe(true);

    // The runtime answers "unknown" when no Orchestrator is resident for the
    // session -- a real answer, and not one of the state-machine states.
    expect(
      interruptResponse.parse({
        accepted: false,
        state: "unknown",
        detail: "no active stream for this session on this node",
      }).state,
    ).toBe("unknown");
  });

  it("defaults the close errors list when the server omits it", () => {
    const parsed = closeResponse.parse({
      session_id: SESSION_ID,
      ended: true,
      summarised: true,
    });
    expect(parsed.errors).toEqual([]);
  });

  it("accepts the diagnostic state endpoint's transition log", () => {
    const parsed = sessionStateResponse.parse({
      session_id: SESSION_ID,
      state: "LECTURING",
      persisted_mode: "lecture",
      exchange_index: 3,
      interruptible: true,
      transitions: [
        {
          from: "OPENING",
          event: "context_ready",
          to: "LECTURING",
          effect: "enter_mode_state",
          at: "2026-08-20T14:00:00Z",
        },
      ],
    });
    expect(parsed.transitions).toHaveLength(1);
  });

  it("accepts a null persisted_mode, which INTERRUPTED and PAUSED both have", () => {
    const parsed = sessionStateResponse.parse({
      session_id: SESSION_ID,
      state: "PAUSED_FOR_QUESTION",
      persisted_mode: null,
      exchange_index: 1,
      interruptible: false,
    });
    expect(parsed.persisted_mode).toBeNull();
  });
});

describe("stream chunk schemas", () => {
  it("includes degraded among the chunk kinds", () => {
    // Spec §8.1 lists four kinds and the runtime emits five. Dropping the fifth
    // would drop the learner-visible failure copy. See DIVERGENCES F2.
    expect(chunkKind.options).toContain("degraded");
    expect(chunkKind.options).toHaveLength(5);
  });

  it("parses each kind the runtime emits", () => {
    for (const kind of chunkKind.options) {
      expect(streamChunk.parse({ kind, payload: {} }).kind).toBe(kind);
    }
  });

  it("defaults an absent payload rather than failing", () => {
    expect(streamChunk.parse({ kind: "end" }).payload).toEqual({});
  });

  it("rejects a chunk with no kind", () => {
    expect(streamChunk.safeParse({ payload: { text: "orphan" } }).success).toBe(false);
  });

  it("parses the Lecturer's end payload, with the artifact it produced", () => {
    const parsed = endPayload.parse({
      turn_id: SESSION_ID,
      segment_index: 4,
      anchor: "The redex is chosen leftmost-outermost.",
      artifact_id: ARTIFACT_ID,
    });
    expect(parsed.segment_index).toBe(4);
    // SD5, closed: this is what `GET /api/artifacts/{id}/citations` needs.
    expect(parsed.artifact_id).toBe(ARTIFACT_ID);
  });

  it("still parses an end payload with no artifact id", () => {
    // A Tutor turn produces none, and a Lecturer turn whose effect batch rolled
    // back has none to name. Both are ordinary, not errors.
    expect(endPayload.parse({ turn_id: SESSION_ID }).artifact_id).toBeUndefined();
  });

  it("parses the four other produced ids and the turn index", () => {
    // v1.0.1 §4: the effect batch commits before the end chunk is sent, so
    // every row a turn wrote is nameable by the time the client reads this.
    const parsed = endPayload.parse({
      turn_id: SESSION_ID,
      turn_index: 12,
      journal_entry_id: ARTIFACT_ID,
      portfolio_item_id: ARTIFACT_ID,
      review_card_id: ARTIFACT_ID,
      queue_item_id: ARTIFACT_ID,
    });
    expect(parsed.journal_entry_id).toBe(ARTIFACT_ID);
    expect(parsed.portfolio_item_id).toBe(ARTIFACT_ID);
    expect(parsed.review_card_id).toBe(ARTIFACT_ID);
    expect(parsed.queue_item_id).toBe(ARTIFACT_ID);
    expect(parsed.turn_index).toBe(12);
  });

  it("still parses an end payload that produced none of them", () => {
    // Most turns write nothing. §4 omits absent ids rather than sending nulls,
    // so the ordinary end chunk carries none of these keys at all.
    const parsed = endPayload.parse({ turn_id: SESSION_ID });
    expect(parsed.journal_entry_id).toBeUndefined();
    expect(parsed.review_card_id).toBeUndefined();
  });

  it("parses a degraded turn's end payload, which has no turn index", () => {
    // A degraded turn emits an end chunk having written no turn row. A
    // required turn_index would make the client reject the one chunk that
    // tells it the turn failed.
    expect(endPayload.parse({ turn_id: SESSION_ID }).turn_index).toBeUndefined();
  });

  it("parses the primitive end payloads", () => {
    expect(endPayload.parse({ primitive: "explain_differently", stance: "intuitive" }).stance).toBe(
      "intuitive",
    );
    expect(
      endPayload.parse({
        primitive: "let_me_try_one",
        next_state: "LAB",
        problem: { prompt: "Reduce it.", difficulty: 2, hint: "Outermost first." },
      }).next_state,
    ).toBe("LAB");
    expect(endPayload.parse({ primitive: "im_lost", refocus_concept_id: null }).primitive).toBe(
      "im_lost",
    );
  });

  it("keeps the answer key out of the problem the client receives", () => {
    // §11.2 withholds the model answer until the learner has attempted. The
    // runtime strips it; this asserts the client's shape has no home for it, so
    // a runtime that regressed could not quietly hand one to a component.
    const parsed = practiceProblem.parse({
      prompt: "Reduce (\\x. x x) (\\y. y).",
      difficulty: 2,
      hint: "Outermost first.",
      model_answer: "\\y. y",
      expected_key_points: ["substitute"],
    });
    expect(parsed).not.toHaveProperty("model_answer");
    expect(parsed).not.toHaveProperty("expected_key_points");
  });

  it("degrades an unparseable problem to null without failing the turn", () => {
    const parsed = endPayload.parse({ next_state: "LAB", problem: { nonsense: true } });
    expect(parsed.problem).toBeNull();
    expect(parsed.next_state).toBe("LAB");
  });

  it("parses a degraded payload", () => {
    const parsed = degradedPayload.parse({
      text: "There's high demand right now, so responses are slower than usual.",
      reason: "rate_limit",
    });
    expect(parsed.reason).toBe("rate_limit");
  });
});

describe("citation schemas", () => {
  const base = {
    marker: "P1",
    chunk_id: CHUNK_ID,
    source_id: ARTIFACT_ID,
    source_title: "An Introduction to Functional Programming",
    source_authors: ["Michaelson"],
    page_start: 42,
    page_end: 44,
    section_path: "Chapter 3 › 3.2 Beta Reduction",
    excerpt: "A redex is an application of a lambda abstraction…",
    excerpt_start_offset: 0,
    excerpt_end_offset: 300,
    source_deleted: false,
  };

  it("accepts a resolved citation", () => {
    expect(citation.parse(base).source_title).toContain("Functional Programming");
  });

  it("accepts a retired source with its excerpt withheld", () => {
    // §10.3: the backend withholds the text rather than trusting the client to
    // hide it, so `excerpt` must be nullable or every retired citation fails.
    const parsed = citation.parse({ ...base, source_deleted: true, excerpt: null });
    expect(parsed.source_deleted).toBe(true);
    expect(parsed.excerpt).toBeNull();
  });

  it("accepts an unpaginated source", () => {
    const parsed = citation.parse({ ...base, page_start: null, page_end: null, section_path: null });
    expect(parsed.page_start).toBeNull();
  });

  it("accepts an artifact with no citations", () => {
    // An ungrounded artifact is a real thing to render, not a 404.
    const parsed = artifactCitationsResponse.parse({ artifact_id: ARTIFACT_ID, citations: [] });
    expect(parsed.citations).toEqual([]);
  });

  it("rejects a citation missing its chunk id", () => {
    const { chunk_id: _omitted, ...without } = base;
    expect(citation.safeParse(without).success).toBe(false);
  });

  it("rejects a non-uuid chunk id", () => {
    expect(citation.safeParse({ ...base, chunk_id: "not-a-uuid" }).success).toBe(false);
  });
});

describe("budget error detail", () => {
  it("parses the 402 detail body", () => {
    const parsed = budgetExceededDetail.parse({
      message: "You've reached your daily usage limit. Your progress is saved — resume tomorrow.",
      scope: "daily",
      reset_at: "2026-08-21T04:00:00+00:00",
    });
    expect(parsed.scope).toBe("daily");
  });

  it("rejects a detail with no reset time", () => {
    // Without it the UI cannot say when to come back, which is the one useful
    // thing a budget message contains.
    expect(
      budgetExceededDetail.safeParse({ message: "Over limit.", scope: "daily" }).success,
    ).toBe(false);
  });
});

/**
 * The read surface (§6.1, §6.4, §6.5).
 *
 * Fixtures copied from what `backend/studium/api/reads.py` actually returns,
 * including the two corrections that only a real response could have revealed:
 * `journal_event_kind` has eight values and not the six this file's first
 * version listed, and `concept_mastery` carries the decayed posterior the desk
 * is supposed to render.
 */
describe("desk and journal schemas", () => {
  const ENTRY_ID = "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed";
  const CONCEPT_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301";

  const entry = {
    id: ENTRY_ID,
    learner_subject_id: CONCEPT_ID,
    concept_id: CONCEPT_ID,
    concept_name: "Beta-reduction",
    status: "open",
    summary: "Treats reduction order as significant for the result.",
    summary_author: "tutor",
    hypothesis: null,
    learner_note: "",
    first_seen_at: "2026-08-19T10:00:00+00:00",
    last_touched_at: "2026-08-20T15:00:00+00:00",
  };

  it("accepts a journal entry with the hypothesis withheld", () => {
    // Data layer §11 keeps it from the learner; the field stays nullable rather
    // than absent, so the decision is one constant away from reversing (F16).
    expect(journalEntry.parse(entry).hypothesis).toBeNull();
  });

  it("accepts every journal_event_kind the database has", () => {
    // The six-value version of this enum would have failed the whole detail
    // view the first time the Tracker revised a hypothesis (F17).
    const kinds = [
      "created",
      "revisited",
      "partially_addressed",
      "resolved",
      "reopened",
      "archived",
      "hypothesis_updated",
      "learner_note_added",
    ];
    const parsed = journalEntryDetail.parse({
      ...entry,
      history: kinds.map((kind, index) => ({
        id: `6ba7b810-9dad-11d1-80b4-00c04fd430c${index}`,
        kind,
        at: "2026-08-19T10:00:00+00:00",
        session_id: null,
      })),
    });
    expect(parsed.history.map((event) => event.kind)).toEqual(kinds);
  });

  it("rejects an event kind the database cannot produce", () => {
    expect(
      journalEntryDetail.safeParse({
        ...entry,
        history: [
          {
            id: "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
            kind: "addressed",
            at: "2026-08-19T10:00:00+00:00",
            session_id: null,
          },
        ],
      }).success,
    ).toBe(false);
  });

  it("accepts the desk payload, decayed mastery included", () => {
    const parsed = deskResponse.parse({
      learner: {
        id: CONCEPT_ID,
        display_name: "Test Learner",
        timezone: "America/Toronto",
        default_session_minutes: 90,
        show_cost: false,
      },
      open_session: null,
      recent_sessions: [
        {
          id: CONCEPT_ID,
          mode: "lecture",
          started_at: "2026-08-20T14:00:00+00:00",
          ended_at: "2026-08-20T15:00:00+00:00",
          duration_minutes: 60,
          concepts_touched: ["Beta-reduction"],
        },
      ],
      syllabus_next: [{ id: CONCEPT_ID, name: "The Church-Rosser theorem" }],
      open_journal_entries: [entry],
      mastery: [
        {
          concept_id: CONCEPT_ID,
          concept_name: "Beta-reduction",
          p_known: 0.82,
          p_known_decayed: 0.74,
        },
      ],
    });
    // The decayed value is what the desk renders (F18) -- it is what the
    // Curator sequenced against.
    expect(parsed.mastery[0]?.p_known_decayed).toBe(0.74);
  });

  it("rejects a mastery row with no decayed value", () => {
    expect(
      deskResponse.safeParse({
        learner: { id: CONCEPT_ID, display_name: "x" },
        mastery: [{ concept_id: CONCEPT_ID, concept_name: "Beta-reduction", p_known: 0.8 }],
      }).success,
    ).toBe(false);
  });

  it("accepts the session summary with its mastery deltas", () => {
    const parsed = sessionSummary.parse({
      session_id: CONCEPT_ID,
      summary: "We worked through beta-reduction.",
      concepts_touched: [
        { concept_id: CONCEPT_ID, concept_name: "Beta-reduction", before: 0.65, after: 0.82 },
      ],
      open_threads: ["Confluence is still not settled."],
      next_focus_concept_id: CONCEPT_ID,
      next_focus_concept_name: "The Church-Rosser theorem",
      duration_minutes: 47.5,
      cost_usd: 0.42,
    });
    expect(parsed.concepts_touched[0]?.after).toBe(0.82);
  });

  it("accepts a summary whose cost is withheld", () => {
    const parsed = sessionSummary.parse({
      session_id: CONCEPT_ID,
      summary: "Short session.",
      next_focus_concept_id: null,
      next_focus_concept_name: null,
      duration_minutes: 12,
      cost_usd: null,
    });
    expect(parsed.cost_usd).toBeNull();
    expect(parsed.open_threads).toEqual([]);
  });
});

import { describe, expect, it } from "vitest";
import {
  artifactCitationsResponse,
  budgetExceededDetail,
  chunkKind,
  citation,
  closeResponse,
  degradedPayload,
  endPayload,
  interruptResponse,
  sessionStateResponse,
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

  it("parses the Lecturer's end payload", () => {
    const parsed = endPayload.parse({
      turn_id: SESSION_ID,
      segment_index: 4,
      anchor: "The redex is chosen leftmost-outermost.",
    });
    expect(parsed.segment_index).toBe(4);
    // Not emitted by the runtime today; parsed so the client picks it up the
    // moment it is. See DIVERGENCES F3.
    expect(parsed.artifact_id).toBeUndefined();
  });

  it("parses the primitive end payloads", () => {
    expect(endPayload.parse({ primitive: "explain_differently", stance: "intuitive" }).stance).toBe(
      "intuitive",
    );
    expect(
      endPayload.parse({ primitive: "let_me_try_one", next_state: "LAB", problem: { id: 1 } })
        .next_state,
    ).toBe("LAB");
    expect(endPayload.parse({ primitive: "im_lost", refocus_concept_id: null }).primitive).toBe(
      "im_lost",
    );
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

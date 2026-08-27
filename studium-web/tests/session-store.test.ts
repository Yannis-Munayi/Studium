import { beforeEach, describe, expect, it, vi } from "vitest";
import { canInterrupt, currentTurn, useSessionStore } from "@/lib/state/session";
import type { StreamChunk } from "@/lib/api/schemas";

/**
 * §17 Tier 1: "State management: Zustand stores behave correctly under sequence
 * of actions."
 *
 * The sequence that matters is a whole interrupted lecture, driven as function
 * calls: open, stream, interrupt, cut, question, answer, resume. Every one of
 * those transitions is a place §8.3 makes a promise, and none of them needs a
 * network or a DOM to check.
 */

const text = (value: string): StreamChunk => ({ kind: "text", payload: { text: value } });
const end = (payload: Record<string, unknown> = {}): StreamChunk => ({ kind: "end", payload });

const SESSION = "0f8fad5b-d9cb-469f-a165-70867728950e";

describe("useSessionStore", () => {
  beforeEach(() => {
    useSessionStore.getState().reset();
  });

  it("starts idle with nothing rendered", () => {
    const state = useSessionStore.getState();
    expect(state.phase).toBe("idle");
    expect(state.turns).toEqual([]);
    expect(state.interruptRequested).toBe(false);
  });

  it("accumulates text chunks into the open turn", () => {
    const store = useSessionStore.getState();
    store.open({ sessionId: SESSION, mode: "lecture" });
    store.beginTurn("lecturer");

    store.applyChunk(text("Beta reduction "));
    store.applyChunk(text("rewrites an application."));

    const turns = useSessionStore.getState().turns;
    expect(turns).toHaveLength(1);
    expect(turns[0]?.text).toBe("Beta reduction rewrites an application.");
    expect(turns[0]?.complete).toBe(false);
  });

  it("closes the turn and records the runtime state from the end chunk", () => {
    const store = useSessionStore.getState();
    store.open({ sessionId: SESSION, mode: "lecture" });
    store.beginTurn("lecturer");
    store.applyChunk(text("Done."));
    store.applyChunk(end({ next_state: "TUTORIAL", segment_index: 2 }));

    const state = useSessionStore.getState();
    expect(state.turns[0]?.complete).toBe(true);
    expect(state.turns[0]?.end?.segment_index).toBe(2);
    expect(state.runtimeState).toBe("TUTORIAL");
  });

  it("ignores an end chunk carrying a state the client does not know", () => {
    // A runtime that grows a state must not blank the UI on this client.
    const store = useSessionStore.getState();
    store.beginTurn("lecturer");
    store.applyChunk(end({ next_state: "TIME_TRAVEL" }));

    expect(useSessionStore.getState().runtimeState).toBeNull();
    expect(useSessionStore.getState().turns[0]?.complete).toBe(true);
  });

  it("surfaces a degraded chunk as learner-visible copy", () => {
    // The chunk kind spec §8.1 omits. Dropping it is the silent failure §3 bans.
    const store = useSessionStore.getState();
    store.beginTurn("lecturer");
    store.applyChunk({
      kind: "degraded",
      payload: { text: "There's high demand right now.", reason: "rate_limit" },
    });

    const degradation = useSessionStore.getState().degradation;
    expect(degradation?.text).toBe("There's high demand right now.");
    expect(degradation?.reason).toBe("rate_limit");
  });

  it("does not render tool_effect or trace chunks", () => {
    const store = useSessionStore.getState();
    store.beginTurn("lecturer");
    store.applyChunk(text("Visible."));
    store.applyChunk({ kind: "tool_effect", payload: { kind: "record_mastery_evidence" } });
    store.applyChunk({ kind: "trace", payload: { cost_usd: "0.01" } });

    expect(useSessionStore.getState().turns[0]?.text).toBe("Visible.");
  });

  it("adopts an orphaned text chunk rather than dropping the prose", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const store = useSessionStore.getState();
    store.applyChunk(text("No turn was opened."));

    expect(useSessionStore.getState().turns).toHaveLength(1);
    expect(useSessionStore.getState().turns[0]?.text).toBe("No turn was opened.");
    expect(warn).toHaveBeenCalled();
  });

  it("runs a full interrupt cycle in the order §8.3 specifies", () => {
    const store = useSessionStore.getState();
    store.open({ sessionId: SESSION, mode: "lecture" });

    // 1. A lecture is streaming.
    store.beginTurn("lecturer");
    store.setPhase("streaming");
    store.applyChunk(text("Beta reduction rewrites an application. "));

    // 2. The learner raises a hand: the button arms before the server answers.
    store.requestInterrupt();
    expect(useSessionStore.getState().interruptRequested).toBe(true);
    expect(useSessionStore.getState().phase).toBe("interrupting");

    // 3. The current sentence finishes and the stream stops.
    store.applyChunk(text("The redex is chosen leftmost-outermost."));
    store.applyChunk(end({}));

    // 4. The cut point is recorded, so the transcript can mark it.
    const cut = useSessionStore.getState().turns[0];
    expect(cut?.interruptedAt).toBe(cut?.text.length);

    // 5. Resolving parks the session waiting on the learner.
    store.resolveInterrupt();
    expect(useSessionStore.getState().phase).toBe("paused");
    expect(useSessionStore.getState().interruptRequested).toBe(false);
    expect(useSessionStore.getState().pausedAfterTurnId).toBe(cut?.id);

    // 6. Their question, then the Tutor's answer, are separate turns.
    store.beginTurn("learner");
    store.appendText("Why leftmost-outermost?");
    store.finishTurn();

    store.beginTurn("tutor");
    store.setPhase("streaming");
    store.applyChunk(text("Because it is normalising."));
    store.applyChunk(end({}));

    const turns = useSessionStore.getState().turns;
    expect(turns.map((t) => t.speaker)).toEqual(["lecturer", "learner", "tutor"]);
    // The Tutor's turn was not interrupted, so it carries no cut marker.
    expect(turns[2]?.interruptedAt).toBeNull();
  });

  it("clears the armed state when an interrupt is cancelled", () => {
    const store = useSessionStore.getState();
    store.beginTurn("lecturer");
    store.setPhase("streaming");
    store.requestInterrupt();
    store.cancelInterrupt();

    expect(useSessionStore.getState().interruptRequested).toBe(false);
    expect(useSessionStore.getState().pausedAfterTurnId).toBeNull();
  });

  it("counts reconnect attempts and clears them on a clean read", () => {
    const store = useSessionStore.getState();
    store.noteReconnect();
    store.noteReconnect();
    expect(useSessionStore.getState().reconnectAttempts).toBe(2);
    expect(useSessionStore.getState().phase).toBe("reconnecting");

    store.clearReconnects();
    expect(useSessionStore.getState().reconnectAttempts).toBe(0);
  });

  describe("the lab problem (§6.3, §9.3)", () => {
    const PROBLEM = {
      prompt: "Reduce (\\x. x x) (\\y. y).",
      difficulty: 2,
      hint: "Start with the outermost redex.",
    };

    it("takes the problem off the end chunk that moves the session to LAB", () => {
      const store = useSessionStore.getState();
      store.open({ sessionId: SESSION, mode: "lecture" });
      store.beginTurn("tutor");
      store.applyChunk(
        end({ primitive: "let_me_try_one", next_state: "LAB", problem: PROBLEM }),
      );

      const state = useSessionStore.getState();
      expect(state.runtimeState).toBe("LAB");
      expect(state.labProblem?.prompt).toBe(PROBLEM.prompt);
    });

    it("keeps the problem through a wrong answer that still has attempts", () => {
      // §7: LAB --answer_submitted[incorrect, attempts remain]--> LAB. Clearing
      // here would take the question away from a learner about to try again.
      const store = useSessionStore.getState();
      store.open({ sessionId: SESSION, mode: "lecture" });
      store.beginTurn("tutor");
      store.applyChunk(end({ next_state: "LAB", problem: PROBLEM }));

      store.beginTurn("evaluator");
      store.applyChunk(end({ verdict: "incorrect", next_state: "LAB" }));

      expect(useSessionStore.getState().labProblem?.prompt).toBe(PROBLEM.prompt);
    });

    it("drops the problem when the session leaves LAB", () => {
      const store = useSessionStore.getState();
      store.open({ sessionId: SESSION, mode: "lecture" });
      store.beginTurn("tutor");
      store.applyChunk(end({ next_state: "LAB", problem: PROBLEM }));

      store.beginTurn("evaluator");
      store.applyChunk(end({ verdict: "correct", next_state: "TUTORIAL" }));

      const state = useSessionStore.getState();
      expect(state.runtimeState).toBe("TUTORIAL");
      expect(state.labProblem).toBeNull();
    });

    it("drops the problem when reconciliation says the runtime is not in LAB", () => {
      // The reconcile against `GET /state` is authoritative (F9). Before the
      // runtime learned to transition out of LECTURING on this primitive (R13),
      // this is the step that revealed the disagreement -- and it must resolve
      // in the runtime's favour, not the chunk's.
      const store = useSessionStore.getState();
      store.open({ sessionId: SESSION, mode: "lecture" });
      store.beginTurn("tutor");
      store.applyChunk(end({ next_state: "LAB", problem: PROBLEM }));

      store.setRuntimeState("LECTURING");

      expect(useSessionStore.getState().labProblem).toBeNull();
    });

    it("keeps the problem when reconciliation agrees the runtime is in LAB", () => {
      const store = useSessionStore.getState();
      store.open({ sessionId: SESSION, mode: "lecture" });
      store.beginTurn("tutor");
      store.applyChunk(end({ next_state: "LAB", problem: PROBLEM }));

      store.setRuntimeState("LAB");

      expect(useSessionStore.getState().labProblem?.prompt).toBe(PROBLEM.prompt);
    });

    it("a problem the client cannot parse costs the bench, not the turn", () => {
      const store = useSessionStore.getState();
      store.open({ sessionId: SESSION, mode: "lecture" });
      store.beginTurn("tutor");
      store.applyChunk(text("Here is one to try."));
      store.applyChunk(end({ next_state: "LAB", problem: { unexpected: true } }));

      const state = useSessionStore.getState();
      expect(state.turns[0]?.complete).toBe(true);
      expect(state.turns[0]?.text).toBe("Here is one to try.");
      expect(state.labProblem).toBeNull();
    });
  });

  it("records the artifact id the runtime names on the end chunk", () => {
    // SD5: the field that makes §10's citation cards resolvable.
    const ARTIFACT = "7c9e6679-7425-40de-944b-e07fc1f90ae7";
    const store = useSessionStore.getState();
    store.open({ sessionId: SESSION, mode: "lecture" });
    store.beginTurn("lecturer");
    store.applyChunk(text("A redex is an application [P1]."));
    store.applyChunk(end({ turn_id: SESSION, artifact_id: ARTIFACT }));

    expect(useSessionStore.getState().turns[0]?.artifactId).toBe(ARTIFACT);
  });

  it("leaves the artifact id null when the turn produced no artifact", () => {
    // A Tutor turn, or a Lecturer turn whose effect batch rolled back. The
    // card says the source is not linked rather than spinning (§3).
    const store = useSessionStore.getState();
    store.open({ sessionId: SESSION, mode: "tutorial" });
    store.beginTurn("tutor");
    store.applyChunk(end({ turn_id: SESSION }));

    expect(useSessionStore.getState().turns[0]?.artifactId).toBeNull();
  });

  it("wipes everything on reset, so a second session starts clean", () => {
    const store = useSessionStore.getState();
    store.open({ sessionId: SESSION, mode: "lecture" });
    store.beginTurn("lecturer");
    store.applyChunk(text("Prior session."));
    store.reset();

    const state = useSessionStore.getState();
    expect(state.turns).toEqual([]);
    expect(state.sessionId).toBeNull();
    expect(state.phase).toBe("idle");
  });
});

describe("currentTurn", () => {
  it("returns the open turn and nothing once it is complete", () => {
    const open = { id: "a", complete: false } as never;
    const closed = { id: "a", complete: true } as never;
    expect(currentTurn([open])).toBe(open);
    expect(currentTurn([closed])).toBeNull();
    expect(currentTurn([])).toBeNull();
  });
});

describe("canInterrupt", () => {
  it("arms only while a stream is actually running", () => {
    expect(canInterrupt("streaming", "LECTURING")).toBe(true);
    expect(canInterrupt("idle", "LECTURING")).toBe(false);
    expect(canInterrupt("paused", "LECTURING")).toBe(false);
    expect(canInterrupt("complete", "LECTURING")).toBe(false);
  });

  it("stays disarmed while a session is opening or closing", () => {
    // §8.3: interruption "doesn't apply" in these states, and §8.4 makes close
    // explicitly uninterruptible.
    expect(canInterrupt("streaming", "CLOSING")).toBe(false);
    expect(canInterrupt("streaming", "OPENING")).toBe(false);
  });
});

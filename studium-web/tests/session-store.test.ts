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

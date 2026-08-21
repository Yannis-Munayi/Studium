/**
 * Session runtime state (spec §14.2, `useSessionStore`).
 *
 * Everything the classroom needs that is not server state: the stream buffer,
 * the interrupt state, the turn list, the degradation notice. §3's boundary
 * holds -- nothing here is written back, and nothing here mirrors a row.
 *
 * The chunk handlers are store actions rather than hook internals so §17's
 * Tier 1 case ("Zustand stores behave correctly under sequence of actions")
 * can drive a whole interrupted lecture as a list of function calls, with no
 * network, no timers, and no DOM.
 */
import { create } from "zustand";
import type { SessionMode, SessionState, StreamChunk } from "@/lib/api/schemas";
import { degradedPayload, endPayload, sessionState } from "@/lib/api/schemas";
import type { DegradationNotice, RenderedTurn, StreamPhase } from "@/lib/stream/types";

/** Which agent a turn's text came from, inferred from how it was requested. */
export type Speaker = RenderedTurn["speaker"];

/**
 * The state a session in each mode opens into.
 *
 * Mirrors `MODE_ENTRY_STATE` in `orchestration/state_machine.py`, including its
 * one non-obvious row: orientation has no runtime state of its own and opens as
 * a tutorial, "which is the shape an intake conversation actually takes".
 */
export const MODE_ENTRY_STATE: Record<SessionMode, SessionState> = {
  lecture: "LECTURING",
  tutorial: "TUTORIAL",
  lab: "LAB",
  review: "REVIEW",
  office_hours: "OFFICE_HOURS",
  summative_assessment: "SUMMATIVE_ASSESSMENT",
  orientation: "TUTORIAL",
};

interface SessionSlice {
  sessionId: string | null;
  mode: SessionMode | null;
  focusConceptId: string | null;
  runtimeState: SessionState | null;

  phase: StreamPhase;
  turns: RenderedTurn[];
  degradation: DegradationNotice | null;
  reconnectAttempts: number;

  /**
   * Set the moment the learner gestures, before the server has answered.
   *
   * Distinct from `phase === "interrupting"`: the phase follows the stream, and
   * this follows the *learner*. §8.3 step 1 wants the button to depress on the
   * gesture, not on the 202 -- a control that waits for a round trip to
   * acknowledge a press reads as a control that did not work.
   */
  interruptRequested: boolean;
  /** Where the last interrupt cut the lecture; drives §8.3 step 4's rule. */
  pausedAfterTurnId: string | null;

  // --- lifecycle ---
  open: (input: { sessionId: string; mode: SessionMode; focusConceptId?: string | null }) => void;
  reset: () => void;

  // --- stream ---
  beginTurn: (speaker: Speaker, id?: string) => string;
  appendText: (text: string) => void;
  applyChunk: (chunk: StreamChunk) => void;
  finishTurn: () => void;
  setPhase: (phase: StreamPhase) => void;
  setRuntimeState: (state: SessionState) => void;
  noteReconnect: () => void;
  clearReconnects: () => void;
  dismissDegradation: () => void;

  // --- interruption ---
  requestInterrupt: () => void;
  cancelInterrupt: () => void;
  resolveInterrupt: () => void;
}

const initial = {
  sessionId: null,
  mode: null,
  focusConceptId: null,
  runtimeState: null,
  phase: "idle" as StreamPhase,
  turns: [] as RenderedTurn[],
  degradation: null,
  reconnectAttempts: 0,
  interruptRequested: false,
  pausedAfterTurnId: null,
};

let turnCounter = 0;
function nextTurnId(): string {
  turnCounter += 1;
  return `turn-${turnCounter}`;
}

export const useSessionStore = create<SessionSlice>((set, get) => ({
  ...initial,

  open: ({ sessionId, mode, focusConceptId = null }) =>
    set({
      ...initial,
      sessionId,
      mode,
      focusConceptId,
      // Seeded from the mode rather than left null until the runtime says
      // otherwise. It never does for an ordinary lecture segment: the
      // Lecturer's `end` chunk carries `turn_id`, `segment_index` and `anchor`,
      // and only the Orchestrator's own transitions carry `next_state`. With a
      // null state, §9.2's contextual buttons have nothing to key off and never
      // appear at all. See DIVERGENCES-FRONTEND F9.
      runtimeState: MODE_ENTRY_STATE[mode] ?? null,
    }),

  reset: () => set({ ...initial }),

  beginTurn: (speaker, id) => {
    const turnId = id ?? nextTurnId();
    set((s) => ({
      turns: [
        ...s.turns,
        {
          id: turnId,
          speaker,
          text: "",
          complete: false,
          artifactId: null,
          interruptedAt: null,
          end: null,
        },
      ],
    }));
    return turnId;
  },

  appendText: (text) =>
    set((s) => {
      const turns = s.turns.slice();
      const last = turns[turns.length - 1];
      // A `text` chunk with no open turn is a runtime contract break, not a
      // reason to drop the learner's content -- adopt it into a lecturer turn
      // so the prose is on screen and the anomaly is in the console.
      if (!last || last.complete) {
        console.warn("[session] text chunk with no open turn; adopting");
        turns.push({
          id: nextTurnId(),
          speaker: "lecturer",
          text,
          complete: false,
          artifactId: null,
          interruptedAt: null,
          end: null,
        });
        return { turns };
      }
      turns[turns.length - 1] = { ...last, text: last.text + text };
      return { turns };
    }),

  applyChunk: (chunk) => {
    switch (chunk.kind) {
      case "text": {
        const text = chunk.payload["text"];
        if (typeof text === "string") get().appendText(text);
        return;
      }

      case "degraded": {
        // §3: never a silent failure. This is the chunk that carries the copy.
        const parsed = degradedPayload.safeParse(chunk.payload);
        if (parsed.success) {
          set({ degradation: { ...parsed.data, at: Date.now() } });
        }
        return;
      }

      case "end": {
        const parsed = endPayload.safeParse(chunk.payload);
        const end = parsed.success ? parsed.data : null;
        set((s) => {
          const turns = s.turns.slice();
          const index = turns.length - 1;
          const last = turns[index];
          if (last) {
            turns[index] = {
              ...last,
              complete: true,
              end,
              artifactId: end?.artifact_id ?? last.artifactId,
              interruptedAt: s.interruptRequested ? last.text.length : last.interruptedAt,
            };
          }
          const next = end?.next_state;
          const runtime = next ? sessionState.safeParse(next) : null;
          return {
            turns,
            ...(runtime?.success ? { runtimeState: runtime.data } : {}),
          };
        });
        return;
      }

      case "tool_effect":
        // §8.1: not displayed. Effects are applied server-side; the chunk is a
        // notification, and acting on it here would be a second writer.
        return;

      case "trace":
        if (process.env.NODE_ENV !== "production") {
          console.debug("[trace]", chunk.payload);
        }
        return;
    }
  },

  finishTurn: () =>
    set((s) => {
      const turns = s.turns.slice();
      const index = turns.length - 1;
      const last = turns[index];
      if (last && !last.complete) turns[index] = { ...last, complete: true };
      return { turns };
    }),

  setPhase: (phase) => set({ phase }),

  setRuntimeState: (runtimeState) => set({ runtimeState }),

  noteReconnect: () =>
    set((s) => ({ phase: "reconnecting", reconnectAttempts: s.reconnectAttempts + 1 })),

  clearReconnects: () => set({ reconnectAttempts: 0 }),

  dismissDegradation: () => set({ degradation: null }),

  requestInterrupt: () => set({ interruptRequested: true, phase: "interrupting" }),

  cancelInterrupt: () => set({ interruptRequested: false, pausedAfterTurnId: null }),

  resolveInterrupt: () =>
    set((s) => ({
      interruptRequested: false,
      phase: "paused",
      pausedAfterTurnId: s.turns[s.turns.length - 1]?.id ?? null,
    })),
}));

/** The turn currently accumulating text, if any. */
export function currentTurn(turns: RenderedTurn[]): RenderedTurn | null {
  const last = turns[turns.length - 1];
  return last && !last.complete ? last : null;
}

/**
 * Whether an interrupt is meaningful right now (§8.3's button states).
 *
 * `interruptible` on the server is the authority; this is the client's
 * prediction of it, used to disable the button rather than to decide anything.
 * The two agreeing is nice; the two disagreeing costs one no-op POST that the
 * runtime answers with `accepted: false`.
 */
export function canInterrupt(phase: StreamPhase, state: SessionState | null): boolean {
  if (phase !== "streaming") return false;
  return state !== "CLOSING" && state !== "OPENING";
}

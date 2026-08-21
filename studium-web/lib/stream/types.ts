/** Shared streaming types (spec §8). */
import type { DegradedPayload, EndPayload, SessionState } from "@/lib/api/schemas";

/** What the classroom shows for the turn currently in flight. */
export type StreamPhase =
  /** No stream open. */
  | "idle"
  /** POST sent, no bytes yet. §7.1's "Preparing your session…". */
  | "connecting"
  /** Bytes arriving. */
  | "streaming"
  /** Interrupt signalled; waiting for the sentence to close (§8.3 step 3). */
  | "interrupting"
  /** Sentence closed, message input focused (§8.3 steps 4-5). */
  | "paused"
  /** Stream ended cleanly. */
  | "complete"
  /** Connection dropped; reconnect in progress (§15.3). */
  | "reconnecting"
  /** Reconnection gave up. */
  | "failed";

/** One completed or in-flight turn in the reading column. */
export interface RenderedTurn {
  id: string;
  /** Which voice produced it -- drives §6.2's distinct type treatment. */
  speaker: "lecturer" | "tutor" | "learner" | "curator" | "evaluator";
  /** Accumulated text so far. */
  text: string;
  /** True once the `end` chunk for this turn has arrived. */
  complete: boolean;
  /** From the `end` payload, when the runtime supplies it (see F3). */
  artifactId: string | null;
  /** Where an interrupt cut this turn, if it was cut (§8.3 step 4). */
  interruptedAt: number | null;
  end: EndPayload | null;
}

/** A `degraded` chunk, surfaced as learner-visible copy (§15). */
export interface DegradationNotice extends DegradedPayload {
  at: number;
}

export interface StreamSnapshot {
  phase: StreamPhase;
  turns: RenderedTurn[];
  degradation: DegradationNotice | null;
  /** Runtime state as of the last `end` chunk that carried one. */
  state: SessionState | null;
  /** Failed reconnect attempts since the last clean read (§15.3). */
  reconnectAttempts: number;
}

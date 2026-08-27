"use client";

/**
 * The stream consumer (spec §8.1, §8.3).
 *
 * One hook owns the whole streaming interaction: opening a turn, batching its
 * deltas, signalling an interrupt, resuming, reconnecting, and announcing each
 * of those to a screen reader. Splitting it further sounds tidier and is not --
 * every one of those steps reads or writes the same "is a stream open right
 * now" fact, and two owners of that fact is the bug §8 spends its length
 * preventing.
 */
import { useCallback, useEffect, useRef } from "react";
import { fetchSessionState, signalInterrupt } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";
import { announce, STREAM_ANNOUNCEMENTS } from "@/lib/a11y/announcer";
import { useSessionStore, type Speaker } from "@/lib/state/session";
import { DeltaBuffer } from "./buffer";
import { readTurnStream, StreamInterruptedError, type TurnRequest } from "./transport";

/** §15.3: "If reconnection fails after three attempts". */
export const MAX_RECONNECT_ATTEMPTS = 3;

/** Exponential backoff between reconnects, capped so it stays a UI event. */
function backoffMs(attempt: number): number {
  return Math.min(1000 * 2 ** attempt, 8000);
}

export interface SendOptions extends TurnRequest {
  /** Which voice the reply should be attributed to in the reading column. */
  speaker?: Speaker;
  /** Suppress the "Tutor is answering" announcement for a lecture resume. */
  announceAs?: string | null;
}

export function useSessionStream(sessionId: string | null) {
  const store = useSessionStore;
  const abortRef = useRef<AbortController | null>(null);
  const bufferRef = useRef<DeltaBuffer | null>(null);
  /** The request in flight, kept so a reconnect can replay it (see F6). */
  const lastSendRef = useRef<SendOptions | null>(null);

  const teardown = useCallback(() => {
    bufferRef.current?.dispose();
    bufferRef.current = null;
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  useEffect(() => teardown, [teardown]);

  /** Run one turn to completion. Returns false if it ended in a drop. */
  const runTurn = useCallback(
    async (options: SendOptions): Promise<boolean> => {
      if (!sessionId) return false;
      const state = store.getState();

      const controller = new AbortController();
      abortRef.current = controller;

      const buffer = new DeltaBuffer((text) => store.getState().appendText(text));
      bufferRef.current = buffer;

      state.beginTurn(options.speaker ?? "lecturer");
      state.setPhase("connecting");

      try {
        for await (const chunk of readTurnStream(
          sessionId,
          {
            text: options.text ?? "",
            primitive: options.primitive ?? null,
            intent: options.intent ?? null,
          },
          { signal: controller.signal },
        )) {
          if (store.getState().phase === "connecting") {
            store.getState().setPhase("streaming");
            store.getState().clearReconnects();
          }

          if (chunk.kind === "text") {
            const text = chunk.payload["text"];
            if (typeof text === "string") buffer.push(text);
            continue;
          }

          // Anything that is not text is a control chunk. Drain first so the
          // prose that preceded it is on screen before its consequence is --
          // otherwise a `degraded` notice can render above the sentence it
          // interrupted.
          buffer.drain();
          store.getState().applyChunk(chunk);

          if (chunk.kind === "degraded") {
            announce(String(chunk.payload["text"] ?? ""), "assertive");
          }
        }

        buffer.drain();
        store.getState().finishTurn();
        void reconcileState(sessionId);

        // The interrupt landed and the sentence has closed: §8.3 step 4.
        if (store.getState().interruptRequested) {
          store.getState().resolveInterrupt();
          announce(STREAM_ANNOUNCEMENTS.readyForQuestion, "assertive");
        } else {
          store.getState().setPhase("complete");
          announce(STREAM_ANNOUNCEMENTS.streamComplete, "polite");
        }
        return true;
      } catch (error) {
        buffer.drain();
        store.getState().finishTurn();

        if (error instanceof StreamInterruptedError) return false;

        if (error instanceof ApiError) {
          store.getState().setPhase("failed");
          // A budget stop is not a stream failure and must not be retried into.
          announce(error.message, "assertive");
          throw error;
        }
        store.getState().setPhase("failed");
        throw error;
      } finally {
        buffer.dispose();
        if (abortRef.current === controller) abortRef.current = null;
      }
    },
    [sessionId, store],
  );

  /**
   * Send a turn, reconnecting on a mid-stream drop (§15.3).
   *
   * **The reconnect replays the turn; it does not resume it.** §8.1 asks for a
   * resume request carrying the last `turn_index`, and the runtime has no
   * endpoint that accepts one. Replaying is the honest fallback and it is not
   * free -- the learner may see the opening of a segment twice -- so the
   * classroom marks the retry boundary rather than pretending it was seamless.
   * See DIVERGENCES-FRONTEND.md F6.
   */
  const send = useCallback(
    async (options: SendOptions): Promise<void> => {
      lastSendRef.current = options;

      for (let attempt = 0; attempt <= MAX_RECONNECT_ATTEMPTS; attempt += 1) {
        const clean = await runTurn(options);
        if (clean) return;

        if (attempt === MAX_RECONNECT_ATTEMPTS) {
          store.getState().setPhase("failed");
          return;
        }
        store.getState().noteReconnect();
        announce(STREAM_ANNOUNCEMENTS.reconnecting, "assertive");
        await sleep(backoffMs(attempt));
      }
    },
    [runTurn, store],
  );

  /** §8.3 steps 1-2. Optimistic: the button depresses before the POST lands. */
  const interrupt = useCallback(async (): Promise<void> => {
    if (!sessionId) return;
    store.getState().requestInterrupt();
    announce(STREAM_ANNOUNCEMENTS.requestingInterrupt, "assertive");

    try {
      const result = await signalInterrupt(sessionId);
      if (!result.accepted) {
        // Nothing was interruptible. Not an error the learner needs told about
        // in an alert -- the stream was already over -- but the armed button
        // must un-arm, or it stays depressed with nothing to release it.
        store.getState().cancelInterrupt();
        store.getState().setPhase(store.getState().turns.length ? "complete" : "idle");
      }
    } catch {
      store.getState().cancelInterrupt();
      store.getState().setPhase("streaming");
      announce("Could not reach the tutor to interrupt.", "assertive");
    }
  }, [sessionId, store]);

  /** §8.3 "Cancellation": Escape before typing resumes the lecture. */
  const cancelInterrupt = useCallback(() => {
    store.getState().cancelInterrupt();
    store.getState().setPhase("complete");
    announce(STREAM_ANNOUNCEMENTS.resumingLecture, "assertive");
  }, [store]);

  /** §15.3's manual "Try again". */
  const retry = useCallback(async (): Promise<void> => {
    const last = lastSendRef.current;
    if (!last) return;
    store.getState().clearReconnects();
    await send(last);
  }, [send, store]);

  const stop = useCallback(() => {
    teardown();
    store.getState().setPhase("complete");
  }, [store, teardown]);

  return { send, interrupt, cancelInterrupt, retry, stop };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Ask the runtime what state the session is actually in, after each turn.
 *
 * The store seeds a state from the session mode and updates it whenever an
 * `end` chunk carries `next_state`. Neither is authoritative: the seed is a
 * guess, and most `end` chunks carry no state at all (F9). `GET /state` is the
 * only thing that knows, and it is cheap -- the Orchestrator is in memory, so
 * this is a dictionary lookup, not a query.
 *
 * Failures are swallowed on purpose. A stale contextual toolbar is a small
 * cosmetic wrong; an error banner over a lecture that streamed perfectly well,
 * because a diagnostic call failed, is a larger one.
 */
async function reconcileState(sessionId: string): Promise<void> {
  try {
    const { state } = await fetchSessionState(sessionId);
    useSessionStore.getState().setRuntimeState(state);
  } catch {
    // Intentionally silent; see above.
  }
}

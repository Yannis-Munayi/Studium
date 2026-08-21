/**
 * Degradation copy (spec §15.4, mirroring agent runtime §21).
 *
 * The backend already owns learner-facing copy for every failure it can
 * classify (`studium/copy/degradation.py`), and it sends that copy down the
 * wire inside `degraded` chunks and the 402 body. **Where the backend supplies
 * a message, the frontend renders it verbatim.** Writing a second sentence for
 * the same failure in a different register is how one product acquires two
 * voices, and the learner reads whichever one the failure path happened to take.
 *
 * What lives here is the copy for failures that never reach the backend at all:
 * the network dropped, the route threw, the schema did not match. Those have no
 * server-side counterpart because the server was not involved.
 */
import type { FailureKind } from "@/lib/api/errors";

export interface DegradationCopy {
  /** One line naming what happened. */
  title: string;
  /** One line naming what to do next. §3: never a spinner without explanation. */
  body: string;
  /** Label for the recovery action, or null when there is nothing to retry. */
  action: string | null;
}

/** Failures the client sees on its own. */
export const CLIENT_FAILURES: Record<FailureKind, DegradationCopy> = {
  network: {
    title: "Can't reach the tutor",
    body: "Your progress is saved. This is usually the connection rather than anything you did.",
    action: "Try again",
  },
  server_error: {
    title: "The server is having trouble right now",
    body: "Nothing is lost. Give it a moment and try again.",
    action: "Try again",
  },
  rate_limited: {
    title: "The system is under high load",
    body: "Please try again in a moment.",
    action: "Try again",
  },
  not_found: {
    title: "Not found",
    body: "That page or session doesn't exist. It may have been closed.",
    action: null,
  },
  forbidden: {
    title: "Access denied",
    body: "You don't have access to that. If this looks wrong, sign in again.",
    action: null,
  },
  invalid_request: {
    title: "Session not started",
    body: "Something about that request didn't line up. Returning to the desk usually clears it.",
    action: null,
  },
  malformed_response: {
    title: "The tutor's reply came back malformed",
    body: "It's been logged. Ask that again and it should come through cleanly.",
    action: "Try again",
  },
  // Never rendered from this table -- the 402 body carries the copy, with the
  // reset time already localised. Present so the record is total, and so a
  // missing case is a type error rather than a blank panel.
  budget_exceeded: {
    title: "You've reached your usage limit",
    body: "Your progress is saved. Come back when the limit resets.",
    action: null,
  },
};

/** §15.3's two-stage SSE drop copy. */
export const CONNECTION = {
  reconnecting: {
    title: "Lost connection to the tutor",
    body: "Trying to reconnect…",
    action: "Try again",
  },
  lost: {
    title: "We can't reach the tutor right now",
    body: "Your progress is saved. Try again in a few minutes.",
    action: "Try again",
  },
} satisfies Record<string, DegradationCopy>;

/** §15.1's route-level error boundary. */
export const BOUNDARY: DegradationCopy = {
  title: "Something went wrong loading this page",
  body: "It's been logged. You can try again, or head back to the desk.",
  action: "Try again",
};

export function copyFor(kind: FailureKind): DegradationCopy {
  return CLIENT_FAILURES[kind];
}

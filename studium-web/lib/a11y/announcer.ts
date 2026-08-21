/**
 * The single announcement region (spec §13.4, WCAG 4.1.3).
 *
 * §13.4 is explicit: one `aria-live` region at the top of the app, and no
 * component-level ones. Component-level regions are how two announcements
 * arrive at once and a screen reader drops one -- usually the important one,
 * because the chatty one wins by arriving more often.
 *
 * That decision has a consequence this module has to handle: a single region
 * cannot be `polite` and `assertive` at the same time. Two regions exist (one
 * of each politeness, both rendered once by `<Announcements />`) and this
 * routes to them. It is still "one region per politeness", which is what the
 * rule is protecting.
 */
import { create } from "zustand";

export type Politeness = "polite" | "assertive";

interface AnnouncementState {
  polite: string;
  assertive: string;
  /** Bumped on every announce so identical consecutive messages re-fire. */
  nonce: number;
  announce: (message: string, politeness?: Politeness) => void;
  clear: () => void;
}

export const useAnnouncements = create<AnnouncementState>((set) => ({
  polite: "",
  assertive: "",
  nonce: 0,
  announce: (message, politeness = "polite") =>
    set((prior) => ({
      ...(politeness === "assertive" ? { assertive: message } : { polite: message }),
      nonce: prior.nonce + 1,
    })),
  clear: () => set({ polite: "", assertive: "" }),
}));

/** Announce from outside React (the stream hook's chunk handler). */
export function announce(message: string, politeness: Politeness = "polite"): void {
  useAnnouncements.getState().announce(message, politeness);
}

/**
 * The exact strings §8.3 specifies, in one place.
 *
 * Copy that a sighted learner never sees is copy that drifts, because nobody
 * reviews it. Naming them here puts them in the same review surface as the rest
 * of `lib/copy/`.
 */
export const STREAM_ANNOUNCEMENTS = {
  requestingInterrupt: "Requesting interrupt",
  readyForQuestion: "Ready for your question",
  tutorAnswering: "Tutor is answering",
  resumingLecture: "Resuming lecture",
  lectureContinuing: "Lecture continuing",
  streamComplete: "Response complete",
  reconnecting: "Connection lost. Reconnecting.",
  reconnected: "Reconnected.",
} as const;

/**
 * Surface copy (spec §16.5).
 *
 * All of it in one file so tone is reviewable as tone. §16.5's test is whether
 * a line treats the learner as an adult doing serious work; that is a judgement
 * you make by reading the lines next to each other, not by finding them one at
 * a time across forty components.
 */

export const DESK = {
  heading: "The desk",
  continueSession: "Continue",
  startNext: "Start next session",
  beginStudying: "Begin studying",
  firstTime: "Nothing here yet. Pick a subject and we'll start at the beginning.",
  recentSessions: "Recent sessions",
  noRecentSessions: "No sessions yet.",
  syllabusNext: "Coming up",
  openEntries: "Open in your journal",
  noOpenEntries: "Nothing open. That's a good place to be.",
  mastery: "Where you stand",
  reviewQueue: "Due for review",
  reviewDeferred: "Review scheduling isn't running yet.",
} as const;

export const SESSION_START = {
  heading: "Start a session",
  mode: "What kind of session?",
  duration: "How long do you have?",
  focus: "What are we working on?",
  submit: "Begin",
  preparing: "Preparing your session…",
  // §7.1: on slow starts, say why rather than spinning silently.
  slowStart: "Opening a session assembles your prior context. This can take a moment.",
  modes: {
    lecture: "Lecture — I'll teach, you can interrupt any time",
    tutorial: "Tutorial — back and forth, mostly your questions",
    lab: "Lab — practice problems with feedback",
    review: "Review — spaced repetition (not available yet)",
  },
} as const;

export const CLASSROOM = {
  interrupt: "Interrupt",
  interruptHint: "Raise your hand — the current sentence finishes first",
  askQuestion: "Your question…",
  messagePlaceholder: "Type to ask something…",
  send: "Send",
  continueLecture: "Continue lecture",
  askAnother: "Ask another question",
  newContent: "New content below",
  pausedHere: "Paused here",
  resumed: "Picking up where we left off",
  stillGenerating: "still writing",
  escalate: "This has become a longer conversation. Move to office hours?",
  escalateAccept: "Move to office hours",
  escalateDismiss: "Keep going as we are",
  closeSession: "Close session",
  confirmClose: "Close this session? Your progress is saved and a summary will be generated.",
  confirmCloseAction: "Close",
  cancel: "Cancel",
  wrappingUp: "Wrapping up your session…",
  retrievalCheck: "A quick check on last time before we continue",
} as const;

export const CHECK = {
  heading: "Quick check",
  answerPlaceholder: "Your answer…",
  submit: "Submit answer",
  checking: "Checking…",
  correct: "Nice.",
  partial: "Close.",
  incorrect: "Not quite.",
  modelAnswer: "Here's what a full answer looks like:",
  showHint: "Show hint",
  showAnotherHint: "Show another hint",
} as const;

export const BENCH = {
  heading: "The bench",
  problem: "Problem",
  workspace: "Your work",
  feedback: "Feedback",
  submit: "Submit",
  submitting: "Checking…",
  awaitingSubmission: "Feedback appears here once you submit.",
  expectedTime: "Expected time",
} as const;

export const JOURNAL = {
  heading: "Confusion journal",
  filters: "Filters",
  status: "Status",
  subject: "Subject",
  concept: "Concept",
  dateRange: "Date range",
  empty: "Nothing matches these filters.",
  search: "Search entries",
  summary: "Summary",
  writtenByYou: "written by you",
  writtenByTutor: "written by the tutor",
  hypothesis: "What the system thinks is going on",
  // §12.1: the tooltip that keeps an inference from reading as a verdict.
  hypothesisTooltip:
    "This is the system's inference from how the conversation went, not something you said. It can be wrong.",
  learnerNote: "Your notes",
  learnerNotePlaceholder: "Anything you want to remember about this…",
  saved: "Saved",
  history: "History",
  markResolved: "Mark resolved",
  confirmResolve: "Mark this resolved? You can reopen it later if the confusion returns.",
  markPartial: "Mark partial",
  reopen: "Reopen",
  archive: "Archive",
  resolveFailed: "Could not mark resolved. Please try again.",
  statusFailed: "Could not update this entry. Please try again.",
  notFound: "That entry isn't here. It may have been archived or deleted.",
  loadFailed: "Couldn't load your journal just now.",
} as const;

export const SESSION_CLOSE = {
  heading: "Session complete",
  conceptsTouched: "What we covered",
  openThreads: "Carried forward",
  whatsNext: "Next",
  startNext: "Start next session",
  done: "Done",
  duration: "Duration",
  cost: "Cost",
} as const;

export const HELP = {
  heading: "Keyboard shortcuts",
  // §13.2: global shortcuts are documented in a `?` overlay reachable anywhere.
  shortcuts: [
    { keys: "?", action: "Open this help" },
    { keys: "/", action: "Open the command palette" },
    { keys: "Ctrl/Cmd + K", action: "Open the command palette" },
    { keys: "Space", action: "Interrupt the tutor (outside a text field)" },
    { keys: "Escape", action: "Cancel an interrupt, or close what's open" },
    { keys: "Enter", action: "Send your message" },
  ],
} as const;

export const PALETTE = {
  placeholder: "Search primitives…",
  empty: "No primitive matches that.",
  /** Announced as the learner types (§9.1). */
  countLabel: (n: number) => `${n} ${n === 1 ? "option" : "options"} available`,
} as const;

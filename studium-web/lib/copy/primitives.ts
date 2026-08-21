/**
 * The eight tutorial primitives, as the learner meets them (spec §9).
 *
 * `name` is the wire value `PRIMITIVE_NAMES` in `agents/schemas.py` accepts;
 * everything else is presentation. The two are in one table because the failure
 * mode otherwise is a button that reads correctly and posts a name the runtime
 * rejects with a 422 -- which looks to the learner like the tutor refusing to
 * answer.
 */
import type { SessionState } from "@/lib/api/schemas";

export interface Primitive {
  /** Wire name. Must match `PRIMITIVE_NAMES` exactly. */
  name: string;
  /** Command palette label (§9.1). */
  label: string;
  /** One line under the label; also the fuzzy-match corpus. */
  description: string;
  /** §9.1's single-letter shortcut, shown at the right of the palette row. */
  shortcut: string;
  /**
   * Whether invoking this stops an in-flight stream (§7.2).
   *
   * §7.2 gives two examples and no rule: `why_does_this_matter` does not stop
   * the flow, `explain_differently` does. The rule behind them is whether the
   * primitive is *about* the current exposition. Asking for the same idea
   * differently is a statement that the current one is not working, so
   * continuing it wastes tokens and the learner's attention. Asking why it
   * matters is a question alongside it.
   */
  interruptsStream: boolean;
  /**
   * Whether the runtime asks the learner something before producing output
   * (§9.3). Drives whether the palette hands focus to the message input.
   */
  promptsFirst: boolean;
}

export const PRIMITIVES: readonly Primitive[] = [
  {
    name: "explain_differently",
    label: "Explain differently",
    description: "Ask for the same idea in a different style",
    shortcut: "e",
    interruptsStream: true,
    promptsFirst: true,
  },
  {
    name: "prove_it_to_me",
    label: "Prove it to me",
    description: "See the derivation or justification",
    shortcut: "p",
    interruptsStream: true,
    promptsFirst: true,
  },
  {
    name: "where_does_this_fit",
    label: "Where does this fit",
    description: "See this concept's place in the graph",
    shortcut: "w",
    interruptsStream: false,
    promptsFirst: false,
  },
  {
    name: "vocabulary_check",
    label: "Check a term",
    description: "Ask whether it's the word or the idea that's unclear",
    shortcut: "v",
    interruptsStream: false,
    promptsFirst: true,
  },
  {
    name: "show_worked_example",
    label: "Show worked example",
    description: "Work one all the way through, step by step",
    shortcut: "x",
    interruptsStream: true,
    promptsFirst: false,
  },
  {
    name: "let_me_try_one",
    label: "Let me try one",
    description: "Move to the bench with a problem at your level",
    shortcut: "t",
    interruptsStream: true,
    promptsFirst: false,
  },
  {
    name: "why_does_this_matter",
    label: "Why does this matter",
    description: "See what this concept lets you do later",
    shortcut: "y",
    interruptsStream: false,
    promptsFirst: false,
  },
  {
    name: "im_lost",
    label: "I'm lost",
    description: "Back up to the last thing that made sense",
    shortcut: "l",
    interruptsStream: true,
    promptsFirst: true,
  },
] as const;

export const PRIMITIVE_BY_NAME: ReadonlyMap<string, Primitive> = new Map(
  PRIMITIVES.map((p) => [p.name, p]),
);

/**
 * §9.2's contextual buttons, keyed by runtime state.
 *
 * The states not in this table (OPENING, CLOSING, INTERRUPTED, IDLE) get no
 * buttons, which is the same answer §9.2 gives PAUSED_FOR_QUESTION and for the
 * same reason: there is nothing a primitive would usefully do to a session that
 * is starting, ending, or waiting on the learner.
 */
export const CONTEXTUAL_BUTTONS: Partial<Record<SessionState, readonly string[]>> = {
  LECTURING: ["explain_differently", "show_worked_example", "im_lost"],
  TUTORIAL: ["prove_it_to_me", "where_does_this_fit", "im_lost"],
  LAB: ["show_worked_example", "im_lost"],
  REVIEW: ["explain_differently", "im_lost"],
  OFFICE_HOURS: ["prove_it_to_me", "where_does_this_fit", "im_lost"],
  PAUSED_FOR_QUESTION: [],
};

export function buttonsFor(state: SessionState | null): Primitive[] {
  if (!state) return [];
  const names = CONTEXTUAL_BUTTONS[state] ?? [];
  return names.flatMap((n) => {
    const primitive = PRIMITIVE_BY_NAME.get(n);
    return primitive ? [primitive] : [];
  });
}

/**
 * §9.3's opening question for the primitives that ask one.
 *
 * Shown as the input's placeholder while the runtime's own version streams in,
 * so the learner is not typing into an unlabelled box for the second and a half
 * it takes the Tutor's phrasing to arrive.
 */
export const PROMPTS: Record<string, string> = {
  explain_differently: "What about the current explanation isn't working?",
  prove_it_to_me: "What would you expect the argument to look like?",
  vocabulary_check: "Which term is giving you trouble?",
  im_lost: "What's the last thing that made sense?",
};

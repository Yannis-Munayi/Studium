import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as Tooltip from "@radix-ui/react-tooltip";
import { renderWithProviders } from "./helpers";
import { InterruptButton, isTypingContext } from "@/components/session/interrupt-button";
import { MessageInput } from "@/components/session/message-input";
import { CommandPalette } from "@/components/session/command-palette";
import { ComprehensionCheck, MAX_ATTEMPTS } from "@/components/session/comprehension-check";
import { shouldOfferEscalation } from "@/components/session/resumption-card";
import { Disclosure } from "@/components/ui/disclosure";
import { useSessionStore } from "@/lib/state/session";
import { useCommandPaletteStore, filterPrimitives } from "@/lib/state/palette";
import { PRIMITIVES, buttonsFor, PRIMITIVE_BY_NAME } from "@/lib/copy/primitives";
import { useAnnouncements, STREAM_ANNOUNCEMENTS } from "@/lib/a11y/announcer";

/**
 * The interaction rules §8.3 and §9 specify.
 *
 * These are the behaviours the spec spends its length on -- keyboard conflicts,
 * cancellation, and what happens to a half-typed question. Each one below is a
 * sentence from the spec that could be implemented backwards without anything
 * looking broken.
 */

const noop = () => {};

beforeEach(() => {
  useSessionStore.getState().reset();
  useCommandPaletteStore.getState().reset();
  useAnnouncements.getState().clear();
});

/** Put the store into a live, interruptible stream. */
function startStreaming() {
  const store = useSessionStore.getState();
  store.open({ sessionId: "0f8fad5b-d9cb-469f-a165-70867728950e", mode: "lecture" });
  store.beginTurn("lecturer");
  store.setPhase("streaming");
  store.applyChunk({ kind: "end", payload: { next_state: "LECTURING" } });
  store.finishTurn();
  store.beginTurn("lecturer");
  store.setPhase("streaming");
}

describe("the Space shortcut (§8.3)", () => {
  it("interrupts when focus is on the document body", async () => {
    startStreaming();
    const onInterrupt = vi.fn();
    renderWithProviders(
      <Tooltip.Provider>
        <InterruptButton onInterrupt={onInterrupt} />
      </Tooltip.Provider>,
    );

    await userEvent.keyboard(" ");
    expect(onInterrupt).toHaveBeenCalledOnce();
  });

  it("does not interrupt while typing in a text field", async () => {
    // "inside an input, Space types a space" -- a learner writing a question
    // must be able to put spaces in it.
    startStreaming();
    const onInterrupt = vi.fn();
    renderWithProviders(
      <Tooltip.Provider>
        <InterruptButton onInterrupt={onInterrupt} />
        <textarea aria-label="question" />
      </Tooltip.Provider>,
    );

    const field = screen.getByLabelText("question");
    await userEvent.click(field);
    await userEvent.keyboard("a b");

    expect(onInterrupt).not.toHaveBeenCalled();
    expect(field).toHaveValue("a b");
  });

  it("does not steal Space from other controls", () => {
    // Space activates buttons and checkboxes. Intercepting it globally would
    // break every other control on the surface to make one convenient.
    const button = document.createElement("button");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    const editable = document.createElement("div");
    // The attribute, not the property: `isContentEditable` is computed from the
    // rendered tree and reads `false` on a node that is not in the document.
    editable.setAttribute("contenteditable", "true");
    const disabledEditable = document.createElement("div");
    disabledEditable.setAttribute("contenteditable", "false");

    expect(isTypingContext(button)).toBe(true);
    expect(isTypingContext(checkbox)).toBe(true);
    expect(isTypingContext(editable)).toBe(true);
    expect(isTypingContext(disabledEditable)).toBe(false);
    expect(isTypingContext(document.createElement("p"))).toBe(false);
    expect(isTypingContext(null)).toBe(false);
  });

  it("is inert when nothing is streaming", async () => {
    const onInterrupt = vi.fn();
    renderWithProviders(
      <Tooltip.Provider>
        <InterruptButton onInterrupt={onInterrupt} />
      </Tooltip.Provider>,
    );

    await userEvent.keyboard(" ");
    expect(onInterrupt).not.toHaveBeenCalled();
  });

  it("arms the button visually only while a stream is interruptible", () => {
    const { rerender } = renderWithProviders(
      <Tooltip.Provider>
        <InterruptButton onInterrupt={noop} />
      </Tooltip.Provider>,
    );
    expect(screen.getByRole("button")).not.toHaveAttribute("data-armed");

    // The store mutation drives a subscribed component, so it is a React
    // update and has to be inside act like any other.
    act(() => startStreaming());
    rerender(
      <Tooltip.Provider>
        <InterruptButton onInterrupt={noop} />
      </Tooltip.Provider>,
    );
    expect(screen.getByRole("button", { name: "Interrupt" })).toHaveAttribute("data-armed");
  });
});

describe("the message input (§8.3)", () => {
  it("treats the first keystroke during a stream as an interrupt", async () => {
    startStreaming();
    const onTypingInterrupt = vi.fn();
    renderWithProviders(
      <MessageInput onSend={noop} onTypingInterrupt={onTypingInterrupt} onCancelInterrupt={noop} />,
    );

    await userEvent.type(screen.getByLabelText(/type to ask something/i), "why");
    // Once, on the transition into non-empty -- not once per keystroke.
    expect(onTypingInterrupt).toHaveBeenCalledOnce();
  });

  it("cancels the interrupt on Escape before anything is typed", async () => {
    startStreaming();
    useSessionStore.getState().requestInterrupt();
    const onCancelInterrupt = vi.fn();
    renderWithProviders(
      <MessageInput onSend={noop} onTypingInterrupt={noop} onCancelInterrupt={onCancelInterrupt} />,
    );

    await userEvent.click(screen.getByLabelText(/type to ask something/i));
    await userEvent.keyboard("{Escape}");
    expect(onCancelInterrupt).toHaveBeenCalledOnce();
  });

  it("clears the box on Escape when a question is half-written, and keeps the interrupt", async () => {
    // Treating a half-written question as consent to discard the interrupt
    // would throw away the learner's words along with it.
    startStreaming();
    useSessionStore.getState().requestInterrupt();
    const onCancelInterrupt = vi.fn();
    renderWithProviders(
      <MessageInput onSend={noop} onTypingInterrupt={noop} onCancelInterrupt={onCancelInterrupt} />,
    );

    const field = screen.getByLabelText(/type to ask something/i);
    await userEvent.type(field, "why leftmost");
    await userEvent.keyboard("{Escape}");

    expect(field).toHaveValue("");
    expect(onCancelInterrupt).not.toHaveBeenCalled();
  });

  it("sends on Enter and newlines on Shift+Enter", async () => {
    const onSend = vi.fn();
    renderWithProviders(
      <MessageInput onSend={onSend} onTypingInterrupt={noop} onCancelInterrupt={noop} />,
    );

    const field = screen.getByLabelText(/type to ask something/i);
    await userEvent.type(field, "first line{Shift>}{Enter}{/Shift}second line");
    expect(onSend).not.toHaveBeenCalled();

    await userEvent.keyboard("{Enter}");
    expect(onSend).toHaveBeenCalledWith("first line\nsecond line");
  });

  it("refuses to send whitespace", async () => {
    const onSend = vi.fn();
    renderWithProviders(
      <MessageInput onSend={onSend} onTypingInterrupt={noop} onCancelInterrupt={noop} />,
    );

    await userEvent.type(screen.getByLabelText(/type to ask something/i), "   {Enter}");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("takes focus and relabels itself when the lecture pauses (§8.3 step 5)", async () => {
    startStreaming();
    useSessionStore.getState().requestInterrupt();
    useSessionStore.getState().resolveInterrupt();

    renderWithProviders(
      <MessageInput onSend={noop} onTypingInterrupt={noop} onCancelInterrupt={noop} />,
    );

    await waitFor(() => {
      expect(screen.getByLabelText(/your question/i)).toHaveFocus();
    });
  });
});

describe("the command palette (§9.1)", () => {
  it("opens on / and on Ctrl+K", async () => {
    renderWithProviders(<CommandPalette onInvoke={noop} />);

    await userEvent.keyboard("/");
    expect(useCommandPaletteStore.getState().open).toBe(true);

    await act(async () => {
      useCommandPaletteStore.getState().setOpen(false);
    });
    await userEvent.keyboard("{Control>}k{/Control}");
    expect(useCommandPaletteStore.getState().open).toBe(true);
  });

  it("does not open on / typed into a text field", async () => {
    // A learner writing "and/or" must get a slash.
    renderWithProviders(
      <>
        <CommandPalette onInvoke={noop} />
        <textarea aria-label="question" />
      </>,
    );

    await userEvent.click(screen.getByLabelText("question"));
    await userEvent.keyboard("and/or");

    expect(useCommandPaletteStore.getState().open).toBe(false);
    expect(screen.getByLabelText("question")).toHaveValue("and/or");
  });

  it("invokes the highlighted primitive on Enter and closes", async () => {
    const onInvoke = vi.fn();
    useCommandPaletteStore.getState().setOpen(true);
    renderWithProviders(<CommandPalette onInvoke={onInvoke} />);

    await userEvent.keyboard("{Enter}");
    expect(onInvoke).toHaveBeenCalledWith(PRIMITIVES[0]);
    expect(useCommandPaletteStore.getState().open).toBe(false);
  });

  it("moves the selection with the arrow keys, wrapping at the ends", async () => {
    useCommandPaletteStore.getState().setOpen(true);
    renderWithProviders(<CommandPalette onInvoke={noop} />);

    await userEvent.keyboard("{ArrowDown}");
    expect(useCommandPaletteStore.getState().focusedIndex).toBe(1);

    await userEvent.keyboard("{ArrowUp}{ArrowUp}");
    // A hard stop at the ends reads as the arrow key having failed.
    expect(useCommandPaletteStore.getState().focusedIndex).toBe(PRIMITIVES.length - 1);
  });

  it("keeps focus in the input while the selection moves", async () => {
    useCommandPaletteStore.getState().setOpen(true);
    renderWithProviders(<CommandPalette onInvoke={noop} />);

    const input = screen.getByRole("combobox");
    await userEvent.keyboard("{ArrowDown}");

    expect(input).toHaveFocus();
    // The selection is reported through aria-activedescendant, not by moving focus.
    expect(input).toHaveAttribute("aria-activedescendant", `palette-option-${PRIMITIVES[1]!.name}`);
  });

  it("announces the match count as the learner types", async () => {
    useCommandPaletteStore.getState().setOpen(true);
    renderWithProviders(<CommandPalette onInvoke={noop} />);

    await userEvent.type(screen.getByRole("combobox"), "prove");
    await waitFor(() => {
      expect(useAnnouncements.getState().polite).toMatch(/option/);
    });
  });

  it("closes on Escape without invoking anything", async () => {
    const onInvoke = vi.fn();
    useCommandPaletteStore.getState().setOpen(true);
    renderWithProviders(<CommandPalette onInvoke={onInvoke} />);

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(useCommandPaletteStore.getState().open).toBe(false));
    expect(onInvoke).not.toHaveBeenCalled();
  });
});

describe("primitive filtering (§9.1)", () => {
  it("returns everything for an empty query", () => {
    expect(filterPrimitives("")).toHaveLength(PRIMITIVES.length);
  });

  it("ranks a label prefix above a description match", () => {
    const results = filterPrimitives("prove");
    expect(results[0]?.name).toBe("prove_it_to_me");
  });

  it("matches a subsequence", () => {
    expect(filterPrimitives("expdif")[0]?.name).toBe("explain_differently");
  });

  it("matches on the description when the label does not", () => {
    expect(filterPrimitives("derivation").map((p) => p.name)).toContain("prove_it_to_me");
  });

  it("returns nothing for a query that matches nothing", () => {
    expect(filterPrimitives("zzzzzz")).toEqual([]);
  });
});

describe("primitive wiring (§9.2, §7.2)", () => {
  it("every primitive name matches the runtime's PRIMITIVE_NAMES", () => {
    // A button that reads correctly and posts a name the runtime rejects with a
    // 422 looks to the learner like the tutor refusing to answer.
    const runtimeNames = [
      "explain_differently",
      "prove_it_to_me",
      "where_does_this_fit",
      "vocabulary_check",
      "show_worked_example",
      "let_me_try_one",
      "why_does_this_matter",
      "im_lost",
    ];
    expect(PRIMITIVES.map((p) => p.name).sort()).toEqual([...runtimeNames].sort());
  });

  it("gives each primitive a distinct shortcut letter", () => {
    const shortcuts = PRIMITIVES.map((p) => p.shortcut);
    expect(new Set(shortcuts).size).toBe(shortcuts.length);
  });

  it("shows §9.2's contextual buttons for each state", () => {
    expect(buttonsFor("LECTURING").map((p) => p.name)).toEqual([
      "explain_differently",
      "show_worked_example",
      "im_lost",
    ]);
    expect(buttonsFor("TUTORIAL").map((p) => p.name)).toEqual([
      "prove_it_to_me",
      "where_does_this_fit",
      "im_lost",
    ]);
    expect(buttonsFor("LAB").map((p) => p.name)).toEqual(["show_worked_example", "im_lost"]);
    // §9.2: "no primitive buttons — the question flow is the primitive".
    expect(buttonsFor("PAUSED_FOR_QUESTION")).toEqual([]);
    expect(buttonsFor("CLOSING")).toEqual([]);
    expect(buttonsFor(null)).toEqual([]);
  });

  it("stops the stream only for primitives about the current exposition (§7.2)", () => {
    expect(PRIMITIVE_BY_NAME.get("explain_differently")?.interruptsStream).toBe(true);
    expect(PRIMITIVE_BY_NAME.get("why_does_this_matter")?.interruptsStream).toBe(false);
  });
});

describe("comprehension checks (§7.2, §11.1)", () => {
  it("allows a second attempt after an incorrect first, then reveals the answer", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    const onSubmit = vi
      .fn()
      .mockResolvedValueOnce({
        verdict: "incorrect" as const,
        feedback: "That's the wrong order.",
        hint: "Look at the outermost application.",
      })
      .mockResolvedValueOnce({
        verdict: "incorrect" as const,
        feedback: "Still not it.",
        modelAnswer: "Leftmost-outermost picks the outer redex first.",
      });

    renderWithProviders(
      <ComprehensionCheck question="Which redex is picked?" onSubmit={onSubmit} onComplete={noop} />,
    );

    await user.type(screen.getByLabelText(/your answer/i), "the inner one");
    await user.click(screen.getByRole("button", { name: /submit answer/i }));

    // §7.2: feedback appears after a 300ms beat.
    await act(async () => void (await vi.advanceTimersByTimeAsync(400)));
    expect(await screen.findByText(/that's the wrong order/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /show hint/i })).toBeInTheDocument();

    await user.type(screen.getByLabelText(/your answer/i), "still wrong");
    await user.click(screen.getByRole("button", { name: /submit answer/i }));
    await act(async () => void (await vi.advanceTimersByTimeAsync(400)));

    expect(await screen.findByText(/here's what a full answer looks like/i)).toBeInTheDocument();
    // The cycle is over, so there is nothing left to submit.
    expect(screen.queryByRole("button", { name: /submit answer/i })).not.toBeInTheDocument();
    expect(onSubmit).toHaveBeenCalledTimes(MAX_ATTEMPTS);

    vi.useRealTimers();
  });

  it("surfaces a grading failure rather than looking graded", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    const onSubmit = vi.fn().mockRejectedValue(new Error("network"));
    renderWithProviders(
      <ComprehensionCheck question="Which redex?" onSubmit={onSubmit} onComplete={noop} />,
    );

    await user.type(screen.getByLabelText(/your answer/i), "an answer");
    await user.click(screen.getByRole("button", { name: /submit answer/i }));

    // An ungraded answer that looks graded is worse than an error: the learner
    // moves on believing they were right.
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't be checked/i);
    vi.useRealTimers();
  });
});

describe("hints (§6.3, §11.2)", () => {
  it("reports the first expansion once, for the trace", async () => {
    const onExpand = vi.fn();
    renderWithProviders(
      <Disclosure label="Show hint" onExpand={onExpand}>
        Substitute first.
      </Disclosure>,
    );

    const trigger = screen.getByRole("button", { name: "Show hint" });
    await userEvent.click(trigger); // open
    await userEvent.click(trigger); // close
    await userEvent.click(trigger); // open again

    // "Recorded to the trace so the reviewer can see when hints were needed" --
    // needed once, not three times.
    expect(onExpand).toHaveBeenCalledOnce();
  });

  it("keeps a collapsed hint out of the DOM entirely", async () => {
    renderWithProviders(<Disclosure label="Show hint">The answer is substitution.</Disclosure>);

    // Hidden-but-present would be reachable by find-in-page and by a screen
    // reader's browse mode, handing out the hint to a learner who did not ask.
    expect(screen.queryByText(/the answer is substitution/i)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Show hint" }));
    expect(screen.getByText(/the answer is substitution/i)).toBeInTheDocument();
  });
});

describe("office-hours escalation (§8.3)", () => {
  it("offers at three tutor turns or ten minutes, and not before", () => {
    expect(shouldOfferEscalation({ tutorTurnsSinceResume: 2, minutesSinceInterrupt: 9 })).toBe(false);
    expect(shouldOfferEscalation({ tutorTurnsSinceResume: 3, minutesSinceInterrupt: 0 })).toBe(true);
    expect(shouldOfferEscalation({ tutorTurnsSinceResume: 0, minutesSinceInterrupt: 10 })).toBe(true);
  });
});

describe("announcements (§8.3, §13.4)", () => {
  it("names the exact strings §8.3 specifies", () => {
    expect(STREAM_ANNOUNCEMENTS.requestingInterrupt).toBe("Requesting interrupt");
    expect(STREAM_ANNOUNCEMENTS.readyForQuestion).toBe("Ready for your question");
    expect(STREAM_ANNOUNCEMENTS.tutorAnswering).toBe("Tutor is answering");
    expect(STREAM_ANNOUNCEMENTS.resumingLecture).toBe("Resuming lecture");
  });

  it("routes assertive and polite messages to their own channels", () => {
    useAnnouncements.getState().announce("polite thing", "polite");
    useAnnouncements.getState().announce("urgent thing", "assertive");

    expect(useAnnouncements.getState().polite).toBe("polite thing");
    expect(useAnnouncements.getState().assertive).toBe("urgent thing");
  });

  it("bumps the nonce so a repeated message is announced again", () => {
    const before = useAnnouncements.getState().nonce;
    useAnnouncements.getState().announce("Ready for your question", "assertive");
    useAnnouncements.getState().announce("Ready for your question", "assertive");

    // Identical consecutive text is not a DOM change, so without the nonce the
    // second one is silent.
    expect(useAnnouncements.getState().nonce).toBe(before + 2);
  });
});

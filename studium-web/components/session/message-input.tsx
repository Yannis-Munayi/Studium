"use client";

import { useEffect, useId, useRef, useState } from "react";
import { CLASSROOM } from "@/lib/copy/surfaces";
import { useSessionStore } from "@/lib/state/session";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";

/**
 * The always-available message input (spec §8.3).
 *
 * Carries three of §8.3's requirements at once: it is the third way to
 * interrupt ("beginning to type ... is also treated as an interrupt intent"),
 * it is what receives focus when a sentence closes (step 5), and Escape in it
 * before anything is typed cancels the interrupt.
 */
export function MessageInput({
  onSend,
  onTypingInterrupt,
  onCancelInterrupt,
  disabled = false,
}: {
  onSend: (text: string) => void;
  /** Fired once when typing begins mid-stream (§8.3's third affordance). */
  onTypingInterrupt: () => void;
  onCancelInterrupt: () => void;
  disabled?: boolean;
}) {
  const [value, setValue] = useState("");
  const phase = useSessionStore((s) => s.phase);
  const interruptRequested = useSessionStore((s) => s.interruptRequested);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const labelId = useId();

  const paused = phase === "paused";

  // §8.3 step 5: "Message input receives focus."
  useEffect(() => {
    if (paused) textareaRef.current?.focus();
  }, [paused]);

  // Grow with the content (§6.2: "expanding as needed"), capped so the reading
  // column is never pushed off screen by a long question.
  useEffect(() => {
    const node = textareaRef.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, 240)}px`;
  }, [value]);

  function submit() {
    const text = value.trim();
    if (!text) return;
    setValue("");
    onSend(text);
  }

  return (
    <div className="border-t border-line bg-background pt-normal">
      <label id={labelId} htmlFor="message-input" className="sr-only">
        {paused ? CLASSROOM.askQuestion : CLASSROOM.messagePlaceholder}
      </label>

      <div className="flex items-end gap-tight">
        <textarea
          id="message-input"
          ref={textareaRef}
          rows={1}
          value={value}
          disabled={disabled}
          aria-labelledby={labelId}
          // §8.3 step 5: the placeholder changes once the lecture has paused,
          // so the box says what it is now for rather than what it usually is.
          placeholder={paused ? CLASSROOM.askQuestion : CLASSROOM.messagePlaceholder}
          onChange={(event) => {
            const next = event.target.value;
            // Typing during a live stream *is* the interrupt gesture. Fired on
            // the transition into non-empty only, not on every keystroke.
            if (!value && next && phase === "streaming" && !interruptRequested) {
              onTypingInterrupt();
            }
            setValue(next);
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              // §8.3 "Cancellation": Escape *before typing anything* cancels.
              // With text in the box, Escape clears the box instead -- treating
              // a half-written question as consent to discard the whole
              // interrupt would lose the learner's words.
              if (value) {
                setValue("");
                return;
              }
              if (interruptRequested || paused) onCancelInterrupt();
              return;
            }
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
          className={cn(
            "min-h-10 flex-1 resize-none rounded border border-line bg-background",
            "px-3 py-2 font-sans text-sm text-ink placeholder:text-muted",
            "disabled:cursor-not-allowed disabled:text-muted",
          )}
        />

        {/* §8.3 step 5: "Send button appears." Present but inert until there is
            something to send, rather than appearing and shifting the layout. */}
        <Button variant="primary" onClick={submit} disabled={disabled || !value.trim()}>
          {CLASSROOM.send}
        </Button>
      </div>

      <p className="mt-tight font-sans text-xs text-muted">
        Enter sends · Shift+Enter for a new line
      </p>
    </div>
  );
}

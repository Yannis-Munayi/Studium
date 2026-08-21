"use client";

import { useEffect, useState } from "react";
import { Dialog } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

/**
 * The idle-timeout warning (spec §13.2, WCAG 2.2.1; §7.3).
 *
 * The runtime ends a session at `target_duration_minutes + 15`
 * (`IDLE_TIMEOUT_GRACE_MINUTES` in `state_machine.py`). WCAG 2.2.1 does not
 * allow a timer to end something without warning and without a way to extend,
 * so §13.2 specifies two prompts: at 5 minutes remaining, and at 1.
 *
 * The 5-minute notice is `polite` and the 1-minute one is a modal, because they
 * are different claims. "You have five minutes" is information. "This is about
 * to close" is a question that needs an answer.
 */

/** Matches `IDLE_TIMEOUT_GRACE_MINUTES` in the runtime. */
export const GRACE_MINUTES = 15;
const WARN_AT_MINUTES = 5;
const CONFIRM_AT_MINUTES = 1;
const TICK_MS = 15_000;

export function IdleTimeoutWarning({
  targetDurationMinutes,
  startedAt,
  onExtend,
  onExpire,
}: {
  targetDurationMinutes: number;
  /** Epoch millis. Reset by `onExtend` so extending really does extend. */
  startedAt: number;
  onExtend: () => void;
  onExpire: () => void;
}) {
  const limitMinutes = targetDurationMinutes + GRACE_MINUTES;
  const [remaining, setRemaining] = useState(limitMinutes);

  useEffect(() => {
    function tick() {
      const elapsed = (Date.now() - startedAt) / 60000;
      setRemaining(limitMinutes - elapsed);
    }
    tick();
    // A 15-second tick, not a per-second countdown. A visible clock ticking
    // down while someone is trying to understand a proof is the pressure
    // WCAG 2.2.1 exists to prevent, and this only needs to cross two
    // thresholds.
    const timer = setInterval(tick, TICK_MS);
    return () => clearInterval(timer);
  }, [limitMinutes, startedAt]);

  useEffect(() => {
    if (remaining <= 0) onExpire();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [remaining <= 0]);

  const confirming = remaining <= CONFIRM_AT_MINUTES && remaining > 0;
  const warning = remaining <= WARN_AT_MINUTES && remaining > CONFIRM_AT_MINUTES;

  return (
    <>
      {warning ? (
        <div
          role="status"
          className="fixed bottom-normal left-normal z-30 max-w-72 rounded border border-line bg-surface p-normal"
        >
          <p className="font-sans text-sm text-ink">
            This session closes in about {Math.max(1, Math.round(remaining))} minutes.
          </p>
          <Button variant="secondary" size="sm" className="mt-tight" onClick={onExtend}>
            Keep working
          </Button>
        </div>
      ) : null}

      <Dialog
        open={confirming}
        onOpenChange={(open) => !open && onExtend()}
        title="Still there?"
        description="This session is about to close on its own. Your progress is saved either way."
        footer={
          <>
            <Button variant="secondary" onClick={onExpire}>
              Close it now
            </Button>
            <Button variant="primary" autoFocus onClick={onExtend}>
              Keep working
            </Button>
          </>
        }
      >
        <p className="text-sm text-muted">
          Closing generates your session summary. You can start another right afterwards.
        </p>
      </Dialog>
    </>
  );
}

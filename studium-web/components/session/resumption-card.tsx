"use client";

import { CLASSROOM } from "@/lib/copy/surfaces";
import { Button } from "@/components/ui/button";

/**
 * §8.3 step 7's resumption card, and §8.3's office-hours escalation.
 *
 * Both are decisions offered after a Tutor answer completes, and both are
 * offers rather than transitions the system makes on the learner's behalf --
 * which is the whole point of the interruption model.
 */
export function ResumptionCard({
  onContinue,
  onAskAnother,
  showEscalation,
  onEscalate,
  onDismissEscalation,
}: {
  onContinue: () => void;
  onAskAnother: () => void;
  showEscalation: boolean;
  onEscalate: () => void;
  onDismissEscalation: () => void;
}) {
  return (
    <div className="my-loose rounded border border-line bg-surface p-normal">
      <div className="flex flex-wrap items-center gap-tight">
        <Button variant="primary" onClick={onContinue}>
          {CLASSROOM.continueLecture}
        </Button>
        <Button variant="secondary" onClick={onAskAnother}>
          {CLASSROOM.askAnother}
        </Button>
      </div>

      {showEscalation ? (
        <div className="mt-normal border-t border-line pt-normal">
          <p className="font-sans text-sm text-muted">{CLASSROOM.escalate}</p>
          <div className="mt-tight flex flex-wrap gap-tight">
            <Button variant="secondary" size="sm" onClick={onEscalate}>
              {CLASSROOM.escalateAccept}
            </Button>
            <Button variant="quiet" size="sm" onClick={onDismissEscalation}>
              {CLASSROOM.escalateDismiss}
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/**
 * §8.3's escalation heuristic: "3+ Tutor turns without resuming the lecture, or
 * 10+ minutes elapsed since the interrupt".
 *
 * Pure so §17's Tier 1 tier can assert the boundary rather than wait ten
 * minutes for it.
 */
export function shouldOfferEscalation(input: {
  tutorTurnsSinceResume: number;
  minutesSinceInterrupt: number;
}): boolean {
  return input.tutorTurnsSinceResume >= 3 || input.minutesSinceInterrupt >= 10;
}

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { CLASSROOM } from "@/lib/copy/surfaces";
import { PROMPTS, type Primitive } from "@/lib/copy/primitives";
import { announce, STREAM_ANNOUNCEMENTS } from "@/lib/a11y/announcer";
import type { SessionMode } from "@/lib/api/schemas";
import { useSessionStore } from "@/lib/state/session";
import { useSessionStream, MAX_RECONNECT_ATTEMPTS } from "@/lib/stream/useSessionStream";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { CommandPalette } from "@/components/session/command-palette";
import { ConnectionNotice, RuntimeDegradation } from "@/components/session/degradation-notice";
import { FloatingActions } from "@/components/session/floating-actions";
import { MessageInput } from "@/components/session/message-input";
import { ResumptionCard, shouldOfferEscalation } from "@/components/session/resumption-card";
import { Transcript } from "@/components/session/transcript";
import { SessionCloseDialog } from "./session-close";
import { IdleTimeoutWarning } from "@/components/session/idle-timeout";

/**
 * The classroom (spec §6.2) and the session lifecycle it hosts (§7).
 *
 * This is the only component that owns a stream. Everything below it renders
 * what the stream produced; everything above it routes to it. That split is
 * what keeps §8's interaction in one readable place instead of spread across a
 * provider, three hooks and a context.
 */
export function Classroom({
  sessionId,
  mode,
  subjectName,
  conceptName,
  targetDurationMinutes,
}: {
  sessionId: string;
  mode: SessionMode;
  subjectName: string;
  conceptName: string | null;
  targetDurationMinutes: number;
}) {
  const router = useRouter();
  const store = useSessionStore();
  const { send, interrupt, cancelInterrupt, retry } = useSessionStream(sessionId);

  const [confirmingClose, setConfirmingClose] = useState(false);
  const [closing, setClosing] = useState(false);
  const [summaryOpen, setSummaryOpen] = useState(false);
  const [escalationDismissed, setEscalationDismissed] = useState(false);
  const [tutorTurnsSinceResume, setTutorTurnsSinceResume] = useState(0);

  const startedAt = useRef(Date.now());
  const interruptedAt = useRef<number | null>(null);
  const opened = useRef(false);

  const { phase, turns, degradation, reconnectAttempts } = store;

  // Open the session and pull the first segment exactly once. Two guards, not
  // one: `opened` survives React 19's development double-invoke of effects,
  // which would otherwise bill a second Lecturer call on every mount.
  useEffect(() => {
    if (opened.current) return;
    opened.current = true;
    store.open({ sessionId, mode });
    void send({ text: "", speaker: mode === "lecture" ? "lecturer" : "tutor" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, mode]);

  const handleInterrupt = useCallback(() => {
    interruptedAt.current = Date.now();
    void interrupt();
  }, [interrupt]);

  const handleSend = useCallback(
    (text: string) => {
      store.beginTurn("learner");
      store.appendText(text);
      store.finishTurn();
      setTutorTurnsSinceResume((n) => n + 1);
      announce(STREAM_ANNOUNCEMENTS.tutorAnswering, "assertive");
      void send({ text, speaker: "tutor" });
    },
    [send, store],
  );

  /** §7.2: a primitive stops the stream only when it is about the exposition. */
  const handlePrimitive = useCallback(
    (primitive: Primitive) => {
      if (primitive.interruptsStream && phase === "streaming") handleInterrupt();
      void send({
        text: PROMPTS[primitive.name] ?? "",
        primitive: primitive.name,
        speaker: "tutor",
      });
    },
    [handleInterrupt, phase, send],
  );

  /** §8.3 step 7: continue triggers a resume with a recap. */
  const handleContinue = useCallback(() => {
    setTutorTurnsSinceResume(0);
    setEscalationDismissed(false);
    interruptedAt.current = null;
    announce(STREAM_ANNOUNCEMENTS.lectureContinuing, "assertive");
    void send({ text: "", speaker: "lecturer" });
  }, [send]);

  /**
   * §8.3 "Cancellation": Escape before typing resumes the lecture.
   *
   * The spec is specific that this is a *resume*, not a pause -- "no new stream
   * from the Tutor, the interrupted lecture resumes with a two-sentence recap".
   * Leaving the learner parked on a resumption card would be a different
   * interaction: they raised their hand, changed their mind, and should be back
   * where they were without a second decision to make.
   */
  const handleCancelInterrupt = useCallback(() => {
    cancelInterrupt();
    interruptedAt.current = null;
    void send({ text: "", speaker: "lecturer" });
  }, [cancelInterrupt, send]);

  const handleClose = useCallback(async () => {
    setClosing(true);
    setSummaryOpen(true);
  }, []);

  const elapsedMinutes = (Date.now() - startedAt.current) / 60000;
  const minutesSinceInterrupt =
    interruptedAt.current === null ? 0 : (Date.now() - interruptedAt.current) / 60000;

  const streaming = phase === "streaming" || phase === "connecting" || phase === "interrupting";
  const showResumption = phase === "paused" || (phase === "complete" && tutorTurnsSinceResume > 0);

  return (
    <div className="flex min-h-dvh flex-col">
      <SessionHeader
        subjectName={subjectName}
        conceptName={conceptName}
        turnCount={turns.filter((t) => t.speaker !== "learner").length}
        onClose={() => setConfirmingClose(true)}
        closeDisabled={closing}
      />

      <main id="main" className="flex-1 px-normal py-generous">
        <h1 className="sr-only">
          {subjectName}
          {conceptName ? ` — ${conceptName}` : ""}
        </h1>

        <Transcript turns={turns} streaming={streaming}>
          {phase === "connecting" && turns.every((t) => !t.text) ? (
            <PreparingNotice />
          ) : null}

          {/* The runtime's own degradation copy, rendered verbatim (§21). */}
          {degradation ? (
            <RuntimeDegradation text={degradation.text} reason={degradation.reason} onRetry={retry} />
          ) : null}

          {phase === "reconnecting" || phase === "failed" ? (
            <ConnectionNotice
              attempts={reconnectAttempts}
              maxAttempts={MAX_RECONNECT_ATTEMPTS}
              onRetry={retry}
            />
          ) : null}

          {showResumption ? (
            <ResumptionCard
              onContinue={handleContinue}
              onAskAnother={() => document.getElementById("message-input")?.focus()}
              showEscalation={
                !escalationDismissed &&
                shouldOfferEscalation({ tutorTurnsSinceResume, minutesSinceInterrupt })
              }
              onEscalate={() => {
                setEscalationDismissed(true);
                void send({ text: "", primitive: null, speaker: "tutor" });
              }}
              onDismissEscalation={() => setEscalationDismissed(true)}
            />
          ) : null}
        </Transcript>
      </main>

      <div className="sticky bottom-0 border-line bg-background px-normal pb-normal">
        <div className="mx-auto max-w-[var(--container-measure)]">
          <MessageInput
            onSend={handleSend}
            onTypingInterrupt={handleInterrupt}
            onCancelInterrupt={handleCancelInterrupt}
            disabled={closing}
          />
        </div>
      </div>

      <FloatingActions
        onInterrupt={handleInterrupt}
        onPrimitive={handlePrimitive}
        elapsedMinutes={elapsedMinutes}
        costUsd={null}
      />

      <CommandPalette onInvoke={handlePrimitive} />

      {/* §13.2 (WCAG 2.2.1): warn before a timer ends anything. */}
      <IdleTimeoutWarning
        targetDurationMinutes={targetDurationMinutes}
        startedAt={startedAt.current}
        onExtend={() => (startedAt.current = Date.now())}
        onExpire={handleClose}
      />

      <ConfirmDialog
        open={confirmingClose}
        onOpenChange={setConfirmingClose}
        title={CLASSROOM.closeSession}
        message={CLASSROOM.confirmClose}
        confirmLabel={CLASSROOM.confirmCloseAction}
        cancelLabel={CLASSROOM.cancel}
        onConfirm={() => void handleClose()}
      />

      <SessionCloseDialog
        open={summaryOpen}
        sessionId={sessionId}
        onDismiss={() => {
          setSummaryOpen(false);
          store.reset();
          router.push("/");
        }}
      />
    </div>
  );
}

function SessionHeader({
  subjectName,
  conceptName,
  turnCount,
  onClose,
  closeDisabled,
}: {
  subjectName: string;
  conceptName: string | null;
  turnCount: number;
  onClose: () => void;
  closeDisabled: boolean;
}) {
  return (
    <header className="sticky top-0 z-20 border-b border-line bg-background/95 backdrop-blur-sm">
      <div className="mx-auto flex max-w-5xl items-center justify-between gap-normal px-normal py-3">
        <div className="min-w-0">
          <p className="truncate font-sans text-sm font-medium text-ink">{subjectName}</p>
          {conceptName ? (
            <p className="truncate font-sans text-xs text-muted">{conceptName}</p>
          ) : null}
        </div>

        <div className="flex items-center gap-normal">
          {/* A count of what has happened, not a percentage of what will. The
              session has no fixed length, so a progress bar would be fiction. */}
          <p className="font-sans text-xs text-muted" aria-live="off">
            {turnCount} {turnCount === 1 ? "segment" : "segments"}
          </p>
          {/* §8.4: close cannot be interrupted, so it disables during close. */}
          <Button variant="quiet" size="sm" onClick={onClose} disabled={closeDisabled}>
            {CLASSROOM.closeSession}
          </Button>
        </div>
      </div>
    </header>
  );
}

/** §7.1's loading state: calm, and explaining itself if it runs long. */
function PreparingNotice() {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setSlow(true), 2000);
    return () => clearTimeout(timer);
  }, []);

  return (
    <div role="status" className="py-loose text-center">
      <p className="font-sans text-sm text-muted">Preparing your session…</p>
      {slow ? (
        <p className="mt-tight font-sans text-xs text-muted">
          Opening a session assembles your prior context. This can take a moment.
        </p>
      ) : null}
    </div>
  );
}

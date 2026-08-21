"use client";

import { CONNECTION, copyFor, type DegradationCopy } from "@/lib/copy/degradation";
import type { FailureKind } from "@/lib/api/errors";
import { Button } from "@/components/ui/button";
import { OfflineIcon, WarningIcon, iconProps } from "@/components/ui/icons";

/**
 * Legible degradation (spec §3, §15).
 *
 * One component for every "something went wrong and here is what to do about
 * it" panel in the session surface, so the tone is uniform and the shape is
 * predictable: a title naming what happened, a line naming what to do, and at
 * most one action.
 */
export function DegradationNotice({
  copy,
  onRetry,
  variant = "warning",
}: {
  copy: DegradationCopy;
  onRetry?: () => void;
  variant?: "warning" | "offline";
}) {
  const Icon = variant === "offline" ? OfflineIcon : WarningIcon;

  return (
    <div
      // `alert` rather than `status`: these interrupt what the learner was
      // doing, and §13.4 assigns interruptions the assertive channel.
      role="alert"
      className="my-loose flex items-start gap-normal rounded border border-[var(--color-attention)] bg-surface p-normal"
    >
      <span className="mt-0.5 shrink-0 text-[var(--color-attention)]">
        <Icon {...iconProps} />
      </span>
      <div className="flex-1">
        <p className="font-sans text-sm font-medium text-ink">{copy.title}</p>
        <p className="mt-1 font-sans text-sm text-muted">{copy.body}</p>
        {copy.action && onRetry ? (
          <Button variant="secondary" size="sm" className="mt-normal" onClick={onRetry}>
            {copy.action}
          </Button>
        ) : null}
      </div>
    </div>
  );
}

/**
 * The runtime's own `degraded` chunk (agent runtime §21).
 *
 * The text comes down the wire already written for the learner, so it is shown
 * verbatim -- see the note at the top of `lib/copy/degradation.ts`. The `reason`
 * is not rendered: it names a mechanism ("content_filter", "parse_failure") and
 * §21's second rule is that the learner never sees the mechanism.
 */
export function RuntimeDegradation({
  text,
  onRetry,
}: {
  text: string;
  reason?: string;
  onRetry?: () => void;
}) {
  return (
    <DegradationNotice
      copy={{ title: text, body: "", action: onRetry ? "Try again" : null }}
      {...(onRetry ? { onRetry } : {})}
    />
  );
}

/** §15.3's two stages of a dropped SSE connection. */
export function ConnectionNotice({
  attempts,
  maxAttempts,
  onRetry,
}: {
  attempts: number;
  maxAttempts: number;
  onRetry: () => void;
}) {
  return (
    <DegradationNotice
      variant="offline"
      copy={attempts >= maxAttempts ? CONNECTION.lost : CONNECTION.reconnecting}
      onRetry={onRetry}
    />
  );
}

/** An `ApiError` the client classified itself. */
export function ApiFailureNotice({ kind, onRetry }: { kind: FailureKind; onRetry?: () => void }) {
  return <DegradationNotice copy={copyFor(kind)} {...(onRetry ? { onRetry } : {})} />;
}

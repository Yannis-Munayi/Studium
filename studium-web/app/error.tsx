"use client";

import { useEffect } from "react";
import Link from "next/link";
import { BOUNDARY } from "@/lib/copy/degradation";
import { Button } from "@/components/ui/button";

/**
 * The route error boundary (spec §15.1).
 *
 * Two actions, both named in §15.1: reset, and return to the desk. The second
 * matters more than it looks -- a boundary whose only escape is "try again"
 * strands a learner on a route that reliably throws.
 */
export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // §15.1 sends these to Sentry, which subsystem 7 configures. Until it
    // exists the console is the destination, and saying so beats an empty
    // `TODO` that reads as if reporting were wired.
    console.error("[boundary]", error.digest ?? "", error);
  }, [error]);

  return (
    <div role="alert" className="mx-auto max-w-md px-normal py-generous text-center">
      <h1 className="font-serif text-xl font-semibold leading-[var(--leading-heading)]">
        {BOUNDARY.title}
      </h1>
      <p className="mt-tight font-sans text-sm text-muted">{BOUNDARY.body}</p>

      <div className="mt-loose flex justify-center gap-tight">
        <Button variant="primary" onClick={reset}>
          {BOUNDARY.action}
        </Button>
        <Button variant="secondary" asChild>
          <Link href="/">Return to the desk</Link>
        </Button>
      </div>

      {error.digest ? (
        <p className="mt-loose font-mono text-xs text-muted">Reference: {error.digest}</p>
      ) : null}
    </div>
  );
}

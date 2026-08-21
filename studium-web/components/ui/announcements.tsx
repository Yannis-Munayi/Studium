"use client";

import { useEffect, useState } from "react";
import { useAnnouncements } from "@/lib/a11y/announcer";

/**
 * The app's live regions (spec §13.4, WCAG 4.1.3).
 *
 * Rendered exactly once, at the top of the app shell. Two regions, one per
 * politeness, because a single element cannot be both -- see the note in
 * `lib/a11y/announcer.ts` for why that is still "one region", not a workaround.
 *
 * Both are in the DOM from first paint and start empty. A live region inserted
 * at the same time as its first message is not announced by most screen
 * readers: the region has to exist before the mutation it is reporting.
 */
export function Announcements() {
  const polite = useAnnouncements((s) => s.polite);
  const assertive = useAnnouncements((s) => s.assertive);
  const nonce = useAnnouncements((s) => s.nonce);

  // Identical consecutive messages ("Ready for your question" twice in a row)
  // are not a DOM change, so nothing is announced the second time. Clearing on
  // the nonce and re-setting on the next frame makes each one a real mutation.
  const [renderedPolite, setRenderedPolite] = useState("");
  const [renderedAssertive, setRenderedAssertive] = useState("");

  useEffect(() => {
    setRenderedPolite("");
    setRenderedAssertive("");
    const frame = requestAnimationFrame(() => {
      setRenderedPolite(polite);
      setRenderedAssertive(assertive);
    });
    return () => cancelAnimationFrame(frame);
  }, [polite, assertive, nonce]);

  return (
    <>
      <div
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="sr-only"
        data-testid="announcer-polite"
      >
        {renderedPolite}
      </div>
      <div
        role="alert"
        aria-live="assertive"
        aria-atomic="true"
        className="sr-only"
        data-testid="announcer-assertive"
      >
        {renderedAssertive}
      </div>
    </>
  );
}

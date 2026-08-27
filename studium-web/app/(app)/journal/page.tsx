import { Suspense } from "react";
import { Journal } from "@/components/surfaces/journal";
import { currentUserId } from "@/lib/auth";

/**
 * The journal browser (spec §6.4).
 *
 * `useSearchParams` in the client surface requires a Suspense boundary above
 * it, or the whole route opts out of static rendering with a build warning.
 */
export default async function JournalPage() {
  // Called for its effect, not its value: `cookies()` inside it is what marks
  // this route as request-dependent, and the journal itself needs no id —
  // §6.4's list is every subject's entries and the endpoint reads "me".
  await currentUserId();

  return (
    <Suspense fallback={<p className="p-normal font-sans text-sm text-muted">Loading…</p>}>
      <Journal />
    </Suspense>
  );
}

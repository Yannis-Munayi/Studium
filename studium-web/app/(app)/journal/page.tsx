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
  // The learner-subject the journal is scoped to comes from the desk payload
  // in the finished shape; until that endpoint exists (F4) the surface renders
  // its unavailable state, which needs no id to do.
  await currentUserId();

  return (
    <Suspense fallback={<p className="p-normal font-sans text-sm text-muted">Loading…</p>}>
      <Journal learnerSubjectId={null} />
    </Suspense>
  );
}

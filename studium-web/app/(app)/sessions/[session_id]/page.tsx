import { notFound } from "next/navigation";
import { Classroom } from "@/components/surfaces/classroom";
import { sessionMode, type SessionMode } from "@/lib/api/schemas";
import { isUuid } from "@/lib/auth";

/**
 * The session runtime (spec §6.2, §6.3).
 *
 * One route serves both the classroom and the bench, because they are two
 * renderings of one session rather than two places -- §7.2's "the classroom
 * stays mounted, the content changes" only holds if a mode change is not a
 * navigation. `Classroom` switches on the runtime state it receives from the
 * stream.
 */
export default async function SessionPage({
  params,
  searchParams,
}: {
  params: Promise<{ session_id: string }>;
  searchParams: Promise<{ mode?: string; subject?: string; concept?: string; minutes?: string }>;
}) {
  const { session_id: sessionId } = await params;
  if (!isUuid(sessionId)) notFound();

  const query = await searchParams;

  // §14.3 puts mode in the URL. It is a hint for the first render only -- the
  // runtime's `end` chunks are authoritative from the first turn onward, so a
  // stale or hand-edited query param corrects itself rather than sticking.
  const parsedMode = sessionMode.safeParse(query.mode);
  const mode: SessionMode = parsedMode.success ? parsedMode.data : "tutorial";

  const minutes = Number(query.minutes);

  return (
    <Classroom
      sessionId={sessionId}
      mode={mode}
      subjectName={query.subject ?? "Your subject"}
      conceptName={query.concept ?? null}
      targetDurationMinutes={Number.isFinite(minutes) && minutes > 0 ? minutes : 90}
    />
  );
}

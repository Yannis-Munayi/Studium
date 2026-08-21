import { SurfaceUnavailable } from "@/components/shared/unavailable";

/**
 * Subject detail (spec §5.1).
 *
 * §5.1 lists the route ("roadmap, concepts") and no section specifies its
 * contents -- the concept graph map that would fill it is one of the four
 * deferred surfaces (§2). The route exists so the desk's links have somewhere
 * to go and so adding the map is a page body rather than a routing change.
 */
export default async function SubjectPage({
  params,
}: {
  params: Promise<{ subject_slug: string }>;
}) {
  const { subject_slug: slug } = await params;

  return (
    <div className="mx-auto max-w-2xl px-normal py-loose">
      <h1 className="font-sans text-2xl font-semibold leading-[var(--leading-heading)]">{slug}</h1>
      <div className="mt-loose">
        <SurfaceUnavailable endpoint="desk" what="The subject roadmap" />
      </div>
    </div>
  );
}

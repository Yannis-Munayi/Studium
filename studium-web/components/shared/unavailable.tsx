import { SURFACE_ENDPOINTS, type SurfaceEndpoint } from "@/lib/api/surfaces";

/**
 * What a surface shows when its endpoint is a subsystem behind (F4).
 *
 * §3 asks for degradation that "names what happened and what to do next", and
 * that rule does not stop applying because the cause is a build order rather
 * than an outage. The learner-facing line says the feature is not connected
 * yet; the endpoint name is in a `title` for whoever is building, not on screen
 * for whoever is studying.
 */
export function SurfaceUnavailable({
  endpoint,
  what,
}: {
  endpoint: SurfaceEndpoint;
  /** What the learner was trying to see, in their words. */
  what: string;
}) {
  return (
    <div
      className="rounded border border-dashed border-line p-loose text-center"
      title={`Needs: ${SURFACE_ENDPOINTS[endpoint]}`}
      data-unavailable={endpoint}
    >
      <p className="font-sans text-sm text-ink">{what} isn&apos;t connected yet.</p>
      <p className="mx-auto mt-tight max-w-md font-sans text-sm text-muted">
        Your work is being recorded — this is the reading side of it, and it needs an endpoint the
        backend hasn&apos;t shipped. Nothing here is lost in the meantime.
      </p>
    </div>
  );
}

/**
 * The public health endpoint (infrastructure §7.4, §4.3).
 *
 * §7.4: "External service (Uptime Robot or Better Stack, free tier) pings
 * `https://studium.app/health` every 5 minutes. Failure fires an alert per §8."
 *
 * That URL is the *frontend's* origin — §4.7 gives only this app a public IP —
 * so this route is what the outside world calls "is Studium up", and it has to
 * mean the whole product rather than this process.
 *
 * **So it checks the backend, and the way it reports the result is the
 * decision worth stating.** §15's failure table lists "Backend down" with the
 * detection "uptime monitoring fires (frontend can't reach backend)", which
 * only happens if an unreachable backend makes *this* endpoint fail. It does:
 * an unreachable backend returns 503 here. What it does not do is fail when
 * the backend answers in a degraded state — a model provider outage leaves the
 * backend serving the desk, the journal and every read, and taking the
 * frontend out of Fly's routing pool for that would convert a degraded product
 * into an absent one.
 *
 * **Cheap on purpose.** Polled every 5 minutes by the monitor and every 15
 * seconds by Fly's own check. One short GET with a 3-second timeout, no
 * database, no model call, nothing cached.
 */

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const BACKEND_ORIGIN = process.env["STUDIUM_BACKEND_ORIGIN"] ?? "http://127.0.0.1:8000";

/** Shorter than Fly's 5s check timeout, so this answers rather than being cut off. */
const BACKEND_TIMEOUT_MS = 3000;

type BackendHealth = {
  reachable: boolean;
  status?: number;
  detail?: string;
  database?: unknown;
};

export async function GET(): Promise<Response> {
  const backend = await checkBackend();

  const body = {
    status: backend.reachable ? "ok" : "degraded",
    app: "studium-web",
    environment: process.env["STUDIUM_ENV"] ?? "local",
    release: process.env["FLY_MACHINE_VERSION"] ?? "dev",
    backend,
    note: backend.reachable
      ? undefined
      : "The frontend is serving but cannot reach the backend (§15, 'Backend down').",
  };

  return Response.json(body, {
    // 503 takes this instance out of Fly's routing pool and fires §8.1's
    // uptime alert after two consecutive failures. Correct for an unreachable
    // backend: there is nothing useful this app can serve without it.
    status: backend.reachable ? 200 : 503,
    headers: { "Cache-Control": "no-store" },
  });
}

async function checkBackend(): Promise<BackendHealth> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), BACKEND_TIMEOUT_MS);
  try {
    const response = await fetch(new URL("/health", BACKEND_ORIGIN), {
      signal: controller.signal,
      cache: "no-store",
    });
    // The backend answers 503 when *its* database is unreachable and 200 with
    // a `providers` block when a model provider is not. Either is an answer,
    // and passing the status through means the monitor's alert body says which
    // layer is broken instead of only that something is.
    const payload = (await response.json().catch(() => ({}))) as Record<string, unknown>;
    return {
      reachable: response.ok,
      status: response.status,
      database: payload["database"],
      detail: response.ok ? undefined : "the backend reported itself unhealthy",
    };
  } catch (cause) {
    return {
      reachable: false,
      detail: cause instanceof Error ? `${cause.name}: ${cause.message}` : "unreachable",
    };
  } finally {
    clearTimeout(timer);
  }
}

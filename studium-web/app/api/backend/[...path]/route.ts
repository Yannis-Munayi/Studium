import { currentUserId } from "@/lib/auth";

/**
 * The FastAPI proxy (spec §5.1, "proxy routes to FastAPI backend as needed").
 *
 * Every backend call goes through here, for two reasons.
 *
 * SSE has to survive the hop: a Next route that buffers the body turns a
 * streaming lecture into a single block that arrives when the lecture ends --
 * which looks exactly like the server having hung. The backend already sets
 * `X-Accel-Buffering: no` for the proxy in front of *it*; this is the same
 * problem one layer up. `duplex: "half"` and passing `response.body` through
 * untouched are what keep the stream a stream.
 *
 * And this is **the one place a request can acquire identity**. The read
 * endpoints (`/api/user/me/*`, the journal, the session summary) answer for a
 * learner, and they learn which one from `X-Studium-User` set here from the
 * server-side session. A client cannot choose: any inbound value of that header
 * is stripped before the resolved one goes on. That does not make the API
 * authenticated — the backend still trusts the header, exactly as
 * `POST /api/session` still trusts a `user_id` in its body (F7) — but it does
 * mean no code path in this app can forget to say who is asking, and none can
 * lie about it either.
 */

const BACKEND_ORIGIN = process.env["STUDIUM_BACKEND_ORIGIN"] ?? "http://127.0.0.1:8000";

/** Read by `backend/studium/api/reads.py`. Kept in step with `USER_HEADER` there. */
const USER_HEADER = "x-studium-user";

/**
 * Correlation id (infrastructure §7.3, §3).
 *
 * A learner action is two HTTP requests — browser to this route, this route to
 * FastAPI — and the useful question is "what did this click do", which spans
 * both. Setting the id here and letting the backend honour it is what joins
 * them; `backend/studium/observability/correlation.py` reads this exact header
 * and puts the value on every log line, every Sentry event and every OTel span
 * under the request.
 *
 * A client-supplied value is passed through when it is well-formed. It is not
 * a credential and is never used for authorization — the backend re-sanitises
 * it, and a bad one is replaced rather than trusted.
 */
const CORRELATION_HEADER = "x-request-id";

/** Never buffer, never cache, never statically optimise. */
export const dynamic = "force-dynamic";
export const runtime = "nodejs";
/** An SSE turn can run for minutes; the default would cut a lecture short. */
export const maxDuration = 300;

async function proxy(request: Request, path: string[]): Promise<Response> {
  const incoming = new URL(request.url);
  const target = new URL(`/${path.join("/")}${incoming.search}`, BACKEND_ORIGIN);

  const headers = new Headers(request.headers);
  // Host must name the target, not this app, or FastAPI builds wrong absolute
  // URLs. The rest are hop-by-hop and must not be forwarded.
  headers.delete("host");
  headers.delete("connection");
  headers.delete("content-length");

  // Deleted before it is set, unconditionally. Without the delete a browser
  // could send its own `X-Studium-User` and read any learner's journal; with
  // it, the only value the backend ever sees is the one resolved here.
  headers.delete(USER_HEADER);
  const userId = await identify();
  if (userId) headers.set(USER_HEADER, userId);

  const correlationId = resolveCorrelationId(request);
  headers.set(CORRELATION_HEADER, correlationId);

  let response: Response;
  try {
    response = await fetch(target, {
      method: request.method,
      headers,
      body: hasBody(request.method) ? request.body : null,
      // Required by undici whenever a stream is used as a request body.
      ...(hasBody(request.method) ? { duplex: "half" } : {}),
      redirect: "manual",
      signal: request.signal,
    } as RequestInit);
  } catch (cause) {
    // The backend being down is an operational fact, not a 500 from this app.
    // The correlation id goes out with the failure too: this is the one case
    // where the backend logged nothing, so the id in the browser's response is
    // the only thread back to the request that failed.
    console.error(`[proxy] backend unreachable (${correlationId})`, cause);
    return Response.json(
      { detail: "The tutor service is not reachable." },
      { status: 502, headers: { [CORRELATION_HEADER]: correlationId } },
    );
  }

  const outgoing = new Headers(response.headers);
  outgoing.delete("content-encoding");
  outgoing.delete("content-length");
  outgoing.delete("transfer-encoding");
  outgoing.set(CORRELATION_HEADER, correlationId);

  if (outgoing.get("content-type")?.includes("text/event-stream")) {
    outgoing.set("Cache-Control", "no-cache, no-transform");
    outgoing.set("Connection", "keep-alive");
    outgoing.set("X-Accel-Buffering", "no");
  }

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: outgoing,
  });
}

function hasBody(method: string): boolean {
  return method !== "GET" && method !== "HEAD";
}

/**
 * The id this request travels under.
 *
 * An inbound value survives only if it looks like an opaque token: at most 64
 * characters of `[A-Za-z0-9-_.]`. Anything else is replaced rather than
 * escaped, because a correlation id containing a newline is not one anybody
 * meant to send, and this value ends up in log lines on both sides.
 * `sanitize()` in `backend/studium/observability/correlation.py` applies the
 * identical rule — deliberately duplicated rather than shared, since the two
 * are different runtimes and a header contract is the wrong thing to make one
 * of them depend on the other for.
 */
function resolveCorrelationId(request: Request): string {
  const inbound = request.headers.get(CORRELATION_HEADER)?.trim() ?? "";
  if (inbound && inbound.length <= 64 && /^[A-Za-z0-9\-_.]+$/.test(inbound)) {
    return inbound;
  }
  return crypto.randomUUID().replaceAll("-", "");
}

/**
 * Who the request is for, or null when nobody has identified themselves.
 *
 * Null forwards nothing rather than forwarding a placeholder, so an
 * unidentified caller gets the backend's 401 and the surfaces get an error they
 * can render as "sign in" instead of someone else's data. The throw case is
 * `lib/auth.ts` refusing to run its development placeholder in production; it
 * is swallowed to null here for the same reason — a proxy that 500s on every
 * route because identity is unconfigured is a worse diagnostic than a 401 from
 * the endpoint that wanted it.
 */
async function identify(): Promise<string | null> {
  try {
    return await currentUserId();
  } catch (cause) {
    console.error("[proxy] could not resolve the learner", cause);
    return null;
  }
}

type Context = { params: Promise<{ path: string[] }> };

export async function GET(request: Request, context: Context) {
  return proxy(request, (await context.params).path);
}
export async function POST(request: Request, context: Context) {
  return proxy(request, (await context.params).path);
}
export async function PATCH(request: Request, context: Context) {
  return proxy(request, (await context.params).path);
}
export async function DELETE(request: Request, context: Context) {
  return proxy(request, (await context.params).path);
}

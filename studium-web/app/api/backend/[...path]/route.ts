/**
 * The FastAPI proxy (spec §5.1, "proxy routes to FastAPI backend as needed").
 *
 * Every backend call goes through here. The reason is not tidiness: it is that
 * SSE has to survive the hop, and a Next route that buffers the body turns a
 * streaming lecture into a single block that arrives when the lecture ends --
 * which looks exactly like the server having hung. The backend already sets
 * `X-Accel-Buffering: no` for the proxy in front of *it*; this is the same
 * problem one layer up.
 *
 * `duplex: "half"` and passing `response.body` through untouched are what keep
 * the stream a stream.
 */

const BACKEND_ORIGIN = process.env["STUDIUM_BACKEND_ORIGIN"] ?? "http://127.0.0.1:8000";

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
    console.error("[proxy] backend unreachable", cause);
    return Response.json(
      { detail: "The tutor service is not reachable." },
      { status: 502 },
    );
  }

  const outgoing = new Headers(response.headers);
  outgoing.delete("content-encoding");
  outgoing.delete("content-length");
  outgoing.delete("transfer-encoding");

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

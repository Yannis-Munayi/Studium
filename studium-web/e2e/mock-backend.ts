import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";

/**
 * A mocked-LLM backend for Tier 2 (spec §17).
 *
 * §17's Tier 2 is "E2E against a live backend, no LLM calls". This is that
 * backend: it speaks the exact wire contract `backend/studium/api/app.py`
 * speaks -- same routes, same chunk kinds, same SSE framing, same status codes
 * -- and produces scripted output instead of calling a model.
 *
 * **What this does and does not verify.** It verifies the frontend against the
 * *contract*: that a real HTTP SSE stream over a real socket, arriving in real
 * chunks with real timing, drives the classroom correctly. It does not verify
 * the contract itself -- if the runtime's framing changed, this mock would keep
 * agreeing with a frontend that had stopped working. That is Tier 3's job, and
 * it is why Tier 3 exists rather than being an optimisation.
 *
 * Deliberately dependency-free and process-local: it starts in the Playwright
 * worker, so a Tier 2 run needs no Postgres, no API key, and no Docker.
 */

export interface MockBackendOptions {
  /** Text delivered per turn, one array element per SSE `text` chunk. */
  script?: string[];
  /** Milliseconds between chunks. Non-zero so interrupts have a stream to hit. */
  chunkDelayMs?: number;
  /** Force `POST /api/session` to answer 402 with a budget body. */
  budgetExceeded?: boolean;
  /** Drop the connection mid-stream, without the terminator (§15.3). */
  dropAfterChunks?: number | null;
  /** Emit a `degraded` chunk instead of finishing normally (§21). */
  degradeWith?: { text: string; reason: string } | null;
}

const DEFAULT_SCRIPT = [
  "Beta reduction is the rule that ",
  "rewrites an application of a lambda abstraction",
  " to its argument [P1]. ",
  "The redex is chosen leftmost-outermost. ",
  "That order is normalising, which is the property that matters here. ",
];

export interface MockBackend {
  origin: string;
  close: () => Promise<void>;
  /** Interrupt POSTs received, in order. */
  interrupts: string[];
  /** Turn requests received, in order. */
  turns: Array<{ sessionId: string; text: string; primitive: string | null }>;
  configure: (options: Partial<MockBackendOptions>) => void;
}

const SESSION_ID = "0f8fad5b-d9cb-469f-a165-70867728950e";
const ARTIFACT_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7";
const CHUNK_ID = "16fd2706-8baf-433b-82eb-8c7fada847da";

export async function startMockBackend(
  initial: MockBackendOptions = {},
  port = 0,
): Promise<MockBackend> {
  let options: MockBackendOptions = {
    script: DEFAULT_SCRIPT,
    chunkDelayMs: 40,
    budgetExceeded: false,
    dropAfterChunks: null,
    degradeWith: null,
    ...initial,
  };

  const interrupts: string[] = [];
  const turns: MockBackend["turns"] = [];
  /** Set when an interrupt POST lands, cleared when the next turn starts. */
  let interrupted = false;

  const server: Server = createServer((request, response) => {
    void route(request, response);
  });

  async function route(request: IncomingMessage, response: ServerResponse): Promise<void> {
    const url = new URL(request.url ?? "/", "http://localhost");
    const path = url.pathname;

    if (path === "/health") return json(response, 200, { status: "ok", subsystem: "mock" });

    // --- control channel ---------------------------------------------------
    // The mock runs in its own process (started by Playwright's globalSetup, so
    // the Next server can be pointed at a known port before it boots). Tests
    // therefore cannot call `configure` directly, and reconfigure over HTTP
    // instead. Namespaced under `__` so it can never collide with a real route.

    if (path === "/__control" && request.method === "POST") {
      const body = await readJson(request);
      options = { ...options, ...(body as Partial<MockBackendOptions>) };
      return json(response, 200, { ok: true });
    }

    if (path === "/__control/reset" && request.method === "POST") {
      options = {
        script: DEFAULT_SCRIPT,
        chunkDelayMs: 40,
        budgetExceeded: false,
        dropAfterChunks: null,
        degradeWith: null,
      };
      interrupts.length = 0;
      turns.length = 0;
      interrupted = false;
      return json(response, 200, { ok: true });
    }

    if (path === "/__state") {
      return json(response, 200, { interrupts, turns });
    }

    if (path === "/api/session" && request.method === "POST") {
      if (options.budgetExceeded) {
        return json(response, 402, {
          detail: {
            message:
              "You've reached your daily usage limit. Your progress is saved -- resume tomorrow (resets 12:00 am).",
            scope: "daily",
            reset_at: "2026-08-22T04:00:00+00:00",
          },
        });
      }
      return json(response, 201, { session_id: SESSION_ID, state: "LECTURING" });
    }

    const turnMatch = /^\/api\/session\/([^/]+)\/turn$/.exec(path);
    if (turnMatch && request.method === "POST") {
      const body = await readJson(request);
      turns.push({
        sessionId: turnMatch[1] as string,
        text: String(body?.["text"] ?? ""),
        primitive: (body?.["primitive"] as string | null) ?? null,
      });
      interrupted = false;
      return stream(response);
    }

    const interruptMatch = /^\/api\/session\/([^/]+)\/interrupt$/.exec(path);
    if (interruptMatch && request.method === "POST") {
      interrupts.push(interruptMatch[1] as string);
      interrupted = true;
      return json(response, 200, {
        accepted: true,
        state: "LECTURING",
        detail: "interrupt signalled; the current sentence will finish",
      });
    }

    const closeMatch = /^\/api\/session\/([^/]+)\/close$/.exec(path);
    if (closeMatch && request.method === "POST") {
      return json(response, 200, {
        session_id: closeMatch[1],
        ended: true,
        summarised: true,
        errors: [],
      });
    }

    const stateMatch = /^\/api\/session\/([^/]+)\/state$/.exec(path);
    if (stateMatch) {
      return json(response, 200, {
        session_id: stateMatch[1],
        state: "LECTURING",
        persisted_mode: "lecture",
        exchange_index: 1,
        interruptible: true,
        transitions: [],
      });
    }

    if (/^\/api\/artifacts\/[^/]+\/citations$/.test(path)) {
      return json(response, 200, {
        artifact_id: ARTIFACT_ID,
        citations: [
          {
            marker: "P1",
            chunk_id: CHUNK_ID,
            source_id: ARTIFACT_ID,
            source_title: "An Introduction to Functional Programming",
            source_authors: ["Michaelson"],
            page_start: 42,
            page_end: 44,
            section_path: "Chapter 3 › 3.2 Beta Reduction",
            excerpt: "A redex is an application of a lambda abstraction to an argument.",
            excerpt_start_offset: 0,
            excerpt_end_offset: 64,
            source_deleted: false,
          },
        ],
      });
    }

    // Everything else 404s, the way the real app does for an unknown route --
    // which is what makes the "not built yet" surfaces render their own state.
    return json(response, 404, { detail: `no route ${path}` });
  }

  /** Stream a turn as SSE, honouring the interrupt flag mid-flight. */
  async function stream(response: ServerResponse): Promise<void> {
    response.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    });

    const script = options.script ?? DEFAULT_SCRIPT;
    let sent = 0;

    for (const piece of script) {
      if (options.dropAfterChunks !== null && sent >= (options.dropAfterChunks ?? Infinity)) {
        // No terminator: exactly what a dropped connection looks like.
        response.destroy();
        return;
      }

      response.write(frame({ kind: "text", payload: { text: piece } }));
      sent += 1;
      await sleep(options.chunkDelayMs ?? 40);

      // The runtime finishes the current sentence and stops. The script's
      // pieces are sentence-shaped, so stopping after the next one is the same
      // behaviour at this granularity.
      if (interrupted) break;
    }

    if (options.degradeWith) {
      response.write(frame({ kind: "degraded", payload: options.degradeWith }));
    }

    response.write(
      frame({
        kind: "end",
        payload: { turn_id: SESSION_ID, segment_index: 0, anchor: "leftmost-outermost." },
      }),
    );
    response.write("event: done\ndata: {}\n\n");
    response.end();
  }

  await new Promise<void>((resolve) => server.listen(port, "127.0.0.1", resolve));
  const address = server.address();
  const bound = typeof address === "object" && address ? address.port : port;

  return {
    origin: `http://127.0.0.1:${bound}`,
    interrupts,
    turns,
    configure: (next) => {
      options = { ...options, ...next };
    },
    close: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections?.();
        server.close(() => resolve());
      }),
  };
}

function frame(chunk: unknown): string {
  return `data: ${JSON.stringify(chunk)}\n\n`;
}

function json(response: ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  response.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(payload),
  });
  response.end(payload);
}

async function readJson(request: IncomingMessage): Promise<Record<string, unknown> | null> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(chunk as Buffer);
  if (chunks.length === 0) return null;
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    return null;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export const FIXTURES = { SESSION_ID, ARTIFACT_ID, CHUNK_ID };

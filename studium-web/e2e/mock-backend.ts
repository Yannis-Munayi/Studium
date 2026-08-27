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
const ENTRY_ID = "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed";
const CONCEPT_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301";
const LEARNER_ID = "0f8fad5b-d9cb-469f-a165-70867728950e";

/**
 * §9.3's `let_me_try_one`, as the runtime sends it.
 *
 * Note what is *not* here: `model_answer` and `expected_key_points`. The
 * Orchestrator strips them before the chunk leaves the process, and the mock
 * matching that is the point -- a mock that sent the answer key would let a
 * bench that rendered it pass Tier 2.
 */
const PRACTICE_PROBLEM = {
  prompt: "Reduce (\\x. x x) (\\y. y) to normal form, showing each step.",
  difficulty: 2,
  hint: "Start with the outermost redex.",
};

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

  /**
   * The runtime's own state, mirrored well enough for the client to reconcile
   * against it (F9). `let_me_try_one` moves it to LAB and a graded answer moves
   * it back -- which is the sequence §9.3 describes and the one the classroom
   * keys the bench off.
   */
  let runtimeState = "LECTURING";
  /** Attempts on the current problem, so the second one can be graded correct. */
  let labAttempts = 0;
  /** Journal rows, mutable so a PATCH is observable on the next GET. */
  let journal = seedJournal();

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
      runtimeState = "LECTURING";
      labAttempts = 0;
      journal = seedJournal();
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
      const primitive = (body?.["primitive"] as string | null) ?? null;
      turns.push({
        sessionId: turnMatch[1] as string,
        text: String(body?.["text"] ?? ""),
        primitive,
      });
      interrupted = false;

      // §9.3: the one primitive that changes surface.
      if (primitive === "let_me_try_one") {
        runtimeState = "LAB";
        labAttempts = 0;
        return streamPractice(response);
      }
      if (runtimeState === "LAB") return streamGrade(response);
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

    const summaryMatch = /^\/api\/session\/([^/]+)\/summary$/.exec(path);
    if (summaryMatch && request.method === "GET") {
      return json(response, 200, {
        session_id: summaryMatch[1],
        summary: "We worked through beta-reduction and reached normal forms.",
        concepts_touched: [
          {
            concept_id: CONCEPT_ID,
            concept_name: "Beta-reduction",
            before: 0.65,
            after: 0.82,
          },
        ],
        open_threads: ["Confluence is still not settled."],
        next_focus_concept_id: CONCEPT_ID,
        next_focus_concept_name: "The Church-Rosser theorem",
        duration_minutes: 47.5,
        cost_usd: 0.42,
      });
    }

    const stateMatch = /^\/api\/session\/([^/]+)\/state$/.exec(path);
    if (stateMatch) {
      return json(response, 200, {
        session_id: stateMatch[1],
        state: runtimeState,
        persisted_mode: runtimeState === "LAB" ? "lab" : "lecture",
        exchange_index: turns.length,
        interruptible: runtimeState === "LECTURING",
        transitions: [],
      });
    }

    // --- the read surface (frontend §6.1, §6.4, §6.5) ----------------------

    if (path === "/api/user/me/desk" && request.method === "GET") {
      return json(response, 200, {
        learner: {
          id: LEARNER_ID,
          display_name: "Test Learner",
          timezone: "America/Toronto",
          default_session_minutes: 90,
          show_cost: false,
        },
        open_session: null,
        recent_sessions: [
          {
            id: SESSION_ID,
            mode: "lecture",
            started_at: "2026-08-20T14:00:00+00:00",
            ended_at: "2026-08-20T15:00:00+00:00",
            duration_minutes: 60,
            concepts_touched: ["Beta-reduction"],
          },
        ],
        syllabus_next: [
          { id: CONCEPT_ID, name: "The Church-Rosser theorem" },
        ],
        open_journal_entries: journal.filter((e) =>
          ["open", "partial"].includes(e.status),
        ),
        mastery: [
          {
            concept_id: CONCEPT_ID,
            concept_name: "Beta-reduction",
            p_known: 0.82,
            p_known_decayed: 0.74,
          },
        ],
      });
    }

    if (path === "/api/user/me/journal" && request.method === "GET") {
      const wanted = url.searchParams.get("status")?.split(",").filter(Boolean);
      const search = url.searchParams.get("q")?.toLowerCase();
      return json(
        response,
        200,
        journal
          .filter((e) => !wanted?.length || wanted.includes(e.status))
          .filter((e) => !search || e.summary.toLowerCase().includes(search)),
      );
    }

    const entryMatch = /^\/api\/journal\/([^/]+)$/.exec(path);
    if (entryMatch) {
      const entry = journal.find((e) => e.id === entryMatch[1]);
      if (!entry) return json(response, 404, { detail: "no such entry" });

      if (request.method === "PATCH") {
        const patch = (await readJson(request)) ?? {};
        Object.assign(entry, patch);
        entry.last_touched_at = new Date().toISOString();
        if (typeof patch["status"] === "string") {
          entry.history.push({
            id: crypto.randomUUID(),
            kind: statusEvent(patch["status"] as string),
            at: entry.last_touched_at,
            session_id: null,
          });
        }
        if (typeof patch["learner_note"] === "string") {
          entry.history.push({
            id: crypto.randomUUID(),
            kind: "learner_note_added",
            at: entry.last_touched_at,
            session_id: null,
          });
        }
        const { history: _history, ...withoutHistory } = entry;
        return json(response, 200, withoutHistory);
      }

      return json(response, 200, entry);
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
        payload: {
          turn_id: SESSION_ID,
          segment_index: 0,
          anchor: "leftmost-outermost.",
          // The field that makes §10's hover cards resolvable. The runtime
          // sends it once the segment's effects have been applied (SD5); a
          // mock without it would let a frontend that ignored it pass.
          artifact_id: ARTIFACT_ID,
        },
      }),
    );
    response.write("event: done\ndata: {}\n\n");
    response.end();
  }

  /** §9.3's `let_me_try_one`: the Curator's problem, then LAB. */
  async function streamPractice(response: ServerResponse): Promise<void> {
    sseHead(response);
    response.write(frame({ kind: "text", payload: { text: PRACTICE_PROBLEM.prompt } }));
    await sleep(options.chunkDelayMs ?? 40);
    response.write(
      frame({
        kind: "end",
        payload: {
          turn_id: SESSION_ID,
          primitive: "let_me_try_one",
          next_state: "LAB",
          problem: PRACTICE_PROBLEM,
        },
      }),
    );
    response.write("event: done\ndata: {}\n\n");
    response.end();
  }

  /**
   * A graded attempt (§12).
   *
   * The first attempt is wrong and stays in LAB, the second is correct and
   * leaves it -- which exercises both halves of §7's answer_submitted branch,
   * and in particular that the problem survives a wrong answer and does not
   * survive a right one.
   */
  async function streamGrade(response: ServerResponse): Promise<void> {
    sseHead(response);
    labAttempts += 1;
    const correct = labAttempts > 1;

    response.write(
      frame({
        kind: "text",
        payload: {
          text: correct
            ? "That is the normal form, and your substitution step is right."
            : "You substituted into the wrong redex — look at which one is outermost.",
        },
      }),
    );
    await sleep(options.chunkDelayMs ?? 40);

    if (correct) runtimeState = "TUTORIAL";
    response.write(
      frame({
        kind: "end",
        payload: {
          turn_id: SESSION_ID,
          verdict: correct ? "correct" : "incorrect",
          next_state: correct ? "TUTORIAL" : "LAB",
        },
      }),
    );
    response.write("event: done\ndata: {}\n\n");
    response.end();
  }

  function sseHead(response: ServerResponse): void {
    response.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    });
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

interface MockJournalEntry {
  id: string;
  learner_subject_id: string;
  concept_id: string | null;
  concept_name: string | null;
  status: string;
  summary: string;
  summary_author: string;
  /** Always null: the Tracker's inference is not served to a learner (F16). */
  hypothesis: string | null;
  learner_note: string;
  first_seen_at: string;
  last_touched_at: string;
  history: Array<{ id: string; kind: string; at: string; session_id: string | null }>;
}

/** Rebuilt per test, so a PATCH in one case is invisible to the next. */
function seedJournal(): MockJournalEntry[] {
  return [
    {
      id: ENTRY_ID,
      learner_subject_id: CONCEPT_ID,
      concept_id: CONCEPT_ID,
      concept_name: "Beta-reduction",
      status: "open",
      summary: "Treats reduction order as significant for the result.",
      summary_author: "tutor",
      hypothesis: null,
      learner_note: "",
      first_seen_at: "2026-08-19T10:00:00+00:00",
      last_touched_at: "2026-08-20T15:00:00+00:00",
      history: [
        {
          id: "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
          kind: "created",
          at: "2026-08-19T10:00:00+00:00",
          session_id: SESSION_ID,
        },
      ],
    },
  ];
}

function statusEvent(status: string): string {
  return (
    {
      open: "reopened",
      partial: "partially_addressed",
      resolved: "resolved",
      archived: "archived",
    }[status] ?? "revisited"
  );
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

export const FIXTURES = {
  SESSION_ID,
  ARTIFACT_ID,
  CHUNK_ID,
  ENTRY_ID,
  CONCEPT_ID,
  LEARNER_ID,
  PRACTICE_PROBLEM,
};

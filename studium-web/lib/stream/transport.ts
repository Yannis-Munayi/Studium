/**
 * SSE transport (spec §4 "Streaming", §8.1).
 *
 * **This does not use `EventSource`, and cannot.** §4 chooses the native
 * browser API and §8.1 has it opening `GET /api/session/{id}/stream`. The
 * shipped runtime streams from `POST /api/session/{id}/turn`, with the
 * learner's utterance in the request body -- `EventSource` issues a GET, cannot
 * carry a body, and cannot set headers. There is no arrangement of the two that
 * works. See DIVERGENCES-FRONTEND.md F1.
 *
 * So the reader is `fetch` plus a `ReadableStream`, which costs us the one
 * thing `EventSource` gives away free: automatic reconnection. That is
 * hand-rolled in `useSessionStream`, and it has to be, because §8.1's resume
 * ("send a resume request with the last received `turn_index`") has no endpoint
 * behind it either -- see F6.
 */
import { streamChunk, type StreamChunk } from "@/lib/api/schemas";
import { ApiError, errorFromResponse } from "@/lib/api/errors";
import { backendUrl } from "@/lib/api/client";
import { SseParser, isTerminator } from "./parse";

export interface TurnRequest {
  text?: string;
  /** An explicit primitive button press; skips intent classification (§9.2). */
  primitive?: string | null;
}

export interface TurnStreamOptions {
  signal?: AbortSignal;
  /** Injection seam for tests; defaults to the global. */
  fetchImpl?: typeof fetch;
}

/**
 * Thrown when the connection dies before the terminator frame arrives.
 *
 * Distinct from `ApiError("network")` on purpose: a mid-stream drop has
 * partial output already on screen and is resumable, while a failure to
 * connect has neither. §15.3 shows the learner different copy for each.
 */
export class StreamInterruptedError extends Error {
  readonly deliveredChunks: number;

  constructor(deliveredChunks: number, cause?: unknown) {
    super("The stream ended before the server signalled completion.", { cause });
    this.name = "StreamInterruptedError";
    this.deliveredChunks = deliveredChunks;
  }
}

/**
 * Open one turn and yield its chunks.
 *
 * Completes normally only on the `event: done` terminator. Anything else --
 * the body ending, the socket closing -- throws `StreamInterruptedError`, so
 * "the tutor finished" and "the connection died" can never be confused. Without
 * the terminator they are the same observation: bytes stopped arriving.
 */
export async function* readTurnStream(
  sessionId: string,
  body: TurnRequest,
  options: TurnStreamOptions = {},
): AsyncGenerator<StreamChunk, void, undefined> {
  const doFetch = options.fetchImpl ?? fetch;

  let response: Response;
  try {
    response = await doFetch(backendUrl(`/api/session/${sessionId}/turn`), {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ text: body.text ?? "", primitive: body.primitive ?? null }),
      ...(options.signal ? { signal: options.signal } : {}),
    });
  } catch (cause) {
    if (isAbort(cause)) return;
    throw new ApiError("network", "Could not reach the tutor.", { cause });
  }

  if (!response.ok) throw await errorFromResponse(response);
  if (!response.body) {
    throw new ApiError("malformed_response", "The server sent no stream body.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  let delivered = 0;
  let sawTerminator = false;

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      // `stream: true` matters: a multi-byte character split across two network
      // reads decodes to U+FFFD without it, and lecture prose is full of the
      // em-dashes and Greek letters that would land on.
      for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
        if (isTerminator(frame)) {
          sawTerminator = true;
          return;
        }
        const chunk = toChunk(frame.data);
        if (chunk) {
          delivered += 1;
          yield chunk;
        }
      }
    }

    const trailing = parser.flush();
    if (trailing) {
      if (isTerminator(trailing)) {
        sawTerminator = true;
        return;
      }
      const chunk = toChunk(trailing.data);
      if (chunk) {
        delivered += 1;
        yield chunk;
      }
    }
  } catch (cause) {
    if (isAbort(cause)) return;
    throw new StreamInterruptedError(delivered, cause);
  } finally {
    // A consumer that stops early (component unmount, `break`) lands here with
    // the socket still open. Releasing the lock and cancelling is what stops
    // the server generating tokens nobody will read -- and those tokens cost
    // money, so leaking this is a billing bug, not just a resource one.
    reader.cancel().catch(() => {});
    reader.releaseLock();
  }

  if (!sawTerminator) throw new StreamInterruptedError(delivered);
}

function toChunk(data: string): StreamChunk | null {
  if (!data.trim()) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    // One malformed frame is not worth killing a lecture over. The stream keeps
    // its shape and the learner loses at most one token's worth of text.
    console.warn("[stream] discarded unparseable SSE frame");
    return null;
  }
  const result = streamChunk.safeParse(parsed);
  if (!result.success) {
    console.warn("[stream] discarded chunk with unknown shape", result.error.issues);
    return null;
  }
  return result.data;
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

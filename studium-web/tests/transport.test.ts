import { describe, expect, it, vi } from "vitest";
import { readTurnStream, StreamInterruptedError } from "@/lib/stream/transport";
import { ApiError } from "@/lib/api/errors";
import { DeltaBuffer } from "@/lib/stream/buffer";

/**
 * The SSE transport and the render batcher (spec §8.1).
 *
 * The transport is the piece that had to be rewritten to reach the shipped
 * backend at all (F1), so it gets the most attention: what it yields, what it
 * throws, and -- the one that costs money if wrong -- whether it stops the
 * server generating tokens when the consumer walks away.
 */

const SESSION = "0f8fad5b-d9cb-469f-a165-70867728950e";

/** A Response whose body streams the given SSE text in the given slices. */
function sseResponse(slices: string[], init: ResponseInit = {}): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const slice of slices) controller.enqueue(encoder.encode(slice));
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
    ...init,
  });
}

function frame(chunk: unknown): string {
  return `data: ${JSON.stringify(chunk)}\n\n`;
}

const DONE = "event: done\ndata: {}\n\n";

async function collect(stream: AsyncGenerator<unknown>): Promise<unknown[]> {
  const out: unknown[] = [];
  for await (const chunk of stream) out.push(chunk);
  return out;
}

describe("readTurnStream", () => {
  it("yields every chunk up to the terminator", async () => {
    const fetchImpl = vi.fn(async () =>
      sseResponse([
        frame({ kind: "text", payload: { text: "Beta " } }),
        frame({ kind: "text", payload: { text: "reduction." } }),
        frame({ kind: "end", payload: { segment_index: 0 } }),
        DONE,
      ]),
    );

    const chunks = await collect(readTurnStream(SESSION, { text: "" }, { fetchImpl }));

    expect(chunks).toHaveLength(3);
    expect(chunks[0]).toEqual({ kind: "text", payload: { text: "Beta " } });
    expect(chunks[2]).toMatchObject({ kind: "end" });
  });

  it("POSTs the turn body, which is why EventSource cannot be used", async () => {
    const fetchImpl = vi.fn(async () => sseResponse([DONE]));
    await collect(
      readTurnStream(SESSION, { text: "why?", primitive: "prove_it_to_me" }, { fetchImpl }),
    );

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/backend/api/session/${SESSION}/turn`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ text: "why?", primitive: "prove_it_to_me" });
  });

  it("reassembles a chunk split across network reads", async () => {
    const whole = frame({ kind: "text", payload: { text: "split" } });
    const fetchImpl = vi.fn(async () =>
      sseResponse([whole.slice(0, 12), whole.slice(12), DONE]),
    );

    const chunks = await collect(readTurnStream(SESSION, {}, { fetchImpl }));
    expect(chunks).toEqual([{ kind: "text", payload: { text: "split" } }]);
  });

  it("decodes a multi-byte character split across network reads", async () => {
    // Lecture prose is full of em-dashes and Greek letters. Without a streaming
    // decoder each one that lands on a read boundary becomes U+FFFD.
    const text = frame({ kind: "text", payload: { text: "λx — x" } });
    const bytes = new TextEncoder().encode(text);
    const cut = 30; // lands mid-sequence for at least one character

    const fetchImpl = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(bytes.slice(0, cut));
              controller.enqueue(bytes.slice(cut));
              controller.enqueue(new TextEncoder().encode(DONE));
              controller.close();
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    const chunks = (await collect(readTurnStream(SESSION, {}, { fetchImpl }))) as Array<{
      payload: { text: string };
    }>;
    expect(chunks[0]?.payload.text).toBe("λx — x");
    expect(chunks[0]?.payload.text).not.toContain("�");
  });

  it("throws StreamInterruptedError when the body ends without a terminator", async () => {
    // Without this, "the tutor finished" and "the connection died" are the same
    // observation: bytes stopped arriving.
    const fetchImpl = vi.fn(async () =>
      sseResponse([frame({ kind: "text", payload: { text: "cut off mid-" } })]),
    );

    await expect(collect(readTurnStream(SESSION, {}, { fetchImpl }))).rejects.toBeInstanceOf(
      StreamInterruptedError,
    );
  });

  it("reports how much arrived before a drop", async () => {
    const fetchImpl = vi.fn(async () =>
      sseResponse([
        frame({ kind: "text", payload: { text: "one" } }),
        frame({ kind: "text", payload: { text: "two" } }),
      ]),
    );

    try {
      await collect(readTurnStream(SESSION, {}, { fetchImpl }));
      expect.unreachable("should have thrown");
    } catch (error) {
      expect(error).toBeInstanceOf(StreamInterruptedError);
      expect((error as StreamInterruptedError).deliveredChunks).toBe(2);
    }
  });

  it("raises the budget error from a 402 rather than treating it as a drop", async () => {
    const fetchImpl = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            detail: {
              message: "You've reached your daily usage limit.",
              scope: "daily",
              reset_at: "2026-08-21T04:00:00+00:00",
            },
          }),
          { status: 402, headers: { "Content-Type": "application/json" } },
        ),
    );

    try {
      await collect(readTurnStream(SESSION, {}, { fetchImpl }));
      expect.unreachable("should have thrown");
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).kind).toBe("budget_exceeded");
      // A cap resets on a clock; retrying into it just flickers the message.
      expect((error as ApiError).retryable).toBe(false);
      expect((error as ApiError).budget?.scope).toBe("daily");
    }
  });

  it("discards a malformed frame instead of killing the lecture", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const fetchImpl = vi.fn(async () =>
      sseResponse([
        "data: {not json at all}\n\n",
        frame({ kind: "text", payload: { text: "survived" } }),
        DONE,
      ]),
    );

    const chunks = await collect(readTurnStream(SESSION, {}, { fetchImpl }));
    expect(chunks).toEqual([{ kind: "text", payload: { text: "survived" } }]);
    expect(warn).toHaveBeenCalled();
  });

  it("discards a chunk whose shape is unknown", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const fetchImpl = vi.fn(async () =>
      sseResponse([frame({ kind: "telepathy", payload: {} }), DONE]),
    );

    expect(await collect(readTurnStream(SESSION, {}, { fetchImpl }))).toEqual([]);
    expect(warn).toHaveBeenCalled();
  });

  it("cancels the body when the consumer stops early", async () => {
    // The consumer walking away has to stop the server generating tokens.
    // Leaking this is a billing bug before it is a resource one.
    let cancelled = false;
    const encoder = new TextEncoder();
    const fetchImpl = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(encoder.encode(frame({ kind: "text", payload: { text: "a" } })));
              controller.enqueue(encoder.encode(frame({ kind: "text", payload: { text: "b" } })));
            },
            cancel() {
              cancelled = true;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    const stream = readTurnStream(SESSION, {}, { fetchImpl });
    await stream.next();
    await stream.return(undefined); // the `break` in a for-await

    expect(cancelled).toBe(true);
  });

  it("treats a network failure as an ApiError, not a stream drop", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });

    await expect(collect(readTurnStream(SESSION, {}, { fetchImpl }))).rejects.toBeInstanceOf(
      ApiError,
    );
  });
});

describe("DeltaBuffer", () => {
  it("flushes the first delta immediately", async () => {
    // The first token of a segment must not wait out the coalescing window.
    const flushed: string[] = [];
    const buffer = new DeltaBuffer((text) => flushed.push(text), 50);

    buffer.push("first");
    expect(flushed).toEqual(["first"]);
    buffer.dispose();
  });

  it("coalesces deltas that arrive inside the window", () => {
    vi.useFakeTimers();
    const flushed: string[] = [];
    let now = 0;
    const buffer = new DeltaBuffer((text) => flushed.push(text), 50, () => now);

    buffer.push("a"); // immediate
    now = 10;
    buffer.push("b");
    now = 20;
    buffer.push("c");
    expect(flushed).toEqual(["a"]);

    now = 50;
    vi.advanceTimersByTime(50);
    expect(flushed).toEqual(["a", "bc"]);

    buffer.dispose();
    vi.useRealTimers();
  });

  it("drains held text on demand", () => {
    // Called on the `end` chunk -- without it the last tokens of every segment
    // sit in the buffer waiting on a timer with nothing left to coalesce.
    vi.useFakeTimers();
    const flushed: string[] = [];
    let now = 0;
    const buffer = new DeltaBuffer((text) => flushed.push(text), 50, () => now);

    buffer.push("a");
    now = 5;
    buffer.push("tail");
    buffer.drain();

    expect(flushed).toEqual(["a", "tail"]);
    buffer.dispose();
    vi.useRealTimers();
  });

  it("does not flush an empty buffer", () => {
    const flushed: string[] = [];
    const buffer = new DeltaBuffer((text) => flushed.push(text), 50);
    buffer.push("");
    buffer.drain();
    expect(flushed).toEqual([]);
    buffer.dispose();
  });

  it("drops pending text and its timer on dispose", () => {
    vi.useFakeTimers();
    const flushed: string[] = [];
    let now = 0;
    const buffer = new DeltaBuffer((text) => flushed.push(text), 50, () => now);

    buffer.push("a");
    now = 5;
    buffer.push("orphan");
    buffer.dispose();

    vi.advanceTimersByTime(200);
    expect(flushed).toEqual(["a"]);
    vi.useRealTimers();
  });
});

/**
 * Incremental SSE frame parsing (spec §8.1 step 2).
 *
 * Written as a pure push-parser rather than as part of the hook, because the
 * thing most likely to be wrong here -- a frame split across two network reads
 * -- is invisible in a browser and trivial to assert in a unit test. The hook
 * owns the transport; this owns the grammar.
 *
 * The grammar is the subset of the SSE spec the runtime actually emits
 * (`orchestration/streaming.py::sse_event`): `data:` lines and one
 * `event: done` terminator. Comment lines (`:`) and `id:`/`retry:` fields are
 * accepted and ignored so a future keep-alive heartbeat does not break the
 * parser.
 */

export interface SseFrame {
  /** The `event:` field, or `"message"` when absent, per the SSE default. */
  event: string;
  /** Concatenated `data:` lines, newline-joined, per the SSE spec. */
  data: string;
}

/**
 * Feed bytes in, get whole frames out.
 *
 * Holds the tail of an incomplete frame between calls. `flush()` reports
 * whether anything is still held when the stream ends -- a non-empty tail means
 * the connection died mid-frame, which the consumer treats as a drop rather
 * than a clean end.
 */
export class SseParser {
  private buffer = "";

  push(chunk: string): SseFrame[] {
    // Normalise line endings first: an SSE frame separator is a blank line, and
    // "\r\n\r\n" would otherwise leave a stray "\r" heading the next frame.
    this.buffer += chunk.replace(/\r\n/g, "\n").replace(/\r/g, "\n");

    const frames: SseFrame[] = [];
    let separator = this.buffer.indexOf("\n\n");
    while (separator !== -1) {
      const raw = this.buffer.slice(0, separator);
      this.buffer = this.buffer.slice(separator + 2);
      const frame = parseFrame(raw);
      if (frame) frames.push(frame);
      separator = this.buffer.indexOf("\n\n");
    }
    return frames;
  }

  /** Bytes held back because no frame terminator has arrived yet. */
  get pending(): string {
    return this.buffer;
  }

  /**
   * End of stream. Returns a trailing frame if the final one arrived without
   * its blank line -- some proxies drop it on close -- and clears state.
   */
  flush(): SseFrame | null {
    const raw = this.buffer;
    this.buffer = "";
    return raw.trim() ? parseFrame(raw) : null;
  }
}

function parseFrame(raw: string): SseFrame | null {
  let event = "message";
  const data: string[] = [];

  for (const line of raw.split("\n")) {
    if (line === "" || line.startsWith(":")) continue; // blank or comment

    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    // "Optional single leading space after the colon" -- SSE spec.
    const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");

    if (field === "data") data.push(value);
    else if (field === "event") event = value;
    // `id` and `retry` are accepted and ignored: the runtime does not send
    // them, and reconnection here is by turn replay, not by Last-Event-ID.
  }

  if (data.length === 0 && event === "message") return null;
  return { event, data: data.join("\n") };
}

/** The runtime's terminator frame (`event: done`). */
export function isTerminator(frame: SseFrame): boolean {
  return frame.event === "done";
}

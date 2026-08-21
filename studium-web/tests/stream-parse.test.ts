import { describe, expect, it } from "vitest";
import { SseParser, isTerminator } from "@/lib/stream/parse";

/**
 * §17 Tier 1: "Streaming parser: given SSE event fixtures, chunks dispatch
 * correctly."
 *
 * The cases that matter are the ones a browser makes invisible: a frame split
 * across two network reads, a multi-byte character split across two reads, and
 * a stream that ends without its terminator.
 */
describe("SseParser", () => {
  it("parses a single well-formed frame", () => {
    const parser = new SseParser();
    const frames = parser.push('data: {"kind":"text","payload":{"text":"Hello"}}\n\n');

    expect(frames).toHaveLength(1);
    expect(frames[0]?.event).toBe("message");
    expect(JSON.parse(frames[0]!.data)).toEqual({ kind: "text", payload: { text: "Hello" } });
  });

  it("holds a partial frame until its terminator arrives", () => {
    const parser = new SseParser();

    // Split mid-JSON, which is what a real socket does constantly.
    expect(parser.push('data: {"kind":"te')).toEqual([]);
    expect(parser.push('xt","payload":{"text":"a"}}')).toEqual([]);

    const frames = parser.push("\n\n");
    expect(frames).toHaveLength(1);
    expect(JSON.parse(frames[0]!.data).payload.text).toBe("a");
  });

  it("splits several frames arriving in one read", () => {
    const parser = new SseParser();
    const frames = parser.push(
      'data: {"kind":"text","payload":{"text":"one"}}\n\n' +
        'data: {"kind":"text","payload":{"text":"two"}}\n\n',
    );
    expect(frames).toHaveLength(2);
    expect(JSON.parse(frames[1]!.data).payload.text).toBe("two");
  });

  it("recognises the runtime's done terminator", () => {
    const parser = new SseParser();
    const frames = parser.push("event: done\ndata: {}\n\n");

    expect(frames).toHaveLength(1);
    expect(isTerminator(frames[0]!)).toBe(true);
  });

  it("normalises CRLF so a proxy that rewrites line endings still parses", () => {
    const parser = new SseParser();
    const frames = parser.push('data: {"kind":"end","payload":{}}\r\n\r\n');

    expect(frames).toHaveLength(1);
    expect(JSON.parse(frames[0]!.data).kind).toBe("end");
  });

  it("joins multi-line data fields with newlines, per the SSE spec", () => {
    const parser = new SseParser();
    const frames = parser.push("data: line one\ndata: line two\n\n");
    expect(frames[0]?.data).toBe("line one\nline two");
  });

  it("ignores comments and unknown fields", () => {
    const parser = new SseParser();
    const frames = parser.push(": keep-alive\nid: 7\nretry: 100\ndata: kept\n\n");

    expect(frames).toHaveLength(1);
    expect(frames[0]?.data).toBe("kept");
  });

  it("reports a frame left dangling when the connection dies mid-stream", () => {
    const parser = new SseParser();
    parser.push('data: {"kind":"text","payload":{"text":"cut"}}');

    expect(parser.pending).not.toBe("");
    const trailing = parser.flush();
    expect(trailing).not.toBeNull();
    expect(JSON.parse(trailing!.data).payload.text).toBe("cut");
    // Flushing clears, so a reused parser does not replay the tail.
    expect(parser.pending).toBe("");
  });

  it("flushes nothing when the stream ended cleanly", () => {
    const parser = new SseParser();
    parser.push("data: complete\n\n");
    expect(parser.flush()).toBeNull();
  });
});

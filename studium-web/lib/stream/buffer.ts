/**
 * Render batching (spec §8.1 "Buffering").
 *
 * Text deltas arrive one token at a time. Re-rendering a Markdown tree per
 * token is the difference between a lecture that reads and one that stutters,
 * so deltas landing within a ~50ms window collapse into one flush.
 *
 * The window is a *coalescing* window, not a delay: the first delta after a
 * quiet period flushes immediately and starts the window, so the learner sees
 * the first token of a segment at once rather than 50ms late. Only the deltas
 * behind it wait, and none waits longer than the window.
 */

/** §8.1's "soft ~50ms window". */
export const BATCH_WINDOW_MS = 50;

type Flush = (text: string) => void;

export class DeltaBuffer {
  private pending = "";
  private timer: ReturnType<typeof setTimeout> | null = null;
  /**
   * Negative infinity, not zero, so "never flushed" is unambiguous.
   *
   * With zero, the elapsed calculation reads `now() - 0`, which is only large
   * because wall-clock epoch millis are large. Against any clock that starts
   * near zero -- a monotonic one, or an injected one in a test -- the first
   * delta of a session would be held for the full window instead of rendering
   * at once, which is precisely the behaviour this class exists to avoid.
   */
  private lastFlushAt = Number.NEGATIVE_INFINITY;

  constructor(
    private readonly flush: Flush,
    private readonly windowMs: number = BATCH_WINDOW_MS,
    private readonly now: () => number = () => Date.now(),
  ) {}

  push(delta: string): void {
    if (!delta) return;
    this.pending += delta;

    const elapsed = this.now() - this.lastFlushAt;
    if (elapsed >= this.windowMs) {
      this.emit();
      return;
    }
    if (this.timer === null) {
      this.timer = setTimeout(() => this.emit(), this.windowMs - elapsed);
    }
  }

  /**
   * Emit whatever is held, immediately.
   *
   * Called on the `end` chunk. Without it the last few tokens of every segment
   * would sit in the buffer until a timer that has nothing left to coalesce --
   * a segment visibly finishing 50ms after it finished.
   */
  drain(): void {
    this.emit();
  }

  dispose(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.pending = "";
  }

  private emit(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    this.lastFlushAt = this.now();
    if (!this.pending) return;
    const text = this.pending;
    this.pending = "";
    this.flush(text);
  }
}

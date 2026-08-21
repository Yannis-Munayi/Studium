import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";
import { createElement, type AnchorHTMLAttributes, type ReactNode } from "react";

/**
 * `next/link` renders an anchor and then updates its own state for prefetching.
 * In jsdom that update lands outside `act`, so every component containing a
 * link prints an act warning that has nothing to do with the component.
 *
 * Replaced with the anchor it renders. Nothing under test depends on Next's
 * client-side navigation -- the accessibility properties being asserted are the
 * anchor's (`href`, accessible name, focusability), and those are unchanged.
 * Real navigation is covered in Playwright, in a browser, where it is real.
 */
vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    ...props
  }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string; children: ReactNode }) =>
    createElement("a", { href, ...props }, children),
}));

/**
 * Test environment setup (spec §17, Tier 1).
 *
 * jsdom is missing several browser APIs this app uses. Each stub below exists
 * because a real component needs it, and each is the *minimum* that keeps the
 * component honest -- a stub that does more than the real API would let a test
 * pass on behaviour the browser does not have.
 */

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

beforeEach(() => {
  // Radix measures elements on open. jsdom reports every box as 0x0, which is
  // harmless here but noisy without this.
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
    Element.prototype.setPointerCapture = () => {};
    Element.prototype.releasePointerCapture = () => {};
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }

  // `Transcript` observes a sentinel to decide whether new text is below the
  // fold. jsdom has no layout, so the stub reports "visible" and never fires --
  // which is the correct default: it means the tests never see a "New content"
  // button they did not deliberately arrange.
  if (!("IntersectionObserver" in globalThis)) {
    class StubIntersectionObserver implements IntersectionObserver {
      readonly root = null;
      readonly rootMargin = "";
      readonly thresholds: readonly number[] = [];
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
      takeRecords(): IntersectionObserverEntry[] {
        return [];
      }
    }
    Object.defineProperty(globalThis, "IntersectionObserver", {
      writable: true,
      value: StubIntersectionObserver,
    });
  }

  if (!globalThis.matchMedia) {
    Object.defineProperty(globalThis, "matchMedia", {
      writable: true,
      value: (query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }),
    });
  }

  // `requestAnimationFrame` drives the announcer's clear-and-reset. jsdom has
  // it, but not always with a timing that flushes inside a test tick.
  if (!globalThis.requestAnimationFrame) {
    globalThis.requestAnimationFrame = ((callback: FrameRequestCallback) =>
      setTimeout(() => callback(0), 0) as unknown as number) as typeof requestAnimationFrame;
    globalThis.cancelAnimationFrame = ((handle: number) =>
      clearTimeout(handle)) as typeof cancelAnimationFrame;
  }
});

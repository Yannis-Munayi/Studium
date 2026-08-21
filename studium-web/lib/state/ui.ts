/**
 * UI preferences (spec §14.2, `useUIStore`).
 *
 * Persisted to localStorage, never to the backend -- §3's rule that no client
 * state is persisted server-side. These are all things that should survive a
 * reload and none of which another device needs to agree about.
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";

export type ThemePreference = "system" | "light" | "dark";

interface UIState {
  /**
   * §19 open question 4, answered: the system preference is honoured by
   * default and can be overridden.
   *
   * Trusting the system alone is the cleaner-looking choice and the wrong one
   * here. `prefers-color-scheme` follows a schedule; a learner reading at 11pm
   * with a bright desk lamp is a case the schedule gets backwards, and the cost
   * of being wrong is eye strain during exactly the long reading sessions this
   * product is for. An override is three lines and removes the failure mode.
   */
  theme: ThemePreference;
  /** §16.1's scale, as a multiplier on the 16px base. */
  fontScale: number;
  /** §6.2's cost indicator, off by default. */
  showCost: boolean;
  /** §8.2: auto-scroll is a preference and its default is off. */
  autoScroll: boolean;
  /** §19 open question 3's anticipated focus mode. */
  focusMode: boolean;

  setTheme: (theme: ThemePreference) => void;
  setFontScale: (scale: number) => void;
  toggleCost: () => void;
  toggleAutoScroll: () => void;
  toggleFocusMode: () => void;
}

/** §16.1's modular scale, expressed as multipliers of the 16px base. */
export const FONT_SCALES = [0.875, 1, 1.125, 1.25] as const;

export const useUIStore = create<UIState>()(
  persist(
    (set) => ({
      theme: "system",
      fontScale: 1,
      showCost: false,
      autoScroll: false,
      focusMode: false,

      setTheme: (theme) => set({ theme }),
      setFontScale: (fontScale) => set({ fontScale }),
      toggleCost: () => set((s) => ({ showCost: !s.showCost })),
      toggleAutoScroll: () => set((s) => ({ autoScroll: !s.autoScroll })),
      toggleFocusMode: () => set((s) => ({ focusMode: !s.focusMode })),
    }),
    {
      name: "studium-ui",
      version: 1,
    },
  ),
);

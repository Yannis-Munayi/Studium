"use client";

import { FONT_SCALES, useUIStore, type ThemePreference } from "@/lib/state/ui";
import { cn } from "@/lib/cn";

/**
 * Preferences (spec §5.1, §14.2).
 *
 * Everything here is client state persisted to localStorage -- §3's rule that
 * no client state is written to the backend. That has a consequence worth
 * naming on the page rather than hiding: these do not follow the learner to
 * another machine.
 */
export function SettingsSurface() {
  const { theme, fontScale, showCost, autoScroll, focusMode } = useUIStore();
  const { setTheme, setFontScale, toggleCost, toggleAutoScroll, toggleFocusMode } = useUIStore();

  return (
    <div className="mx-auto max-w-2xl px-normal py-loose">
      <h1 className="font-sans text-2xl font-semibold leading-[var(--leading-heading)]">Settings</h1>

      <section className="mt-loose" aria-labelledby="appearance-heading">
        <h2 id="appearance-heading" className="font-sans text-sm font-semibold uppercase tracking-wide text-muted">
          Appearance
        </h2>

        <fieldset className="mt-normal border-0 p-0">
          <legend className="font-sans text-sm text-ink">Theme</legend>
          {/* §19 open question 4: the system preference is the default, and it
              can be overridden. See the note in lib/state/ui.ts. */}
          <div className="mt-tight flex gap-tight">
            {(["system", "light", "dark"] as ThemePreference[]).map((option) => (
              <label
                key={option}
                className={cn(
                  "cursor-pointer rounded border px-3 py-2 font-sans text-sm capitalize",
                  theme === option ? "border-[var(--color-accent)] text-ink" : "border-line text-muted",
                )}
              >
                <input
                  type="radio"
                  name="theme"
                  value={option}
                  className="sr-only"
                  checked={theme === option}
                  onChange={() => setTheme(option)}
                />
                {option}
              </label>
            ))}
          </div>
        </fieldset>

        <div className="mt-normal">
          <label htmlFor="font-scale" className="font-sans text-sm text-ink">
            Text size
          </label>
          <select
            id="font-scale"
            value={fontScale}
            onChange={(event) => setFontScale(Number(event.target.value))}
            className="mt-tight rounded border border-line bg-background px-3 py-2 font-sans text-sm text-ink"
          >
            {FONT_SCALES.map((scale) => (
              <option key={scale} value={scale}>
                {Math.round(scale * 100)}%
              </option>
            ))}
          </select>
        </div>
      </section>

      <section className="mt-loose" aria-labelledby="reading-heading">
        <h2 id="reading-heading" className="font-sans text-sm font-semibold uppercase tracking-wide text-muted">
          Reading
        </h2>

        <Toggle
          id="auto-scroll"
          label="Follow the text as it arrives"
          // §8.2: off by default, because scrolling the page under someone who
          // is reading is worse than making them press a button.
          description="Off by default. When off, new text waits below and a button takes you to it."
          checked={autoScroll}
          onChange={toggleAutoScroll}
        />

        <Toggle
          id="focus-mode"
          label="Hide controls while reading"
          description="The interrupt button and palette fade until you move the pointer or press Tab."
          checked={focusMode}
          onChange={toggleFocusMode}
        />

        <Toggle
          id="show-cost"
          label="Show what a session costs"
          description="Displays running cost in the session. Off by default."
          checked={showCost}
          onChange={toggleCost}
        />
      </section>

      <p className="mt-generous font-sans text-xs text-muted">
        These are stored in this browser, not on your account, so they don&apos;t follow you to
        another machine.
      </p>
    </div>
  );
}

function Toggle({
  id,
  label,
  description,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  description: string;
  checked: boolean;
  onChange: () => void;
}) {
  return (
    <div className="mt-normal flex items-start gap-tight">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={onChange}
        aria-describedby={`${id}-description`}
        className="mt-1"
      />
      <div>
        <label htmlFor={id} className="font-sans text-sm text-ink">
          {label}
        </label>
        <p id={`${id}-description`} className="font-sans text-xs text-muted">
          {description}
        </p>
      </div>
    </div>
  );
}

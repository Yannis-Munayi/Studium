"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { HELP } from "@/lib/copy/surfaces";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { HelpIcon, JournalIcon, LectureIcon, SettingsIcon, denseIconProps } from "@/components/ui/icons";
import { isTypingContext } from "@/components/session/interrupt-button";

/**
 * App navigation and the help overlay (spec §5.1, §13.2, §13.3).
 *
 * §13.3 (WCAG 3.2.6, "Consistent help") requires help to sit in the same place
 * on every surface, so the `?` trigger lives in this chrome and the session
 * layout -- which hides most chrome -- keeps it.
 */

const NAV = [
  { href: "/", label: "Desk", Icon: LectureIcon },
  { href: "/journal", label: "Journal", Icon: JournalIcon },
  { href: "/settings", label: "Settings", Icon: SettingsIcon },
] as const;

export function AppChrome({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="flex min-h-dvh flex-col">
      <SkipLink />

      <nav aria-label="Main" className="border-b border-line">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-normal px-normal py-3">
          <Link href="/" className="font-serif text-lg font-semibold text-ink">
            Studium
          </Link>

          <ul className="flex items-center gap-tight">
            {NAV.map(({ href, label, Icon }) => {
              const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
              return (
                <li key={href}>
                  <Link
                    href={href}
                    // §13.4: the current page is announced, not just underlined.
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "inline-flex items-center gap-tight rounded px-3 py-2 font-sans text-sm",
                      "transition-colors duration-[var(--duration-hover)]",
                      active ? "text-ink" : "text-muted hover:text-ink",
                    )}
                  >
                    <Icon {...denseIconProps} />
                    {label}
                  </Link>
                </li>
              );
            })}
            <li>
              <HelpOverlay />
            </li>
          </ul>
        </div>
      </nav>

      <main id="main" className="flex-1">
        {children}
      </main>
    </div>
  );
}

/**
 * §13.2: the first tab stop, so a keyboard user is not walked through the whole
 * navigation on every page before reaching the content.
 */
export function SkipLink() {
  return (
    <a
      href="#main"
      className={cn(
        "sr-only rounded bg-[var(--color-accent-strong)] px-4 py-2 font-sans text-sm",
        "text-[var(--color-on-accent)]",
        "focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50",
      )}
    >
      Skip to content
    </a>
  );
}

/** §13.2's `?` shortcut reference, reachable from every surface. */
export function HelpOverlay() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    function handler(event: KeyboardEvent) {
      if (event.key !== "?" || isTypingContext(event.target)) return;
      event.preventDefault();
      setOpen(true);
    }
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  return (
    <>
      <Button variant="quiet" size="sm" onClick={() => setOpen(true)} aria-keyshortcuts="?">
        <HelpIcon {...denseIconProps} />
        <span className="sr-only">{HELP.heading}</span>
      </Button>

      <Dialog open={open} onOpenChange={setOpen} title={HELP.heading}>
        <table className="w-full font-sans text-sm">
          <thead className="sr-only">
            <tr>
              <th scope="col">Keys</th>
              <th scope="col">Action</th>
            </tr>
          </thead>
          <tbody>
            {HELP.shortcuts.map((shortcut) => (
              <tr key={shortcut.keys} className="border-b border-line last:border-b-0">
                <th scope="row" className="py-2 pr-normal text-left font-normal">
                  <kbd className="rounded border border-line px-1.5 py-0.5 font-mono text-xs">
                    {shortcut.keys}
                  </kbd>
                </th>
                <td className="py-2 text-muted">{shortcut.action}</td>
              </tr>
            ))}
          </tbody>
        </table>

        {/* §13.3 (WCAG 3.2.1): the palette's Enter-to-invoke is a documented
            exception to "focus changes do not trigger navigation". */}
        <p className="mt-normal font-sans text-xs text-muted">
          In the command palette, Enter runs the highlighted primitive straight away.
        </p>
      </Dialog>
    </>
  );
}

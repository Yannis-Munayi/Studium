"use client";

import { Slot } from "@radix-ui/react-slot";
import { forwardRef, type ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

/**
 * The one button (spec §16.2, §13.4).
 *
 * Variants are named for what the button *is* in the interface, not for how it
 * looks, so a palette change is one file and a "make this less prominent"
 * request is a variant swap rather than a class edit at the call site.
 */
export type ButtonVariant = "primary" | "secondary" | "quiet" | "danger";
export type ButtonSize = "sm" | "md";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Render as the child element (a link that should look like a button). */
  asChild?: boolean;
}

const base = cn(
  "inline-flex items-center justify-center gap-2 rounded font-sans font-medium",
  "transition-colors duration-[var(--duration-hover)] ease-[var(--ease-studium)]",
  // §13.2: a disabled control still has to be distinguishable, and 50% opacity
  // on a muted palette can fall under 3:1. Colour changes, opacity does not.
  "disabled:cursor-not-allowed disabled:text-muted disabled:border-line",
);

const variants: Record<ButtonVariant, string> = {
  // `accent-strong`, not `accent`: a filled button carries near-white text, and
  // §16.2's accent only reaches 3.22:1 under it. See the note in globals.css.
  primary: cn(
    "bg-[var(--color-accent-strong)] text-[var(--color-on-accent)] border border-transparent",
    "hover:not-disabled:brightness-110 disabled:bg-transparent disabled:border",
  ),
  secondary: cn(
    "bg-transparent text-ink border border-line",
    "hover:not-disabled:bg-surface",
  ),
  quiet: cn(
    "bg-transparent text-muted border border-transparent",
    "hover:not-disabled:text-ink hover:not-disabled:bg-surface",
  ),
  danger: cn(
    "bg-transparent text-[var(--color-concern)] border border-[var(--color-concern)]",
    "hover:not-disabled:bg-[color-mix(in_srgb,var(--color-concern)_12%,transparent)]",
  ),
};

const sizes: Record<ButtonSize, string> = {
  sm: "text-sm px-3 py-1.5 min-h-8",
  // §13.2 (WCAG 2.5.8 target size): 44px is the AAA figure, 24px the AA floor.
  // 36px sits above the floor with room for the focus ring.
  md: "text-sm px-4 py-2 min-h-9",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", asChild = false, className, type, ...props },
  ref,
) {
  const Component = asChild ? Slot : "button";
  return (
    <Component
      ref={ref}
      // A button inside a form with no explicit type submits it. That has
      // silently submitted a form from an "expand hint" control more than once
      // in the history of the web.
      {...(asChild ? {} : { type: type ?? "button" })}
      className={cn(base, variants[variant], sizes[size], className)}
      {...props}
    />
  );
});

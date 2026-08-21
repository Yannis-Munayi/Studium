"use client";

import * as RadixToast from "@radix-ui/react-toast";
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { announce } from "@/lib/a11y/announcer";
import { cn } from "@/lib/cn";
import { WarningIcon, iconProps } from "./icons";

/**
 * Toasts (spec §3 "Optimistic updates", §14.1).
 *
 * These exist for one job: telling the learner that an optimistic update rolled
 * back. That is the only case §3 assigns them, and keeping them to it is
 * deliberate -- a toast for every success is the notification idiom §16.5 rules
 * out, and it trains the learner to ignore the one that matters.
 */

interface ToastMessage {
  id: number;
  title: string;
  body?: string;
}

interface ToastApi {
  /** Report a rollback. Announced assertively as well as shown (§13.4). */
  error: (title: string, body?: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [messages, setMessages] = useState<ToastMessage[]>([]);

  const error = useCallback((title: string, body?: string) => {
    setMessages((prior) => [...prior, { id: Date.now() + prior.length, title, ...(body ? { body } : {}) }]);
    announce(body ? `${title}. ${body}` : title, "assertive");
  }, []);

  const api = useMemo<ToastApi>(() => ({ error }), [error]);

  return (
    <ToastContext.Provider value={api}>
      <RadixToast.Provider swipeDirection="right" duration={8000}>
        {children}
        {messages.map((message) => (
          <RadixToast.Root
            key={message.id}
            onOpenChange={(open) => {
              if (!open) setMessages((prior) => prior.filter((m) => m.id !== message.id));
            }}
            className={cn(
              "flex items-start gap-tight rounded border border-line bg-surface p-normal",
              // §16.4: toasts sliding in is the other named motion exception.
              "data-[state=open]:animate-[slide-in_var(--duration-surface)_var(--ease-studium)]",
            )}
          >
            <span className="mt-0.5 shrink-0 text-[var(--color-concern)]">
              <WarningIcon {...iconProps} />
            </span>
            <div>
              <RadixToast.Title className="font-sans text-sm font-medium">
                {message.title}
              </RadixToast.Title>
              {message.body ? (
                <RadixToast.Description className="mt-1 text-sm text-muted">
                  {message.body}
                </RadixToast.Description>
              ) : null}
            </div>
          </RadixToast.Root>
        ))}
        <RadixToast.Viewport
          className={cn(
            "fixed right-normal top-normal z-50 flex w-[min(24rem,calc(100vw-2rem))]",
            "flex-col gap-tight outline-none",
          )}
        />
      </RadixToast.Provider>
    </ToastContext.Provider>
  );
}

/**
 * Falls back to a no-op outside the provider rather than throwing.
 *
 * A component test that renders one card should not have to mount the whole
 * app shell to satisfy a notification channel it never uses, and a thrown
 * error here would fail those tests for a reason unrelated to what they check.
 */
export function useToast(): ToastApi {
  return useContext(ToastContext) ?? { error: () => {} };
}

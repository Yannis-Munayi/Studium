"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { ApiError } from "@/lib/api/errors";
import { useUIStore } from "@/lib/state/ui";
import { Announcements } from "@/components/ui/announcements";
import { ToastProvider } from "@/components/ui/toast";

/**
 * App-wide providers (spec §4, §14).
 *
 * The QueryClient is built inside state, not at module scope. At module scope
 * it would be shared across every request the server handles, which leaks one
 * learner's cached data into another's response -- the classic Next + TanStack
 * mistake, and one that produces no error at all.
 */
export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // Per-query `staleTime` is set at each hook (§5.3's table). This is
            // the floor for anything that forgets to.
            staleTime: 30_000,
            retry: (failureCount, error) => {
              // A budget stop or a 404 is not going to succeed on a retry, and
              // retrying a 402 turns one calm message into a flicker of it.
              if (error instanceof ApiError && !error.retryable) return false;
              return failureCount < 2;
            },
            refetchOnWindowFocus: false,
          },
          mutations: { retry: false },
        },
      }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <ThemeSync />
        <Announcements />
        {children}
      </ToastProvider>
    </QueryClientProvider>
  );
}

/**
 * Keeps `<html>` in step with the stored preference (§19 open question 4).
 *
 * The inline script in `layout.tsx` handles the first paint; this handles every
 * change after it.
 */
function ThemeSync() {
  const theme = useUIStore((s) => s.theme);
  const fontScale = useUIStore((s) => s.fontScale);

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") delete root.dataset["theme"];
    else root.dataset["theme"] = theme;
  }, [theme]);

  useEffect(() => {
    document.documentElement.style.setProperty("--studium-font-scale", `${fontScale * 100}%`);
  }, [fontScale]);

  return null;
}

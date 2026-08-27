"use client";

/**
 * TanStack Query hooks (spec §14.1).
 *
 * Query keys follow §5.3's nested-tuple convention, and each hook carries the
 * `staleTime` §5.3's table assigns it. The times are here rather than on a
 * global default because they encode *why* -- a mastery snapshot goes stale in
 * 30 seconds because a session is changing it, and a citation never goes stale
 * because an artifact's citations are immutable. A single global number would
 * lose both facts.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchArtifactCitations, fetchSessionState } from "./client";
import {
  fetchDesk,
  fetchJournalEntries,
  fetchJournalEntry,
  fetchSessionSummary,
  resolveJournalEntry,
  updateJournalEntry,
  type JournalFilter,
} from "./surfaces";
import type { JournalEntry, JournalEntryDetail, JournalStatus, UUID } from "./schemas";
import { useToast } from "@/components/ui/toast";
import { JOURNAL } from "@/lib/copy/surfaces";

const MINUTE = 60_000;

/** §5.3: "Artifact citations — 1 hour — Immutable per artifact." */
export function useArtifactCitations(artifactId: UUID | null) {
  return useQuery({
    queryKey: ["artifact", artifactId, "citations"],
    queryFn: () => fetchArtifactCitations(artifactId as UUID),
    enabled: artifactId !== null,
    staleTime: 60 * MINUTE,
  });
}

/** §5.3: "Session turns — 0 (always refetch on view) — Real-time relevance." */
export function useSessionState(sessionId: UUID | null) {
  return useQuery({
    queryKey: ["session", sessionId],
    queryFn: () => fetchSessionState(sessionId as UUID),
    enabled: sessionId !== null,
    staleTime: 0,
  });
}

/**
 * §6.1. Everything the desk shows, in one round trip.
 *
 * `userId` is a signed-in flag, not a parameter: the request carries no user
 * id, because the proxy resolves identity server-side. It stays in the key so
 * that signing in as someone else does not serve the previous learner's desk
 * out of the cache.
 */
export function useDesk(userId: UUID | null) {
  return useQuery({
    queryKey: ["user", userId, "desk"],
    queryFn: () => fetchDesk(),
    enabled: userId !== null,
    // The desk mixes 5-minute data (profile, subjects) with 30-second data
    // (mastery, journal). The shorter one governs -- serving a stale journal
    // for five minutes on the surface whose job is "what is still open" would
    // defeat the surface.
    staleTime: 30_000,
    retry: false,
  });
}

/**
 * §5.3: "Journal entries — 30 seconds."
 *
 * No enrollment argument. §6.4's journal is every subject's entries, and the
 * subject filter is one of the filters — which is also what lets the surface
 * load before anything has told it which enrollment the learner is in.
 */
export function useJournalEntries(filter: JournalFilter = {}) {
  return useQuery({
    queryKey: ["journal", "list", filter],
    queryFn: () => fetchJournalEntries(filter),
    staleTime: 30_000,
    retry: false,
  });
}

export function useJournalEntry(entryId: UUID | null) {
  return useQuery({
    queryKey: ["journal", "entry", entryId],
    queryFn: () => fetchJournalEntry(entryId as UUID),
    enabled: entryId !== null,
    staleTime: 30_000,
    retry: false,
  });
}

/**
 * §14.1's optimistic resolve, with the rollback §3 requires.
 *
 * `cancelQueries` first is not optional: an in-flight refetch that resolves
 * after the optimistic write would overwrite it with the pre-mutation server
 * state, and the entry would visibly un-resolve itself a second later.
 *
 * **The two caches are patched separately, and that is the point.** An earlier
 * version matched `["journal"]` and mapped over whatever it found, which is
 * correct for the list queries and wrong for `["journal", "entry", id]` — that
 * one holds a single object, and calling `.map` on it throws. It never fired
 * while the endpoints did not exist; the moment they answered, resolving an
 * entry *from the detail page* — the only place the resolve button is — would
 * have thrown inside `onMutate`. See DIVERGENCES-FRONTEND.md F19.
 */
export function useResolveJournalEntry() {
  const queryClient = useQueryClient();
  const toast = useToast();

  return useMutation({
    mutationFn: (entryId: UUID) => resolveJournalEntry(entryId),

    onMutate: async (entryId) => {
      await queryClient.cancelQueries({ queryKey: ["journal"] });

      const lists = queryClient.getQueriesData<JournalEntry[]>({
        queryKey: ["journal", "list"],
      });
      const detailKey = ["journal", "entry", entryId] as const;
      const detail = queryClient.getQueryData<JournalEntryDetail>(detailKey);

      queryClient.setQueriesData<JournalEntry[]>({ queryKey: ["journal", "list"] }, (old) =>
        old?.map((entry) =>
          entry.id === entryId ? { ...entry, status: "resolved" as JournalStatus } : entry,
        ),
      );
      if (detail) {
        queryClient.setQueryData<JournalEntryDetail>(detailKey, {
          ...detail,
          status: "resolved",
        });
      }

      return { lists, detailKey, detail };
    },

    onError: (_error, _entryId, context) => {
      context?.lists.forEach(([key, data]) => queryClient.setQueryData(key, data));
      if (context?.detail) queryClient.setQueryData(context.detailKey, context.detail);
      toast.error(JOURNAL.resolveFailed);
    },

    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["journal"] });
    },
  });
}

/**
 * §6.5's close modal.
 *
 * `retry: false` because the expected failure is a 404 meaning "the Curator's
 * summary did not generate", which the close response already reported. Three
 * retries would spend six seconds re-asking a question that has been answered,
 * with the learner watching a dialog that says it is still writing.
 */
export function useSessionSummary(sessionId: UUID | null, enabled = true) {
  return useQuery({
    queryKey: ["session", sessionId, "summary"],
    queryFn: () => fetchSessionSummary(sessionId as UUID),
    enabled: enabled && sessionId !== null,
    // A closed session's summary does not change again unless it is
    // regenerated, which is not something this surface can trigger.
    staleTime: 60 * MINUTE,
    retry: false,
  });
}

/**
 * §6.4's other entry actions: reopen, archive, mark partial.
 *
 * Not optimistic, unlike resolve. §14.1 specifies the optimistic path for the
 * action a learner takes constantly and expects to feel instant; these three
 * are deliberate, occasional, and taken from a surface that is already showing
 * the entry — so a round trip before the pill changes reads as the system
 * having done the thing, not as lag.
 */
export function useSetJournalStatus(entryId: UUID | null) {
  const queryClient = useQueryClient();
  const toast = useToast();

  return useMutation({
    mutationFn: (status: JournalStatus) =>
      updateJournalEntry(entryId as UUID, { status }),
    onError: () => toast.error(JOURNAL.statusFailed),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["journal"] });
    },
  });
}

/**
 * §12.1's autosaved learner note.
 *
 * No optimistic update: the textarea already holds what the learner typed, so
 * there is nothing to show optimistically. What matters is that a failure is
 * visible, because the whole point of an autosave indicator is that the learner
 * stops thinking about saving.
 */
export function useSaveLearnerNote(entryId: UUID | null) {
  const queryClient = useQueryClient();
  const toast = useToast();

  return useMutation({
    mutationFn: (note: string) => updateJournalEntry(entryId as UUID, { learner_note: note }),
    onError: () => toast.error("Couldn't save your note", "Your text is still here — try again."),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["journal", "entry", entryId] });
    },
  });
}

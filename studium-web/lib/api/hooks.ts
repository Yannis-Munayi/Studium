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
  resolveJournalEntry,
  updateJournalEntry,
  type JournalFilter,
} from "./surfaces";
import type { JournalEntry, JournalStatus, UUID } from "./schemas";
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

/** §6.1. Everything the desk shows, in one round trip. */
export function useDesk(userId: UUID | null) {
  return useQuery({
    queryKey: ["user", userId, "desk"],
    queryFn: () => fetchDesk(userId as UUID),
    enabled: userId !== null,
    // The desk mixes 5-minute data (profile, subjects) with 30-second data
    // (mastery, journal). The shorter one governs -- serving a stale journal
    // for five minutes on the surface whose job is "what is still open" would
    // defeat the surface.
    staleTime: 30_000,
    retry: false,
  });
}

/** §5.3: "Journal entries — 30 seconds." */
export function useJournalEntries(learnerSubjectId: UUID | null, filter: JournalFilter = {}) {
  return useQuery({
    queryKey: ["journal", learnerSubjectId, filter],
    queryFn: () => fetchJournalEntries(learnerSubjectId as UUID, filter),
    enabled: learnerSubjectId !== null,
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
 */
export function useResolveJournalEntry() {
  const queryClient = useQueryClient();
  const toast = useToast();

  return useMutation({
    mutationFn: (entryId: UUID) => resolveJournalEntry(entryId),

    onMutate: async (entryId) => {
      await queryClient.cancelQueries({ queryKey: ["journal"] });
      const previous = queryClient.getQueriesData<JournalEntry[]>({ queryKey: ["journal"] });

      queryClient.setQueriesData<JournalEntry[]>({ queryKey: ["journal"] }, (old) =>
        old?.map((entry) =>
          entry.id === entryId ? { ...entry, status: "resolved" as JournalStatus } : entry,
        ),
      );
      return { previous };
    },

    onError: (_error, _entryId, context) => {
      context?.previous.forEach(([key, data]) => queryClient.setQueryData(key, data));
      toast.error(JOURNAL.resolveFailed);
    },

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

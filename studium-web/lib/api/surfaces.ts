/**
 * The desk and journal data paths (spec §6.1, §6.4, §6.5).
 *
 * **These endpoints exist now.** `backend/studium/api/reads.py` serves all five,
 * over the same `journal_entries`, `session_summaries`, `concept_mastery` and
 * `learner_subjects` rows the runtime was already writing. What used to be
 * missing was HTTP, and `UNAVAILABLE` is what stood in for it (F4).
 *
 * `SURFACE_ENDPOINTS` and `UNAVAILABLE` are kept rather than deleted. The set is
 * empty and the guard is a no-op, and both stay because they are the mechanism
 * for the next surface that ships ahead of its server — which §2 promises four
 * more of. A surface that discovers a missing endpoint as a 404 renders "Not
 * found" about a learner's own journal, and that is a lie.
 *
 * **Identity is not a parameter.** The desk and journal read "me": the Next
 * proxy resolves the learner server-side and sets `X-Studium-User`, so no
 * client function can name a different one. That is one place identity can be
 * acquired rather than one per call site — and it is why `fetchDesk` takes no
 * argument at all.
 */
import { ApiError } from "./errors";
import { request } from "./client";
import {
  deskResponse,
  journalEntryDetail,
  journalEntry,
  sessionSummary,
  type DeskResponse,
  type JournalEntry,
  type JournalEntryDetail,
  type JournalStatus,
  type SessionSummary,
  type UUID,
} from "./schemas";
import { z } from "zod";

/** Every backend route these surfaces need. */
export const SURFACE_ENDPOINTS = {
  desk: "GET /api/user/me/desk",
  journalList: "GET /api/user/me/journal",
  journalEntry: "GET /api/journal/{entry_id}",
  journalUpdate: "PATCH /api/journal/{entry_id}",
  sessionSummary: "GET /api/session/{session_id}/summary",
} as const;

export type SurfaceEndpoint = keyof typeof SURFACE_ENDPOINTS;

/**
 * Endpoints not yet served by the backend.
 *
 * Empty as of the build that shipped `studium/api/reads.py`. Adding a name here
 * turns the surface's "not connected yet" state back on without touching the
 * component.
 */
export const UNAVAILABLE: ReadonlySet<SurfaceEndpoint> = new Set([]);

/**
 * A distinct failure kind, so surfaces can say "not built" rather than
 * "not found".
 *
 * `invalid_request` is borrowed as the transport-level kind -- there is no HTTP
 * status for "this endpoint is a subsystem behind" -- but the marker property
 * is what components branch on.
 */
export class SurfaceUnavailableError extends ApiError {
  readonly endpoint: SurfaceEndpoint;

  constructor(endpoint: SurfaceEndpoint) {
    super("invalid_request", `${SURFACE_ENDPOINTS[endpoint]} is not built yet.`);
    this.name = "SurfaceUnavailableError";
    this.endpoint = endpoint;
  }
}

export function isSurfaceUnavailable(error: unknown): error is SurfaceUnavailableError {
  return error instanceof SurfaceUnavailableError;
}

function guard(endpoint: SurfaceEndpoint): void {
  if (UNAVAILABLE.has(endpoint)) throw new SurfaceUnavailableError(endpoint);
}

// --- the desk (§6.1) -------------------------------------------------------

export async function fetchDesk(): Promise<DeskResponse> {
  guard("desk");
  return request("/api/user/me/desk", deskResponse);
}

// --- the journal (§6.4) ----------------------------------------------------

export interface JournalFilter {
  status?: JournalStatus[];
  /**
   * Enrollment ids. Optional, because §6.4 is "all ... entries **across the
   * learner's subjects**" — the subject picker narrows a view that already has
   * something in it, rather than being what makes the view load at all.
   */
  subjectIds?: UUID[];
  conceptIds?: UUID[];
  /** ISO date; §6.4 defaults to the last 30 days. */
  since?: string;
  search?: string;
}

export async function fetchJournalEntries(
  filter: JournalFilter = {},
): Promise<JournalEntry[]> {
  guard("journalList");
  const params = new URLSearchParams();
  if (filter.status?.length) params.set("status", filter.status.join(","));
  if (filter.subjectIds?.length) params.set("learner_subject_id", filter.subjectIds.join(","));
  if (filter.conceptIds?.length) params.set("concept_ids", filter.conceptIds.join(","));
  if (filter.since) params.set("since", filter.since);
  if (filter.search) params.set("q", filter.search);

  const query = params.toString();
  return request(
    `/api/user/me/journal${query ? `?${query}` : ""}`,
    z.array(journalEntry),
  );
}

export async function fetchJournalEntry(entryId: UUID): Promise<JournalEntryDetail> {
  guard("journalEntry");
  return request(`/api/journal/${entryId}`, journalEntryDetail);
}

export async function updateJournalEntry(
  entryId: UUID,
  patch: { status?: JournalStatus; summary?: string; learner_note?: string },
): Promise<JournalEntry> {
  guard("journalUpdate");
  return request(`/api/journal/${entryId}`, journalEntry, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function resolveJournalEntry(entryId: UUID): Promise<JournalEntry> {
  return updateJournalEntry(entryId, { status: "resolved" });
}

// --- session summary (§6.5) ------------------------------------------------

export async function fetchSessionSummary(sessionId: UUID): Promise<SessionSummary> {
  guard("sessionSummary");
  return request(`/api/session/${sessionId}/summary`, sessionSummary);
}

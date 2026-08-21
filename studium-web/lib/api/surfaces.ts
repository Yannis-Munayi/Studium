/**
 * The desk and journal data paths (spec §6.1, §6.4).
 *
 * **These endpoints do not exist yet.** The rows behind them do -- the data
 * layer ships `journal_entries`, `session_summaries`, `concept_mastery`, and
 * `learner_subjects`, and the runtime writes to all four. What is missing is
 * HTTP: `backend/studium/api/app.py` serves the session lifecycle, the
 * interrupt channel, and citation resolution, and nothing else. See
 * DIVERGENCES-FRONTEND.md F4.
 *
 * Rather than let each surface discover that as a 404 and render "Not found"
 * -- which would be a lie, since the learner's journal is neither absent nor
 * forbidden -- the gap is named here. `SURFACE_ENDPOINTS` is the list of what
 * has to exist, at the paths the client already calls. Turning any of them on
 * is deleting one line from `UNAVAILABLE`.
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

/** Every backend route these surfaces need, and whether it is built. */
export const SURFACE_ENDPOINTS = {
  desk: "GET /api/desk",
  journalList: "GET /api/journal",
  journalEntry: "GET /api/journal/{entry_id}",
  journalUpdate: "PATCH /api/journal/{entry_id}",
  sessionSummary: "GET /api/session/{session_id}/summary",
} as const;

export type SurfaceEndpoint = keyof typeof SURFACE_ENDPOINTS;

/**
 * Endpoints not yet served by subsystem 2.
 *
 * Verified against `app.py` at the time of this build. A route that starts
 * answering should be removed from this set; the client call below it is
 * already written against the shape the data layer guarantees.
 */
export const UNAVAILABLE: ReadonlySet<SurfaceEndpoint> = new Set([
  "desk",
  "journalList",
  "journalEntry",
  "journalUpdate",
  "sessionSummary",
]);

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

export async function fetchDesk(userId: UUID): Promise<DeskResponse> {
  guard("desk");
  return request(`/api/desk?user_id=${userId}`, deskResponse);
}

// --- the journal (§6.4) ----------------------------------------------------

export interface JournalFilter {
  status?: JournalStatus[];
  subjectIds?: UUID[];
  conceptIds?: UUID[];
  /** ISO date; §6.4 defaults to the last 30 days. */
  since?: string;
  search?: string;
}

export async function fetchJournalEntries(
  learnerSubjectId: UUID,
  filter: JournalFilter = {},
): Promise<JournalEntry[]> {
  guard("journalList");
  const params = new URLSearchParams({ learner_subject_id: learnerSubjectId });
  if (filter.status?.length) params.set("status", filter.status.join(","));
  if (filter.conceptIds?.length) params.set("concept_ids", filter.conceptIds.join(","));
  if (filter.since) params.set("since", filter.since);
  if (filter.search) params.set("q", filter.search);
  return request(`/api/journal?${params}`, z.array(journalEntry));
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

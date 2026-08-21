/**
 * The HTTP client (spec §5.3).
 *
 * Every request goes through the Next proxy at `/api/backend/*` rather than
 * straight at FastAPI. Two reasons, both structural: the browser never learns
 * the backend origin, and there is exactly one server-side place for a request
 * to acquire identity -- so no client code path can forget to.
 */
import { z } from "zod";
import { ApiError, errorFromResponse } from "./errors";
import {
  artifactCitationsResponse,
  closeResponse,
  interruptResponse,
  sessionStateResponse,
  startSessionResponse,
  type ArtifactCitationsResponse,
  type CloseResponse,
  type InterruptResponse,
  type SessionMode,
  type SessionStateResponse,
  type StartSessionResponse,
  type UUID,
} from "./schemas";

/** Prefix for every backend call. The proxy route strips it. */
export const BACKEND_PREFIX = "/api/backend";

export function backendUrl(path: string): string {
  return `${BACKEND_PREFIX}${path.startsWith("/") ? path : `/${path}`}`;
}

/**
 * Fetch, validate, or throw an `ApiError`.
 *
 * The schema parse is not optional and not `safeParse`-with-a-fallback: a
 * response that does not match its schema is a contract break, and rendering a
 * best-effort partial of it produces a surface that looks fine and is wrong.
 */
export async function request<S extends z.ZodTypeAny>(
  path: string,
  schema: S,
  init: RequestInit = {},
): Promise<z.infer<S>> {
  let response: Response;
  try {
    response = await fetch(backendUrl(path), {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(init.headers ?? {}),
      },
    });
  } catch (cause) {
    throw new ApiError("network", "Could not reach the server.", { cause });
  }

  if (!response.ok) throw await errorFromResponse(response);

  let body: unknown;
  try {
    body = await response.json();
  } catch (cause) {
    throw new ApiError("malformed_response", "The server sent an unreadable response.", { cause });
  }

  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    throw new ApiError(
      "malformed_response",
      `Response did not match its schema: ${parsed.error.issues
        .map((i) => `${i.path.join(".") || "(root)"}: ${i.message}`)
        .join("; ")}`,
      { status: response.status },
    );
  }
  return parsed.data;
}

// --- session lifecycle -----------------------------------------------------

export interface StartSessionInput {
  user_id: UUID;
  mode: SessionMode;
  focus_concept_id?: UUID | null;
  learner_subject_id?: UUID | null;
  target_duration_minutes?: number;
}

export function startSession(input: StartSessionInput): Promise<StartSessionResponse> {
  return request("/api/session", startSessionResponse, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/**
 * Signal an interrupt (§8.3 step 1).
 *
 * The spec expects 202 Accepted; the runtime answers 200 with an `accepted`
 * boolean, and answers it even when nothing was interruptible. That is the
 * better contract for this UI -- "nothing was streaming" is a state the button
 * must render, not an error -- so the client reads the body rather than the
 * status. See DIVERGENCES-FRONTEND.md F5.
 */
export function signalInterrupt(sessionId: UUID): Promise<InterruptResponse> {
  return request(`/api/session/${sessionId}/interrupt`, interruptResponse, { method: "POST" });
}

export function closeSession(sessionId: UUID, reason = "learner_stop"): Promise<CloseResponse> {
  return request(`/api/session/${sessionId}/close`, closeResponse, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export function fetchSessionState(sessionId: UUID): Promise<SessionStateResponse> {
  return request(`/api/session/${sessionId}/state`, sessionStateResponse);
}

// --- citations -------------------------------------------------------------

export function fetchArtifactCitations(artifactId: UUID): Promise<ArtifactCitationsResponse> {
  return request(`/api/artifacts/${artifactId}/citations`, artifactCitationsResponse);
}

/**
 * API failure classification (spec §15.2).
 *
 * One class per thing the learner can be told, because §3 requires the message
 * to name what happened and what to do next. A single `ApiError` carrying a
 * status code would push that decision into every call site, and the call sites
 * would each get it slightly differently.
 */
import { budgetExceededDetail, type BudgetExceededDetail } from "./schemas";

export type FailureKind =
  | "budget_exceeded"
  | "rate_limited"
  | "not_found"
  | "forbidden"
  | "invalid_request"
  | "server_error"
  | "network"
  | "malformed_response";

export class ApiError extends Error {
  readonly kind: FailureKind;
  readonly status: number;
  /** Populated only for `budget_exceeded`. */
  readonly budget?: BudgetExceededDetail;

  constructor(
    kind: FailureKind,
    message: string,
    options: { status?: number; budget?: BudgetExceededDetail; cause?: unknown } = {},
  ) {
    super(message, options.cause !== undefined ? { cause: options.cause } : undefined);
    this.name = "ApiError";
    this.kind = kind;
    this.status = options.status ?? 0;
    if (options.budget) this.budget = options.budget;
  }

  /**
   * Whether retrying the identical request could plausibly succeed.
   *
   * Used by TanStack Query's `retry` and by the SSE reconnect loop. A 402 is
   * *not* retryable: the cap resets on a clock, and hammering it turns one calm
   * message into a flicker of the same message.
   */
  get retryable(): boolean {
    return this.kind === "network" || this.kind === "server_error" || this.kind === "rate_limited";
  }
}

/** Build the right `ApiError` from a non-2xx response. */
export async function errorFromResponse(response: Response): Promise<ApiError> {
  const body = await readBody(response);

  if (response.status === 402) {
    // The backend nests the budget detail under FastAPI's `detail` key.
    const detail = (body as { detail?: unknown } | null)?.detail;
    const parsed = budgetExceededDetail.safeParse(detail);
    if (parsed.success) {
      return new ApiError("budget_exceeded", parsed.data.message, {
        status: 402,
        budget: parsed.data,
      });
    }
    // A 402 whose body we cannot read is still a budget stop -- degrade to the
    // generic copy rather than reporting it as a server error, which would tell
    // the learner to retry into a cap that will refuse them again.
    return new ApiError("budget_exceeded", "You've reached your usage limit.", { status: 402 });
  }

  const kind = classify(response.status);
  return new ApiError(kind, detailMessage(body) ?? response.statusText ?? "Request failed", {
    status: response.status,
  });
}

function classify(status: number): FailureKind {
  if (status === 429) return "rate_limited";
  if (status === 404) return "not_found";
  if (status === 401 || status === 403) return "forbidden";
  if (status >= 500) return "server_error";
  if (status >= 400) return "invalid_request";
  return "server_error";
}

async function readBody(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

/**
 * FastAPI's `detail` is a string for `HTTPException(detail="...")` and a list
 * of objects for a validation error. Neither is learner-facing copy; this is
 * for the log and for `error.tsx`'s developer-visible cause.
 */
function detailMessage(body: unknown): string | null {
  if (!body || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0];
    if (first && typeof first === "object" && "msg" in first) {
      return String((first as { msg: unknown }).msg);
    }
  }
  return null;
}

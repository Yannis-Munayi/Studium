import "server-only";
import { cookies } from "next/headers";

/**
 * Learner identity (spec §5.1's `(auth)` route group, §17's Tier 2 auth flow).
 *
 * **There is no authentication behind this.** The backend has no sign-in
 * endpoint, no session cookie, and no token: `POST /api/session` takes a raw
 * `user_id` in the request body and trusts it. Anyone who can reach the API can
 * open a session as anyone. See DIVERGENCES-FRONTEND.md F7.
 *
 * So this module is a *seam*, not an implementation. It answers "who is the
 * learner" from a cookie the sign-in page sets, which is exactly as strong as
 * asking politely -- and it is deliberately the only place in the app that
 * answers that question, so replacing it with a real session lookup is one
 * function body and no call-site changes.
 *
 * Two properties make the placeholder safe to build against and unsafe to ship:
 * it is server-only (the `server-only` import fails the build if a client
 * component imports it), and it refuses to run outside development unless an
 * explicit opt-out is set.
 */

const COOKIE = "studium-learner";

/** Set by an operator who understands the paragraph above. */
const ALLOW_UNAUTHENTICATED = "STUDIUM_ALLOW_UNAUTHENTICATED";

export class AuthenticationNotBuiltError extends Error {
  constructor() {
    super(
      "Learner identity is a development placeholder and this is not a development build. " +
        "Wire a real session lookup in lib/auth.ts, or set " +
        `${ALLOW_UNAUTHENTICATED}=1 to run without one.`,
    );
    this.name = "AuthenticationNotBuiltError";
  }
}

/**
 * The current learner's id, or null when nobody has identified themselves.
 *
 * Throws in production rather than defaulting to a seeded user. A placeholder
 * that silently works in a deployment is how a placeholder becomes permanent.
 */
export async function currentUserId(): Promise<string | null> {
  // `cookies()` first, deliberately. It is what tells Next this render depends
  // on the request, so a route calling it opts out of static generation. With
  // the guard above it, `next build` would try to prerender these pages, hit
  // the throw, and fail the build -- the guard firing at build time, which is
  // the one moment it is not talking about.
  const store = await cookies();

  if (process.env.NODE_ENV === "production" && process.env[ALLOW_UNAUTHENTICATED] !== "1") {
    throw new AuthenticationNotBuiltError();
  }

  const value = store.get(COOKIE)?.value;
  return value && isUuid(value) ? value : null;
}

export const LEARNER_COOKIE = COOKIE;

export function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}

import { redirect } from "next/navigation";
import { cookies } from "next/headers";
import { LEARNER_COOKIE, isUuid } from "@/lib/auth";
import { Button } from "@/components/ui/button";

/**
 * Sign in (spec §5.1).
 *
 * This is not authentication and does not pretend to be -- see the header of
 * `lib/auth.ts`. It identifies a learner by pasting their id, which is what the
 * backend's `POST /api/session` accepts today, and it says so on the page. A
 * password field here would be worse than none: it would imply a check that
 * nothing performs.
 *
 * Kept because §17's Tier 2 wants "sign in with a test user, land on the desk"
 * as a real flow, and because the shape of the page -- one form, one server
 * action, one redirect -- is what a real sign-in will replace.
 */
export default function SignInPage() {
  async function identify(formData: FormData) {
    "use server";

    const userId = String(formData.get("user_id") ?? "").trim();
    if (!isUuid(userId)) redirect("/sign-in?error=invalid");

    const store = await cookies();
    store.set(LEARNER_COOKIE, userId, {
      httpOnly: true,
      sameSite: "lax",
      path: "/",
      secure: process.env.NODE_ENV === "production",
      maxAge: 60 * 60 * 24 * 30,
    });
    redirect("/");
  }

  return (
    <main id="main" className="mx-auto flex min-h-dvh max-w-md flex-col justify-center px-normal">
      <h1 className="font-serif text-3xl font-semibold leading-[var(--leading-heading)]">Studium</h1>
      <p className="mt-tight font-sans text-sm text-muted">
        Enter your learner id to continue.
      </p>

      <form action={identify} className="mt-loose flex flex-col gap-normal">
        <div>
          <label htmlFor="user_id" className="font-sans text-sm text-ink">
            Learner id
          </label>
          {/* §13.3 (WCAG 3.3.2): the format is not obvious, so it is described. */}
          <p id="user_id-hint" className="mt-1 font-sans text-xs text-muted">
            A UUID, like 0f8fad5b-d9cb-469f-a165-70867728950e
          </p>
          <input
            id="user_id"
            name="user_id"
            required
            aria-describedby="user_id-hint"
            autoComplete="off"
            spellCheck={false}
            className="mt-tight w-full rounded border border-line bg-background px-3 py-2 font-mono text-sm text-ink"
          />
        </div>

        <Button variant="primary" asChild>
          <button type="submit">Continue</button>
        </Button>
      </form>

      <p className="mt-generous font-sans text-xs text-muted">
        This identifies you; it does not authenticate you. The backend has no sign-in yet, so
        don&apos;t put anything here you wouldn&apos;t hand to a stranger.
      </p>
    </main>
  );
}

# Secrets

Infrastructure §6. The registry is `backend/studium/ops/secrets.py`; everything
below reads from it, and `backend/.env.example` is generated from it.

## Where they live (§6.2)

| Environment | Store | How |
|---|---|---|
| Production | Fly secrets | `flyctl secrets set NAME=value -a studium-backend` |
| Local | `.env.local`, gitignored | copy `.env.example` and fill in |
| CI | GitHub Actions secrets | repository settings, scoped per workflow |

`.env.example` is committed and carries names with placeholder values, never
real ones. It is **generated** — `studium ops secrets render --write` — and a
Tier 1 test fails on a diff, so a secret added to the code cannot be missing
from the file a new operator reads.

## Checking a live environment

```sh
studium ops secrets check --environment production
studium ops secrets check --environment local
studium ops secrets check --environment ci --app frontend
```

Three levels, and the distinction is the point:

- **MISSING** — required here. The deployment does not work without it.
- **absent** — degrades a subsystem, loudly and on purpose. `VOYAGE_API_KEY`
  absent means retrieval falls back to keyword-only (retrieval §16); that is a
  decision, not a fault.
- **ok** — present, or not required in this environment.

Reporting all three as "missing" is how a check gets ignored, so it does not.
Nothing prints a value.

## Adding a secret

1. Add a `Variable(...)` to `REGISTRY` in `backend/studium/ops/secrets.py`.
   Classify it: `secret=True` if disclosure would allow unauthorised access,
   enable fraud, or compromise learner data (§6.1's three tests).
2. `studium ops secrets render --write` and commit the regenerated
   `.env.example`.
3. `flyctl secrets set` in production, GitHub secret for CI, `.env.local`
   locally.
4. If it is *not* a secret and the deployment needs it, put it in the `[env]`
   block of `fly.toml` instead. A Tier 1 test fails the build if anything
   classified as a secret appears there.

## Quarterly rotation (§6.3)

```sh
studium ops secrets rotate     # prints the procedure and the current list
```

The ordering it prints is the content. **Set the new key, verify, *then*
revoke the old one.** Revoking first makes the window between the two an
outage whose cause is a rotation nobody has finished writing down.

## Revoking someone's access (§6.4)

> "When a person's access is revoked, all secrets they had access to are
> rotated within 48 hours. This is the 'assume compromise' default even when
> there's no evidence of it."

That means everything in `QUARTERLY_ROTATION`, plus:

- `STUDIUM_SIGNING_KEY` — [`signing-keys.md`](signing-keys.md). This one is a
  full rotation, not a re-issue, and the old public key stays published.
- The database credentials — a Fly-side operation
  (`flyctl postgres users ...`), then `STUDIUM_DATABASE_URL` and
  `STUDIUM_OWNER_DATABASE_URL`.
- GitHub repository access, and the Sentry, Langfuse, Anthropic and Voyage
  consoles. Those are not in any registry and are not automatable from here;
  they are on the checklist because the registry only covers what the code
  reads.

## What is deliberately not a secret

`LANGFUSE_HOST`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `STUDIUM_ENV`,
`STUDIUM_STORAGE_BACKEND`, `STUDIUM_BACKEND_ORIGIN` and the feature flags. §3:
"The distinction between 'configuration that can be committed' (feature flags,
timeouts, model choices) and 'secrets that cannot' is bright and enforced."

Two that look like exceptions and are not:

- **`LANGFUSE_PUBLIC_KEY`** is public in Langfuse's own sense and is kept in
  the secret store anyway, because it is half of a credential pair and
  splitting a pair across a committed file and a secret store is how the wrong
  half gets rotated.
- **`STUDIUM_SIGNING_KEY_ID`** is published at `/api/signing-keys` and is kept
  beside its private half for the same reason: a rotation that updated one and
  not the other issues credentials citing a key that did not sign them.

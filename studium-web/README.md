# Studium — frontend

Subsystem 4 of 7: the Next.js application a learner actually touches.

| | |
|---|---|
| Spec | `Documentation/Planning/Sub System 4 - Frontend and interaction/04-frontend-v1.0.md` |
| Divergences | [DIVERGENCES-FRONTEND.md](DIVERGENCES-FRONTEND.md) |
| Consumes | the agent runtime's SSE contract, and retrieval's citation endpoint |

Read [DIVERGENCES-FRONTEND.md](DIVERGENCES-FRONTEND.md) before trusting the spec
about the network layer. Four of its entries are load-bearing, and three of those
share one cause: **§4 and §8.1 describe an SSE contract subsystem 2 does not
implement.** The stream is a POST, not a GET; it carries a fifth chunk kind the
spec does not list; and it never tells the client which artifact a segment
became.

## Layout

```
app/
  (auth)/sign-in/       identity, not authentication -- see lib/auth.ts
  (app)/                everything behind it; force-dynamic, per learner
    page.tsx              the desk (§6.1)
    sessions/new/         the start form (§7.1)
    sessions/[id]/        the classroom and the bench (§6.2, §6.3)
    journal/              the confusion journal (§6.4)
    settings/             preferences (§14.2)
  api/backend/[...path]/ the FastAPI proxy -- the only way out (F12)

components/
  ui/         Radix wrappers, the icon vocabulary, the live regions
  session/    streaming, citations, interruption, the palette, checks
  surfaces/   the five MVP surfaces
  shared/     chrome, the help overlay, the "not built yet" panel

lib/
  api/        Zod schemas, the client, TanStack hooks, the F4 endpoint map
  stream/     transport, SSE parser, sentence port, citation AST pass, buffer
  state/      Zustand: session, UI preferences, command palette
  a11y/       the announcer and §8.3's exact announcement strings
  copy/       degradation, primitives, per-surface copy (§16.5)
```

## Getting started

```sh
npm ci
npm run dev          # needs a backend; see below
```

The app talks to FastAPI through `STUDIUM_BACKEND_ORIGIN` (default
`http://127.0.0.1:8000`). To run against the real thing:

```sh
cd ../backend && make db-up && make db-reset
uvicorn studium.api.app:app --port 8000
```

Then sign in at `/sign-in` with a seeded learner's UUID. There is no password,
because there is nothing to check one against — see F7.

## Tests

```sh
npm run check        # typecheck + lint + Tier 1
npm run test         # Tier 1 alone (194 tests, no browser, no backend)
npm run build && npm run test:e2e    # Tier 2 (28 tests, real Chromium)
```

**Tier 1** is the gate on every push. It covers the SSE frame parser (including
frames and multi-byte characters split across network reads), the sentence
boundary port, citation marker parsing, the Zustand stores driven through a whole
interrupted lecture, Zod validation at the boundary, and axe-core over every
component.

**Tier 2** drives a real Chromium against a real Next production build, streaming
real SSE over a real socket from a mocked-LLM backend (`e2e/mock-backend.ts`). It
verifies the frontend against the *contract*; it cannot verify the contract, and
that is what Tier 3 is for.

**Tier 3** spends money and never runs in CI:

```sh
STUDIUM_RUN_PAID_TESTS=1 \
STUDIUM_BACKEND_ORIGIN=http://127.0.0.1:8000 \
STUDIUM_TEST_LEARNER_ID=<seeded learner uuid> \
  npm run test:paid
```

## Conventions worth knowing before you edit

- **The stream is a POST and cannot be an `EventSource`.** `lib/stream/transport.ts`
  explains why in full. If you find yourself reaching for `EventSource`, the
  learner's utterance has nowhere to go.

- **A stream that ends without `event: done` is a drop, not a completion.**
  Those two are the same observation — bytes stopped arriving — unless you look
  for the terminator. `transport.ts` throws `StreamInterruptedError` on one and
  returns normally on the other, and §15.3's copy depends on the difference.

- **Cancel the reader when the consumer walks away.** A stream nobody is reading
  is still generating tokens, and those are billed. The `finally` in
  `readTurnStream` is a billing safeguard before it is a resource one.

- **`degraded` is a chunk kind and it carries the only thing the learner will
  see.** On a budget stop it is the runtime's entire output. Dropping it produces
  a turn that renders nothing, with no error.

- **Backend copy is rendered verbatim.** `studium/copy/degradation.py` already
  writes learner-facing sentences, with budget reset times localised. Writing a
  second version here gives the product two voices, and the learner reads
  whichever one the failure path took. `lib/copy/degradation.ts` covers only
  failures the server was never involved in.

- **The sentence detector is a port, and CI checks that it still is.**
  `scripts/regenerate-parity-fixture.py` runs the Python original and writes the
  fixture; the `parity` job regenerates and fails on a diff. Change one side and
  you must change the other or justify it.

- **Citation substitution belongs in the AST.** A regex over rendered output
  rewrites `[P3]` inside a code fence about citation formats. Only the tree knows
  which nodes are literal.

- **Contrast is checked in a browser, not in jsdom.** axe cannot evaluate
  contrast without layout, so Tier 1 disables the rule and Tier 2 checks it in
  both themes. That pass is what caught F8 — §16.2's accent failing §13.1 under
  white text — and reading would not have.

- **One live region per politeness, both mounted from first paint.** A region
  inserted alongside its first message is not announced. Identical consecutive
  messages need the nonce, or the second one is silent.

- **Surfaces without endpoints say so.** `lib/api/surfaces.ts` lists what has to
  exist; enabling one is deleting a line from `UNAVAILABLE`. Do not let them 404
  — "Not found" about a learner's own journal is a lie.

- **`lib/auth.ts` is a seam and refuses to run in production.** That refusal is
  the point. A placeholder that works silently in a deployment is a placeholder
  that becomes permanent.

## Known incomplete

- **Citations do not resolve.** F3 / backend SD5. Every marker renders; no card
  populates. Needs one field on the runtime's `end` chunk.
- **The desk, journal and summary read paths have no server.** F4. The UI is
  built and unit tested; the optimistic-update code has never run against a real
  backend.
- **`let_me_try_one` does not route to the bench.** F15. The bench is built and
  reachable; nothing puts a real Curator problem in it.
- **Shiki is not wired.** Code blocks render monospace without syntax colour.

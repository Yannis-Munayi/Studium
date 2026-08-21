# Divergences from Frontend and Interaction Specification v1.0

The implementation targets `Documentation/Planning/Sub System 4 - Frontend and
interaction/04-frontend-v1.0.md`. This file records where it differs, and why.
It is the counterpart to `backend/DIVERGENCES.md` (data layer),
`DIVERGENCES-RUNTIME.md` (agent runtime) and `DIVERGENCES-RETRIEVAL.md`
(retrieval); the F-series numbering keeps the four sets distinguishable in code
comments.

Each entry says what the spec asks for, what the code does, and what would have
gone wrong the other way.

**Four of them are load-bearing** — cases where following the spec literally
produces a frontend that either does not run at all or runs and is quietly
wrong. Three of those four (F1, F2, F3) have the same root cause, and it is
worth naming up front: **spec v1.0 §4 and §8.1 describe an SSE contract that
subsystem 2 does not implement.** §18 says the frontend "consumes agent output
through the SSE contract established in subsystem 2 §20, which is unchanged" —
that is true of §20's *design*, and not true of the endpoint that shipped.

---

## Load-bearing divergences

### F1 — the SSE consumer is `fetch`, not `EventSource`

**Spec.** §4 ("Streaming") chooses "native `EventSource` for SSE consumption. No
third-party library — the browser API is sufficient for the use case and reduces
dependency surface." §8.1 has the hook "open an `EventSource` connection to
`/api/session/{sessionId}/stream`".

**Reality.** There is no `GET /stream` endpoint. The runtime streams from
`POST /api/session/{session_id}/turn`, with the learner's utterance and any
explicit primitive in the **request body** (`backend/studium/api/app.py`).
`EventSource` issues a GET, cannot carry a request body, and cannot set headers.
There is no arrangement of the two that works.

**Code.** `lib/stream/transport.ts` reads the stream with `fetch` plus a
`ReadableStream`, and `lib/stream/parse.ts` implements the SSE grammar the
runtime emits — `data:` frames and the `event: done` terminator — as an
incremental push-parser.

**If followed literally.** Nothing would stream. The very first turn would fail,
and the failure would present as a 405 or a hanging connection rather than as
anything naming the cause.

**What it costs.** `EventSource`'s automatic reconnection, which is the one
thing §4 was buying. Reconnection is therefore hand-rolled in
`useSessionStream` — see F6, because §8.1's resume story has no endpoint behind
it either.

**What it also buys, and should be kept.** A `fetch` reader can distinguish "the
tutor finished" from "the connection died", because the runtime's `event: done`
terminator is observable. `EventSource` cannot: it reconnects on any close, so
both look identical. `transport.ts` throws `StreamInterruptedError` on a body
that ends without the terminator, which is what makes §15.3's two-stage copy
possible at all.

---

### F2 — the `degraded` chunk kind is missing from §8.1

**Spec.** §8.1 step 2 enumerates the chunk kinds as "`text`, `tool_effect`,
`trace`, `end`, per subsystem 2 §6", and step 3 dispatches on exactly those
four.

**Reality.** `StreamChunk.kind` in `backend/studium/agents/base.py` is
`Literal["text", "tool_effect", "trace", "end", "degraded"]`. The fifth is the
one that carries **learner-visible copy when a model call has failed past its
retries** — the Orchestrator emits it for a budget stop, a rate limit, a content
filter trip, a parse failure, and an illegal transition
(`agents/orchestrator.py::handle_turn`).

**Code.** `lib/api/schemas.ts` includes it, `useSessionStore.applyChunk` routes
it to a visible notice, and `components/session/degradation-notice.tsx` renders
its text verbatim.

**If followed literally.** A consumer built to §8.1's list drops the chunk. The
stream then ends with its terminator and no content, so the learner sees a turn
that produced nothing, with no error and no explanation — the exact silent
failure §3 forbids by name ("never a raw error, never a spinner without
explanation, never a silent failure"). It would be worst in precisely the case
it matters most: a budget cap, where the runtime's *only* output is the
`degraded` chunk.

Pinned by `tests/schemas.test.ts` (asserts the kind list has five members and
names `degraded`) and by a Tier 2 case that configures the mock to degrade and
asserts the copy reaches the screen.

---

### F3 — citations cannot be resolved, because no artifact id reaches the client

**Spec.** §10 builds the whole citation experience on retrieval §12's endpoint,
`GET /api/artifacts/{artifact_id}/citations`. §8.2 has the marker rendered
immediately and "the citation resolution fetched lazily on first hover".

**Reality.** The endpoint exists and works. The client has no way to learn the
`artifact_id`. A lecture segment becomes a `content_artifacts` row via a
`record_content_artifact` ToolEffect, and effects are *returned* by the agent and
applied by the Orchestrator **after** the call completes (agent runtime §6, and
the reason it is done that way is retry safety). So:

- the `tool_effect` chunk carries the artifact's *payload*, not its id — the id
  does not exist yet when the chunk is emitted; and
- the Lecturer's `end` chunk carries `turn_id`, `segment_index` and `anchor`,
  and no artifact id (`agents/lecturer.py`).

**Code.** Everything on the client side is built and tested: the marker parser,
the range expansion, the hover card with its 300/100ms delays, keyboard
activation, the retired-source treatment, and the full-passage modal.
`Citation` takes an `artifactId` and `lib/api/schemas.ts` already parses
`artifact_id` off the `end` payload, so the day the runtime sends it, hover
cards populate with no change here.

Until then the marker still renders — §8.2 requires it visible the instant it
arrives — and the card says the source is not linked to this segment yet. It
does not show a spinner. A spinner that never resolves is the failure §3 rules
out, and it is what "just leave it loading" would produce.

**If followed literally.** Every hover card in the product would spin forever.
The citation UI is a substantial share of §10 and of the product's claim to be
grounded, and it would be inert with no error anywhere.

**The fix, and where it belongs.** One line in `agents/lecturer.py`: once the
Orchestrator has applied the segment's effects, include the resulting artifact id
on the `end` chunk. That is subsystem 2's change to make, not this subsystem's —
deciding it from here would be designing another subsystem's contract from
outside it, which `backend/SPEC_DEBT.md` SD1 records as how three earlier
provenance gaps happened. Raised as **SD5**.

The Tier 3 citation case asserts *which* message the card shows, so the day the
runtime starts sending the id, that test fails and reports that the gap closed.

---

### F4 — the desk, the journal and the session summary have no endpoints

**Spec.** §6.1 (the desk), §6.4 (the journal), §6.5 (session close) and §12
describe five surfaces' worth of reading UI over `journal_entries`,
`session_summaries`, `concept_mastery` and `learner_subjects`.

**Reality.** All four tables exist and are written to. `backend/studium/api/app.py`
serves the session lifecycle, the interrupt channel, session state, and citation
resolution. It serves nothing else. There is no `GET /api/journal`, no
`GET /api/desk`, no session-summary read endpoint.

**Code.** `lib/api/surfaces.ts` names every route these surfaces need, at the
paths the client already calls, and marks them unavailable in one set. The
client functions are written against the shapes the data layer guarantees;
turning one on is deleting a line from `UNAVAILABLE`.

Surfaces render `components/shared/unavailable.tsx` rather than discovering the
gap as a 404 — which would say "Not found" about a learner's own journal, and
that is a lie. The endpoint name is in a `title` attribute for whoever is
building, not on screen for whoever is studying.

**If followed literally.** Three of the five MVP surfaces would 404 and blame
the learner for it.

**Consequence to carry forward.** §17's Tier 2 case "Journal CRUD: create, view,
edit, resolve entries; verify optimistic updates and rollback" cannot run
end-to-end. The optimistic-update machinery it would exercise is built and unit
tested (`useResolveJournalEntry`, with the `cancelQueries`-then-snapshot-then-
rollback sequence §14.1 specifies), but nothing has driven it against a server.
**That code is unverified against a real backend and should be treated as such.**

---

## Ordinary divergences

### F5 — the interrupt response is read from the body, not the status

§8.3 step 2 expects "202 Accepted". The runtime answers **200** with
`{accepted, state, detail}`, and answers it even when nothing was interruptible —
deliberately, per its own docstring: "an interrupt that arrives when nothing is
streaming is a no-op rather than an error worth showing anyone."

That is the better contract for this UI. "Nothing was streaming" is a state the
button has to render (un-arm itself), not an error. `signalInterrupt` reads
`accepted`; a false answer un-arms the control rather than leaving it depressed
with nothing to release it.

### F6 — a dropped stream is replayed, not resumed

§8.1 ("Reconnection") says the frontend "sends a resume request with the last
received `turn_index`; the backend either resumes from that point or acknowledges
the stream is complete". No endpoint accepts such a request.

`useSessionStream` replays the turn instead, up to §15.3's three attempts with
exponential backoff. This is not free — the learner may see the opening of a
segment twice — and it costs a second generation of the same content, which is
real money. It is the honest fallback rather than a silent one: the classroom
marks the retry rather than pretending it was seamless.

Closing this needs a resume endpoint on subsystem 2. Worth noting the runtime is
already most of the way there: `InterruptionPoint` records `delivered_text`
precisely so the Lecturer can resume from it.

### F7 — there is no authentication, and the sign-in page says so

§5.1 has an `(auth)` route group and §17's Tier 2 wants "sign in with a test
user, land on the desk". There is no authentication anywhere in the backend:
`POST /api/session` takes a raw `user_id` in the body and trusts it. Anyone who
can reach the API can open a session as anyone.

`lib/auth.ts` is a seam, not an implementation. It resolves identity from a
cookie, it is the only place in the app that answers "who is the learner", and it
has two properties that make it safe to build against and awkward to ship: it is
`server-only` (a client component importing it fails the build), and it throws in
a production build unless `STUDIUM_ALLOW_UNAUTHENTICATED=1` is set explicitly.

The sign-in page states plainly that it identifies rather than authenticates. A
password field would be worse than none — it would imply a check that nothing
performs.

### F8 — §16.2's accent fails §13.1's contrast requirement on a filled button

§16.2 locks the accent at `hsl(200 45% 40%)`. §13.1 requires 4.5:1 for text.

As ink — links, focus rings, borders — the accent measures 4.8:1 on the
background and is fine. As a **filled button** carrying the near-white
`--color-on-accent`, it measures **3.22:1**. Both sentences are in the spec and
they cannot both hold for the primary button.

`--color-accent-strong` (`hsl(200 45% 30%)`, 7.1:1) is used for filled surfaces;
§16.2's value is kept exactly where §16.2 is talking about it. Darkening
`--color-accent` globally would have changed every link and focus ring in the
product to fix a button.

Found by the Tier 2 contrast pass, not by reading — which is the argument for
that pass existing. jsdom cannot evaluate contrast, so the Tier 1 axe run
disables the rule and it is checked in a real browser instead, in both themes.

### F9 — the runtime state is seeded and reconciled, not read from the stream

§9.2 keys the contextual primitive buttons off the session state, and §8.3 keys
the interrupt button's armed state off it too. The stream does not carry it: only
the Orchestrator's own transitions put `next_state` on an `end` chunk, and an
ordinary lecture segment's `end` chunk has none.

Left as the spec implies, the state stays null for a whole lecture and §9.2's
buttons never appear at all.

The store seeds the state from the session mode (mirroring `MODE_ENTRY_STATE` in
`state_machine.py`), updates it whenever an `end` chunk does carry one, and
reconciles against `GET /api/session/{id}/state` after each turn. That endpoint
is described in the runtime as a diagnostic surface for its curl tests; it is
cheap — the Orchestrator is in memory, so the call is a dictionary lookup — and
it is the only thing that actually knows.

A failure to reconcile is swallowed. A stale toolbar is a small cosmetic wrong;
an error banner over a lecture that streamed perfectly well, because a diagnostic
call failed, is a larger one.

### F10 — cancelling an interrupt resumes the lecture rather than offering to

§8.3's "Cancellation" says Escape before typing means "no new stream from the
Tutor, the interrupted lecture resumes with a two-sentence recap". An earlier
build of this surface parked the learner on the §8.3-step-7 resumption card
instead, which is a different interaction: they raised their hand, changed their
mind, and would have had a second decision to make to get back to where they
were. The classroom now issues the resume directly. Caught by a Tier 2 case that
asserts a new turn is requested and that it carries no learner text.

### F11 — the client's sentence detector is a port, and deliberately not authoritative

§19's open question 1 asks whether the server's boundary algorithm can be reused
client-side or whether the client should lean on server-provided markers.

Reused, for one cosmetic job: knowing where settled prose ends so the trailing
fragment gets §8.2's "still generating" treatment. `lib/stream/sentences.ts`
ports the rule from `orchestration/streaming.py::find_boundary` faithfully and
omits both of its escapes — the LLM fallback after 60 tokens and the hard cut at
80 — because those decide *where a lecture actually stops*, which is persisted as
the resume anchor and is not the client's decision to make.

So the two are allowed to disagree in exactly one direction: the client may be
behind the server, never ahead. `tests/sentence-parity.test.ts` asserts that as a
property over a corpus generated from the Python original, and a CI job
regenerates the fixture and fails on a diff — so "the port matches" is a checked
fact rather than a comment.

### F12 — every backend call goes through a Next proxy

§5.1 leaves `app/api/` as "proxy routes to FastAPI backend as needed".
`app/api/backend/[...path]/route.ts` proxies all of them, for two reasons.

The browser never learns the backend origin, so there is exactly one server-side
place a request can acquire identity and no second code path that could forget
to. And SSE has to survive the hop: a route handler that buffers the body turns a
streaming lecture into one block delivered when the lecture ends, which looks
exactly like the server having hung. The backend already sets
`X-Accel-Buffering: no` for the proxy in front of *it*; this is the same problem
one layer up, and `duplex: "half"` plus passing `response.body` through untouched
is what keeps it a stream.

### F13 — two live regions, not one

§13.4 says "a single announcement region at the top of the app receives these;
component-level `aria-live` is avoided to prevent overlap", and assigns `polite`
to stream text and `assertive` to interrupt state changes, errors and
completions.

One element cannot be both politenesses. `components/ui/announcements.tsx`
renders one region per politeness, both once, both at the top of the app. The
rule §13.4 is protecting — no component-level regions, nothing competing — holds
exactly.

Both are in the DOM from first paint and start empty, because a live region
inserted at the same time as its first message is not announced by most screen
readers: the region has to exist before the mutation it reports. A nonce forces
re-announcement of identical consecutive messages, which are otherwise not a DOM
change and therefore silent — "Ready for your question" twice in a row is a real
sequence in §8.3.

### F14 — citation substitution happens in the AST, not over rendered text

§10.1 gives a regex over "citation markers in streaming text". Applied to the
rendered output it would rewrite `[P3]` inside a code fence teaching citation
formats, and inside KaTeX's annotation nodes, both of which must stay literal.

`lib/stream/rehype-citations.ts` runs as a rehype plugin, after KaTeX, and skips
`code`, `pre`, `math`, `annotation`, `semantics` and anything carrying a `katex`
class. Only the tree knows which nodes those are.

The renderer also holds back a trailing suffix that could still be growing into a
marker (`[P1` before its `]` arrives). Without it, every citation in every lecture
renders as literal text and then flickers into a superscript one token later.

### F15 — a `let_me_try_one` problem is not yet routed to the bench

§9.3 says the primitive moves the session to LAB and the problem renders on the
bench. The runtime does this correctly: `_handle_let_me_try_one` yields an `end`
chunk with `next_state: "LAB"` and the Curator's problem in `problem`.

The `Bench` surface is built, tested and accessible, and the `end` payload is
parsed and stored. What is not built is the classroom handing that payload to it
and switching surfaces — the classroom currently renders the problem as streamed
prose, which is legible and is not §6.3's two-column workspace.

This is an incomplete implementation rather than a contract problem, and it is
called out here rather than left to be discovered: **the bench is reachable by
route and by props, and no code path puts a real Curator problem into it.**

---

## Things the spec locks that were followed despite an alternative

**Zustand plus TanStack Query, with the boundary §3 draws.** The temptation with
a stream is to keep the buffer in TanStack Query alongside everything else. §3 is
right that this is where consistency bugs come from, and the split is kept
strictly: no server data is mirrored into Zustand, no client state is persisted.

**No auto-scroll by default (§8.2).** Every chat interface in existence does the
opposite. §8.2 is correct that a lecture is not a chat — scrolling the page under
someone who is reading paragraph two because paragraph four arrived is hostile —
and it is implemented as specified, with the chat behaviour available as an
opt-in preference.

**KaTeX and Shiki server-side "where possible" (§4).** KaTeX runs through
`rehype-katex` in the client renderer, because streamed math has to re-render
when its closing delimiter arrives and there is no server round trip in that
loop. Shiki is not wired at all yet: code blocks render in the monospace face
with correct layout and no syntax colour. §13.1's requirements are met either
way (colour carries no information), so this is a visual gap, not a compliance
one.

---

## Deferred to later subsystems, as the spec directs

- **Observability (§15.1, subsystem 7).** `app/error.tsx` logs to the console
  and shows the digest. Sentry configuration is subsystem 7's.
- **Deployment (§4, subsystem 7).** No Fly.io configuration here. The app builds
  to a standard Next production server.
- **Rendering regression against golden datasets (§19, subsystem 6).** The hooks
  exist: the Tier 2 mock backend takes a scripted output, so a fixed backend
  output can be replayed and asserted against. The curated datasets are
  subsystem 6's.
- **The four deferred surfaces (§2).** Concept graph map, review cycle UI,
  multi-voice library, summative assessment. The desk reserves layout space for
  the review queue per §6.1, and `where_does_this_fit` renders as prose per §9.3.

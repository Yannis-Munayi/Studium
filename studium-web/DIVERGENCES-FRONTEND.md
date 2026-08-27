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

**Status, 22 August 2026.** F3, F4 and F15 are closed. F16–F22 were added
closing them, and they have a common shape worth reading as one thing: a
frontend built against a contract, with no server behind it, is not verified —
it is only *consistent*. Four of the seven are defects in code that had passing
unit tests, and the tests were passing because the fixtures agreed with the code
about a question neither had asked the real system.

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

### F3 — citations could not be resolved, because no artifact id reached the client

**Closed, 22 August 2026.** Kept because the shape of the gap is the useful
part, and because what closed it was an ordering change rather than a field.

**Spec.** §10 builds the whole citation experience on retrieval §12's endpoint,
`GET /api/artifacts/{artifact_id}/citations`. §8.2 has the marker rendered
immediately and "the citation resolution fetched lazily on first hover".

**What was wrong.** The endpoint existed and worked. The client had no way to
learn the `artifact_id`. A lecture segment becomes a `content_artifacts` row via
a `record_content_artifact` ToolEffect, and effects are *returned* by the agent
and applied by the Orchestrator **after** the call completes (agent runtime §6,
for retry safety). So the `tool_effect` chunk carried the artifact's payload and
not its id — the row did not exist yet — and the Lecturer's `end` chunk carried
`turn_id`, `segment_index` and `anchor`, and nothing else.

**What closed it.** Not one line in `agents/lecturer.py`, as this entry
originally guessed. The Orchestrator now **holds the `end` chunk** until the
turn's effects have committed, then emits it carrying the id the batch produced.
The Lecturer is unchanged. See `backend/SPEC_DEBT.md` SD5 and
`DIVERGENCES-RUNTIME.md`.

**What did not change here.** Everything on the client side was already built:
the marker parser, the range expansion, the hover card and its 300/100ms delays,
keyboard activation, the retired-source treatment, the full-passage modal. The
`end` payload schema already parsed `artifact_id`, and the store already put it
on the turn. The day the runtime sent it, the cards populated — which is what
happened, with no component edit.

**What the placeholder bought while it was open.** The marker still rendered —
§8.2 requires it visible the instant it arrives — and the card said the source
was not linked yet, rather than spinning. A spinner that never resolves is the
failure §3 rules out. The Tier 3 case asserted *that copy*, so closing the gap
broke the test on purpose; it now asserts the resolved card instead.

**Still true.** A resolvable id is not a resolved citation. Whether the card has
anything in it depends on the concept being curated — `concept_sources`
pointers, or embeddings once a vector provider is configured. An ungrounded
artifact returns an empty list and the card says so, which is correct rather
than broken.

---

### F4 — the desk, the journal and the session summary had no endpoints

**Closed, 22 August 2026.** `backend/studium/api/reads.py` serves all five, and
`UNAVAILABLE` is now empty.

**Spec.** §6.1 (the desk), §6.4 (the journal), §6.5 (session close) and §12
describe five surfaces' worth of reading UI over `journal_entries`,
`session_summaries`, `concept_mastery` and `learner_subjects`.

**What was wrong.** All four tables existed and were written to; no HTTP
exposed them. Surfaces rendered `components/shared/unavailable.tsx` rather than
discovering the gap as a 404 — which would have said "Not found" about a
learner's own journal, and that is a lie.

**What the mechanism was worth.** `SURFACE_ENDPOINTS` and `UNAVAILABLE` are kept
rather than deleted: the set is empty and the guard is a no-op, and both stay
because §2 promises four more deferred surfaces. Turning one off again is adding
a name to a set.

**Four things only a real response could have shown**, all found by serving the
endpoints and now recorded separately: F16 (the hypothesis), F17 (the event
enum), F18 (decayed mastery) and F19 (the optimistic-update crash). The last is
the one that matters most for how this entry used to read. It ended:

> The optimistic-update machinery it would exercise is built and unit tested …
> but nothing has driven it against a server. **That code is unverified against
> a real backend and should be treated as such.**

It was right to say so. The code was wrong, and in a way its unit tests could
not see. See F19.

**§17's Tier 2 case now runs.** "Journal CRUD: create, view, edit, resolve
entries; verify optimistic updates and rollback" is
`e2e/tier2.surfaces.spec.ts`.

**Identity, and what it is not.** These endpoints answer for "me", and the Next
proxy is the single place that says who that is: it resolves the learner
server-side, strips any inbound `X-Studium-User`, and sets its own. That means
no client code path can forget to identify itself and none can lie about it.
It does **not** mean the API is authenticated — the backend trusts the header
exactly as `POST /api/session` trusts a `user_id` in its body (F7). The scoping
is real and checked (`TestScoping` in `tests/online/test_read_surface.py`); the
authentication is still absent.

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

### F15 — a `let_me_try_one` problem is routed to the bench

**Closed, 22 August 2026.** The entry is kept because its diagnosis was wrong in
an instructive way.

§9.3 says the primitive moves the session to LAB and the problem renders on the
bench. This entry said "the runtime does this correctly" and that the missing
piece was the classroom switching surfaces. The classroom was indeed missing —
`Classroom` now renders `Bench` when the runtime reports LAB and a problem is in
the store, and §7.2's "the classroom stays mounted, the content changes" is why
it is a branch rather than a route.

**But the runtime was not correct.** Three defects sat behind this one, none
visible from the frontend, all found by driving the primitive against the real
stack:

- The state machine had no `PRIMITIVE_INVOKED` row outside `TUTORIAL`, so from
  a lecture the chunk said LAB and the machine stayed put (R13).
- Nothing fired `context_ready`, so no real session ever left `OPENING` — where
  no primitive can move anything at all (R14).
- The graded answer was classified as a `comment` and routed to the Tutor, so
  even a rendered bench was never graded (R16, and F21 below).

**And one defect in the payload.** `_handle_let_me_try_one` sent the whole
`PracticeProblem` — including `model_answer` and `expected_key_points`. §11.2
withholds the model answer until the learner has attempted, and the bench having
no component that draws it is a weaker guarantee than the browser never
receiving it. The runtime now splits the problem: the Orchestrator keeps the key
for the Evaluator, the client gets the question. `lib/api/schemas.ts`'s
`practiceProblem` has no field for the answer, so a runtime that regressed could
not hand one to a component. Checked on the wire in both Tier 2 and Tier 3.

**What the bench renders and does not invent.** `toBenchProblem` maps `prompt` →
`statement` and `hint` → a one-element `hints` list. §6.3's `setup`,
`constraints` and `expectedMinutes` have no source in `PracticeProblem` and are
left undefined rather than synthesised — a fabricated "about 10 minutes" would
be the surface making a claim about the learner's work.

---

## Found by serving the endpoints (22 August 2026)

F16–F22 all came out of closing F3, F4 and F15. They are grouped because they
share a cause: **every one was invisible while the surfaces had no data.** A
schema nothing parses, a colour nothing paints, a cache updater nothing calls —
none of them can be wrong until something exercises them.

### F16 — the Tracker's hypothesis is not served to the learner

Two specifications disagree and both are explicit.

Data layer §11, implemented in `studium.acl.project_journal_entry`, says
`journal_entries.hypothesis` is "a Tutor-facing prompt aid … never serialised to
the learner" — because it can be wrong and reading it can be dispiriting.

Frontend §6.4 asks for it on the entry list, and §12.1 asks for it on the detail
view, read-only, behind a "this is the system's inference, not your words"
framing built precisely to answer that concern.

**Resolved in favour of the projection that already shipped.** Overturning a
privacy decision from the endpoint that would benefit from overturning it is not
this build's call. `reads.SERVE_HYPOTHESIS_TO_LEARNER` is a single constant with
both specs quoted above it; the field stays in the response and in the schema,
nullable, so flipping the constant is the whole reversal.

**What it costs.** The surfaces render the hypothesis `if (entry.hypothesis)`,
so they degrade to exactly §6.4's "if it exists" branch and the code that draws
it is currently dead. Journal search excludes the hypothesis for the same
reason — matching on text the learner cannot see produces results they cannot
account for.

### F17 — `journal_event_kind` has eight values, and the schema had six

`lib/api/schemas.ts` listed `["created", "revisited", "addressed", "resolved",
"reopened", "archived"]`. The database enum (data layer §6.7) has
`partially_addressed` where the schema had `addressed`, plus
`hypothesis_updated` and `learner_note_added`, which were absent.

Schema mismatches are a hard failure at the boundary by design (§4, §5.3). So
the first revised hypothesis on any entry — a thing the Confusion-Tracker does
routinely — would have taken the whole detail view down with "the server sent a
response that did not match its schema". Two of the three missing values are
written by the code paths this build added.

The enum is now the database's, and `journal-entry.tsx` maps each value to a
sentence: rendering them raw put `partially_addressed` on screen for a learner
reading their own history.

### F18 — the desk renders decayed mastery, not the raw posterior

`concept_mastery` carries both `p_known` (the BKT posterior after the most
recent evidence) and its value under the forgetting curve. The Curator, the
unlock gate and `suggest_next_unlocked` all read the decayed one — data layer
C1 is explicit, and recomputes it at read time rather than trusting the stored
column, because that column is only as fresh as the last decay job.

§6.1 says "a small visual … showing overall subject mastery" and does not say
which number. The desk showing the raw posterior would summarise a state no
decision was made against: 0.9 on a concept the system had already decided to
revisit.

The endpoint returns both and recomputes the decayed value in SQL, the same
expression `session.lifecycle._refresh_touched_decay` uses. `MasterySummary`
renders `p_known_decayed`. Both fields are in the schema so the difference is
visible rather than a silent choice of one.

### F19 — the optimistic resolve crashed on the surface it was used from

`useResolveJournalEntry` matched `queryKey: ["journal"]` and mapped over
whatever it found. That is correct for the list queries and wrong for
`["journal", "entry", id]`, which holds a single object — `old?.map` on an
object throws.

**It never fired while the endpoints did not exist**, and its unit tests could
not see it: they exercised the mutation against list data only. The moment the
endpoints answered, resolving an entry *from the detail page* — the only place
the resolve button is — would have thrown inside `onMutate`.

This is the specific thing F4 warned about ("unverified against a real backend
and should be treated as such"), and it is worth noting the warning was correct
and still did not prevent the bug. What would have is the Tier 2 case that could
not run.

The two caches are now patched separately, and both are rolled back separately.
Query keys were also narrowed to `["journal", "list", filter]` so the two shapes
cannot be matched by one pattern again.

### F20 — two more status colours fail §13.1 as text

F8's problem, twice more. §16.2 specifies the status colours as *signals* — a
dot, an icon, a bar — and §13.1 requires 4.5:1 of anything carrying text.
Measured on the light background: `--color-correct` is 4.08:1 and
`--color-attention` is 3.60:1. Both are used as small text — §6.4's status pill
and §11.1's verdict lead ("Nice." / "Close.") are words. `--color-concern`
measures 5.80:1 and needs no variant, which is why there isn't one.

`--color-correct-strong` and `--color-attention-strong` are the same hue and
saturation, darkened until they clear 4.5:1 against `--color-surface` — the
harder of the two grounds these land on. Dark mode measures 8.9:1 and 8.1:1 on
the plain tokens, so the strong variants alias them there.

**Why it took until now.** The Tier 2 contrast pass has been running since the
frontend shipped and passed every time, because the desk and the journal had no
rows and therefore no pills. A colour nothing paints cannot fail a contrast
check. Found the first time those surfaces had data in them.

### F21 — the classroom declares its intent where the words do not carry it

§8 classifies every turn with a model call over the learner's utterance. That is
right for typed prose and wrong for a control, and the frontend was relying on
it for both.

- **The bench's Submit.** "It reduces to the identity." classifies as a
  `comment`, which in LAB routes to the Tutor and is never graded. The words are
  not the signal; the button is.
- **Opening and continuing a lecture.** Both sent an empty utterance, which
  classifies as `comment`. The runtime routes to the Lecturer on `LECTURING`
  *and* `intent: "next"`; anything else falls through to the Tutor's generic
  answer. **A lecture session therefore never called the Lecturer** — no
  segment, no artifact, and nothing for §10's citations to resolve against. F3
  was closed and would still have had nothing to show.

The runtime already honoured `LearnerInput.intent` ahead of the classifier;
`TurnRequest` had no field for it (R16). It does now, and the classroom sends
`intent: "next"` for the three lecture-advancing calls and `intent: "answer"`
from the bench. This is the same trade §9.2's primitive buttons already make.

### F22 — the session start navigates with the mode the runtime returned

§20 allows one active session per learner, so `POST /api/session` returns the
existing session when there is one — whatever mode *it* was started in. The
start form navigated with the mode it had asked for.

A learner with an unfinished tutorial who asked for a lecture got a classroom
that believed it was lecturing while the runtime ran a tutorial. Every turn then
disagreed about which agent should serve it, and nothing reported anything: both
halves were behaving correctly on their own reading.

The response now carries `mode` and `resumed` (R15) and the form navigates with
what came back. The field is optional in the schema so a backend that predates
it does not fail the parse; the caller falls back to what it asked for.

**Not addressed:** whether silently resuming is right at all, as against
refusing with a 409 and offering to close the old session. That is a product
question and it is raised in `backend/SPEC_DEBT.md`, not decided here.

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

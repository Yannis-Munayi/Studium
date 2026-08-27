# Divergences from Agent Runtime Specification v1.0

The implementation targets `spec/Sub System 2 - Agent Runtime/02-agent-runtime-v1.0.md`.
This file records where it differs, and why. It is the counterpart to
`DIVERGENCES.md`, which covers the data layer; the R-series numbering keeps the
two sets distinguishable in code comments.

Each entry says what the spec asks for, what the code does, and what would have
gone wrong the other way. Five of them (R1, R2, R4, R13, R14) are cases where
following the spec literally — or, for the last two, failing to follow it at all
— produces a system that runs and is quietly wrong. Those are marked
**load-bearing**.

R13–R16 were added on 22 August 2026 while closing the subsystem 4 gaps, and
they share a provenance worth naming: **every one of them was invisible to the
tiers below Tier 3, because each of those tiers supplies the thing the runtime
was failing to produce.** The Tier 1 tests set the session state by hand; the
frontend's Tier 2 mock answers with the state the runtime was not reaching. Two
components can each be correct against a fixture and wrong against each other.

---

## Load-bearing divergences

### R1 — cache-write tokens are read from the nested breakdown

**Spec.** §19's `AnthropicClient.stream` sketch reads
`response.usage.cache_creation_5m_tokens` and
`response.usage.cache_creation_1h_tokens`.

**Reality.** The API does not expose those as top-level usage fields. It reports
one `cache_creation_input_tokens` total, with the per-TTL split under a nested
`cache_creation` object on SDK versions that surface it.

**Code.** `studium.llm.models.usage_from_response` reads the nested breakdown
when present and otherwise attributes the single total to the TTL the request
asked for. The fallback is exact, because one request only ever writes at one
TTL. An unknown TTL books to the 5-minute tier, the cheaper multiplier, so an
accounting gap never overstates spend it cannot verify.

**If followed literally.** `getattr` on a missing attribute yields `None`, which
coerces to zero — every cache write would be recorded as free. Cache writes are
the *expensive* tier (1.25x and 2x base input), so the ledger would understate
spend by the largest single component and `cost_ledger` would drift from the
provider's invoice with no signal.

---

### R2 — the Confusion-Tracker's cached prefix does not cache

**Spec.** §17 assigns the Confusion-Tracker a 1-hour cache TTL. §18 routes it to
Haiku 4.5, because it runs on every learner turn and cost sensitivity is high.

**Reality.** Haiku 4.5's minimum cacheable prefix is **4096 tokens** — four
times Opus 4.8's 1024. A per-concept tracker prefix is roughly 600-900 tokens.
Below the minimum the API accepts the `cache_control` marker and silently caches
nothing: no error, `cache_creation_input_tokens: 0`.

**Code.** `CachedPrefix.will_cache()` compares the prefix against the model's
minimum, and `system_blocks()` omits the marker when it cannot cache — so a
doomed cache-write premium is never billed. `tests/llm/test_prompt_stability.py`
asserts this specific combination rather than leaving it as a comment.

**Consequence to carry forward.** §17's projected saving does not apply to this
agent. It is the highest-frequency agent in the system (one call per learner
turn, 40-60 turns per session), so this is the largest gap between the spec's
cost model and the built one. Options, none taken here because the spec locks
both the TTL and the routing:

1. Accept it — Haiku input is $1/MTok and the prefix is under 1k tokens, so the
   absolute cost is small (~$0.001/turn of uncached prefix).
2. Pad the prefix past 4096 tokens with genuinely useful content (curated
   misconceptions, worked examples of the gap patterns) so caching engages.
3. Route the tracker to Opus, where 1024 is the floor — but §18 chose Haiku
   *because* this agent runs constantly, so this trades a small certain saving
   for a large certain cost.

Recorded as a v1.1 candidate for §17.

---

### R4 — one `session_turns` row per LLM call

**Spec.** §22's Langfuse hierarchy shows four generations
(`orchestrator.classify_intent`, `curator.open_session`,
`reviewer.retrieval_check`, `evaluator.grade_check`) grouped under a single
`turn_index=0`. §3 requires every LLM call to write an `agent_traces` row.

**Reality.** `agent_traces.session_turn_id` is `NOT NULL` **and** `UNIQUE`. The
data layer models a trace as one-to-one with a turn, so a single turn cannot
carry four traces. The two requirements cannot both hold as written.

**Code.** Each LLM call writes its own `session_turns` row, so `turn_index`
advances per call rather than per learner-facing exchange. The learner-facing
grouping survives as `exchange_index` inside the turn's `input` JSONB and as the
Langfuse span, so "what did the learner see as one reply" is still answerable.

**Why this reading.** The data layer's own docstring for `SessionTurn` calls it
"the message log: every LLM interaction, learner input, tool invocation" — one
row per interaction is what subsystem 1 intended. The alternative, buffering
several calls into one trace, would discard per-call cost attribution, which §3
makes non-negotiable.

**Data-layer v1.2 candidate.** If `turn_index` is meant to be learner-facing,
the schema needs either a nullable `exchange_index` column on `session_turns` or
the unique constraint on `agent_traces.session_turn_id` relaxed to a plain FK.
Recorded per §22's instruction to log schema needs rather than apply them.

---

## Ordinary divergences

### R3 — the Evaluator and Reviewer get 1-hour TTLs

§17 assigns TTLs to the Tutor (5m), the Lecturer, Curator and
Confusion-Tracker (1h), and the Orchestrator's classifier (none). It is silent
on the Evaluator and the Reviewer. Both are given 1 hour: a rubric prefix is
stable for as long as the rubric is, and a Reviewer's per-concept prefix
outlives the gaps between cards in a review session.

**Related finding, not a divergence.** A 1-hour cache write costs 2x base input
and a read costs 0.1x, so 1-hour caching is a *net loss* at two calls
(2.1x against 2.0x) and only pays from the third. That holds comfortably for the
Lecturer, whose segment sequences are many calls on one concept. It is marginal
for the Curator, whose own §9 cost profile is "~1 at session open and ~2-4 at
topic transitions" — a session that opens and transitions once pays more for
caching than it saves. Pinned in
`tests/llm/test_cost_and_routing.py::test_one_hour_caching_does_not_pay_until_the_third_call`
and left as a §17 v1.1 candidate rather than changed, since the spec locks it.

### R5 — the async runtime bridges to a synchronous data layer

§4 makes every agent method `async def`; subsystem 1 is synchronous SQLAlchemy
over psycopg. `studium.asyncdb` runs database work in a worker thread
(`asyncio.to_thread`) with one transaction per call.

Porting the data layer to asyncio would mean re-testing every query, trigger and
isolation guarantee subsystem 1 already verified, to remove a thread hop that is
noise next to a multi-second model call at three-user scale. The interface is
narrow enough that swapping in an async engine later touches one file.

The bridge enforces two rules by shape: no session is held across an `await`
(a model call between two statements would pin a connection for the length of
the generation), and callers return plain values rather than ORM instances
(a detached instance lazy-loading from the event loop thread raises far from
its cause).

### R6 — context rows are dicts, not typed row models

§6 sketches `LearningSessionRow`, `UserRow`, `ConceptRow` and friends. Those
would be a second declaration of a schema the ORM already declares, and the two
drift the first time a column is added — silently, because a Pydantic model with
a missing field validates fine.

Rows in `SessionContext` are plain dicts produced by `asyncdb.row_to_dict`, so
the ORM stays the single source of truth. The fields the *runtime* owns and the
data layer does not — `passages`, `concepts_seen`, `warnings`,
`grounding_version` — are typed.

### R7 — a tutorial interruption is a declared transition

§7's diagram draws an arrow from TUTORIAL into PAUSED_FOR_QUESTION; its
transition table has no such row. The table is what the implementation follows,
so the row was added — a learner interrupting the Tutor mid-answer is not an
error state, and without it the machine would refuse the transition.

### R8 — `mastery.suggest_next_unlocked` is new code

§9 names `mastery.suggest_next_unlocked(learner_subject_id)` as the Curator's
deterministic fallback after two failed unlock checks. Subsystem 1 never shipped
it. Added to `studium/mastery.py` under the name the spec uses — purely
additive, no existing behaviour changed. It picks the lowest-depth unlocked
concept with the least mastery, computing decay in SQL for the same reason
`graph.unlock_status` does.

### R9 — review grading is attributed to the Reviewer

§14 says a review response is "graded by a `check_partial` call to the
Evaluator". §18 routes `Reviewer / grade_response` to Haiku 4.5 as its own row.
Implemented as the Reviewer making its own single-criterion call, which is what
the routing table describes, so the trace is attributed to `reviewer`.

Grading integrity is unaffected: the prompt is the Evaluator's rubric-grading
prefix and carries no Reviewer framing. If §22's per-agent Evaluator dashboard
is meant to include review grades, this is the attribution decision to revisit.

### R10 — deltas stream through until an interrupt arrives

§20 describes `sentence_boundary_iter` as buffering tokens and emitting full
sentences. Buffering unconditionally would make every lecture arrive in
sentence-sized jumps and give up the reason to stream at all.

Deltas are forwarded as they arrive; the boundary machinery engages only once
the interrupt event is set, which is the only time a boundary matters. §20's
actual requirement — "stops requesting new tokens after the next completed
sentence emission" — holds exactly.

### R11 — one budget implementation, two names

The §5 layout names both `orchestration/budget.py` ("cost cap enforcement") and
`session/budget_gate.py` ("pre-flight budget check"). Those are one mechanism,
and implementing it twice would give the runtime two places to disagree about
whether a learner is over their cap. The implementation lives in
`session/budget_gate.py`; `orchestration/budget.py` re-exports it.

### R12 — the retrieval check is a transition effect, not a state

§7's transition table gives the prior-summary branch of `OPENING` the target
"(retrieval-check subroutine)". No event leaves that target, so modelling it as
a state would strand the session. It runs as the transition's effect and the
destination is the mode's entry state, which is what the diagram's arrows show.

---

## Added closing the subsystem 4 gaps (22 August 2026)

The four below came out of closing SD5 and frontend F4/F15. Two are load-bearing
and are marked so; all four were found by running a real session in a real
browser, which is the first time anything had.

### R13 — `PRIMITIVE_INVOKED` has rows beyond `TUTORIAL` — **load-bearing**

**Spec.** §7's transition table gives `primitive_invoked` two rows, both from
`TUTORIAL`: one to `LAB` under `let_me_try_one`, one back to `TUTORIAL` for the
other seven.

**Reality.** The primitive palette lives on the classroom (frontend §9.3), and a
lecture session sits in `LECTURING`. The Orchestrator gated the event on
`state is State.TUTORIAL`, so from anywhere else the primitive ran and the
machine did not move — while `_handle_let_me_try_one` still emitted an `end`
chunk saying `next_state: "LAB"`.

**What went wrong.** The client believed the session was in LAB and the runtime
knew it was not. The learner's next message was then classified in `LECTURING`
and routed to `_handle_conversational` — the Tutor — instead of to
`_handle_lab_answer`. The problem was never graded, and nothing anywhere
reported an error: two components each behaving correctly on their own reading
of the state.

**Code.** `state_machine.py` gains four rows — `LECTURING` and
`PAUSED_FOR_QUESTION`, each splitting on the same guard the `TUTORIAL` rows use
— and `orchestrator._handle_primitive` fires the event wherever the table has a
row (`machine.can(...)`) rather than from one named state. The guard
`primitive_stays_in_tutorial` is renamed `primitive_holds_state`, because it now
holds three states and the old name described only the first.

**Why the table rather than the Orchestrator.** Special-casing "also allow it
from LECTURING" in the dispatcher would put a transition rule somewhere §7 says
transitions do not live, and the Tier 1 sweep — which enumerates the table —
would not have seen it.

### R14 — nothing fired `context_ready`, so no session left `OPENING` — **load-bearing**

**Spec.** §7's table: `OPENING --context_ready--> ` the mode's entry state, split
on whether there is a prior summary. §16's opening sequence is what performs it.

**Reality.** `Event.CONTEXT_READY` existed, both rows existed, and **no
production code path fired it**. `start_session` put the machine in `OPENING`
and nothing ever took it out. Every real session spent its entire life there.

**What went wrong, in three ways at once.**

- `OPENING` is not in `is_interruptible`, so every interrupt was refused for the
  whole session — `signal_interrupt` returned `accepted: false` every time.
- `OPENING` had no `PRIMITIVE_INVOKED` row, so no primitive could move the
  session (R13's fix alone would not have helped a real session).
- `_handle_conversational` routes by state and falls through to the Tutor's
  generic `answer` for any state it does not name. **A lecture session therefore
  never called the Lecturer** — no segment, no `content_artifacts` row, and
  nothing for §10's citations to resolve against.

**Why it survived every tier below Tier 3.** Each one supplied the state the
runtime was failing to reach. The Tier 1 tests and the paid integration tests
both set `orchestrator.machine.state` by hand before the turn; the frontend's
Tier 2 mock answers `GET /state` with `LECTURING`. The only observer that could
have caught it is a real session against the real runtime, and this build is the
first time one ran.

**Code.** `orchestrator._leave_opening`, called from `handle_turn` immediately
after the context is assembled — which is exactly when §16 says the session has
what it needs to leave. The guard is passed honestly (`has_prior_summary` from
the context) rather than hard-coded to the simple branch, so the transition log
records which path a session took even though both targets are the same (R12).

### R15 — a resumed session reports the mode it has, not the one requested

**Spec.** §20's `POST /api/session` returns `{session_id, state}`.

**Reality.** §20 also allows at most one active session per learner, and
`open_session` enforces it by returning the existing session when there is one.
That session may have been started in a different mode — and the response said
nothing about it, so the client navigated with the mode it had asked for.

Found the plain way: a Tier 3 run kept resuming an abandoned *tutorial* session
while the URL said `mode=lecture`. The classroom asked for lecture segments, the
runtime routed every turn to the Tutor, and both were behaving correctly.

**Code.** `open_session` returns an `OpenedSession(session_id, mode, resumed)`,
the Orchestrator holds it, and `StartSessionResponse` carries `mode` and
`resumed`. A resume whose mode differs from the request logs a warning rather
than a debug line: the learner asked for one thing and is getting another, and
every routing decision downstream follows the resumed mode.

**Not fixed here.** Whether resuming is the right behaviour at all — as against
refusing with a 409 and letting the learner close the old session — is a product
question §20 answers only implicitly. Reporting the mode makes the current
behaviour honest; it does not settle that.

### R16 — a client may declare an intent it already knows

**Spec.** §8 classifies every turn's intent with a Haiku call, with a rule-based
floor below 0.7 confidence.

**Reality.** That is right for typed prose and wrong for a control. The bench's
Submit is an *answer* whatever words are in the box — and "It reduces to the
identity." classifies as a `comment`, which in `LAB` falls through to the Tutor
and is never graded. The runtime already had the seam: `LearnerInput.intent` is
honoured by `_classify` ahead of the model call. What was missing was any way
for a client to reach it — `TurnRequest` carried `text` and `primitive` only.

**Code.** `TurnRequest.intent`, validated against `DECLARABLE_INTENTS` —
`question`, `answer`, `comment`, `next`, `back`. The eight `primitive:*` members
of `Intent` are excluded because they have their own field, and `interrupt` and
`end_session` because they have their own endpoints; accepting either here would
give one signal two spellings that could disagree.

This is the same trade §8 already makes for the primitive buttons — "a button
press skips classification entirely; paying a model call to re-derive a signal
the client already sent would be waste" — applied to the two other controls whose
meaning is not in the words.

---

## Added implementing the v1.0.1 transition-emission patch (23 August 2026)

### R17 — `PAUSED_FOR_QUESTION` is in every primitive row the patch gives to `TUTORIAL`

**Spec.** v1.0.1 §3.2's validity matrix names, per primitive, the source states
it is legal from. No row lists `PAUSED_FOR_QUESTION`.

**Reality.** R13 — ratified into the machine three weeks earlier and documented
above — put `PAUSED_FOR_QUESTION` on the `primitive_invoked` rows precisely
because the palette is on screen during a paused lecture. §3.2's matrix is a
tightening of the *primitive* dimension, which the machine did not previously
constrain at all; it does not appear to be a re-litigation of the *state*
dimension R13 settled. Read as written, it would silently revoke R13.

**Code.** `PRIMITIVE_MATRIX` in `state_machine.py` carries
`State.PAUSED_FOR_QUESTION` in all eight rows — every row that contains
`TUTORIAL` also contains it, with no other change to §3.2's contents.

**What the alternative costs.** The palette does not disappear when a learner
raises their hand; §5.1's resumption card sits alongside it. Following §3.2
literally would make every primitive button on a paused lecture return the
400 the patch introduced — the "clear message" of §3.2 would be shown for the
most ordinary interaction in the surface. The failure would be visible rather
than silent, which is an improvement over R13's original defect, but it is
still a working control that stops working.

**Wanted from v1.1.** Confirmation, or an explicit revocation. This is the one
place where the patch and the code disagree about what the matrix contains, and
it is a single line in `state_machine.py` either way. If §3.2 was intended to
narrow R13, the frontend's primitive palette needs to hide during a pause, and
that is a subsystem 4 change rather than a runtime one.

### R18 — an unknown primitive is 422; a known one in the wrong state is 400

**Spec.** §3.2 requires that an invalid primitive/state combination "must be
surfaced to the client so the UI can present a clear message." It does not name
a status code.

**Reality.** Two failures are being distinguished. A primitive name the runtime
has never heard of is a malformed request — the client sent a field value
outside the enum, which is what 422 means and what `POST /turn` already returned
for the same mistake. A real primitive invoked from a state that does not
permit it is a well-formed request the runtime is refusing, which is 400.

**Code.** `POST /api/session/{id}/primitive` returns 422 with the accepted
names, or 400 with `{primitive, state, valid_from}`. `valid_from` is the
matrix's own answer, so a client rendering "you can do this from a lecture or a
tutorial" is reading the same source the refusal came from rather than a copy
that can drift.

**Why it matters.** A single code for both would leave the client unable to
tell "you have a bug" from "not right now", and those want different copy: the
first is not the learner's problem and should never reach them, the second is
a normal thing to say out loud.

### R19 — the session-open endpoint is `POST /api/session`, not `POST /api/session/new`

**Spec.** §7.1's prose describes the end-to-end test as asserting "after
`POST /api/session/new` returns, the session reaches LECTURING."

**Reality.** The route has been `POST /api/session` since the original build,
and §7's emission table names it correctly; only §7.1's prose uses the other
spelling. No route was added or renamed — a `/new` suffix on a POST is
redundant with the method, and adding an alias would give one action two names
that could later diverge.

**Why this is recorded at all.** The emission-path test resolves `Client:`
emissions against the live router, so a table that named `/api/session/new`
would now fail at commit time rather than pass and mislead. That is the check
working. It is noted here so the v1.1 author corrects the prose rather than the
code.

---

## Things the spec locks that were followed despite a newer option

### Model IDs

§4 and §18 lock Claude Opus 4.8 and Claude Haiku 4.5. Both are current and
valid, and the routing table is implemented exactly as written.

Claude Opus 5 has since shipped at the same price as Opus 4.8 ($5/$25 per MTok)
with a lower prompt-cache minimum (512 tokens against 1024). The lower minimum
is directly relevant to R2 — it would not rescue the Haiku-routed
Confusion-Tracker, but it widens what caches elsewhere. Model constants are
centralised in `studium/llm/models.py`, so the change is one edit plus a
re-baseline of the cost tests. Not made here: §18 is a locked table, and
swapping the model under it silently would make the spec and the code disagree
about the thing the spec is most explicit about. Raised as a v1.1 candidate.

---

## Deferred to later subsystems, as the spec directs

- **Retrieval interior (§25, subsystem 3).** `studium/retrieval` defines the
  `PassageRetriever` protocol and ships `CuratedPointerRetriever`, which reads
  the `concept_sources.chunk_ids` an author curated and returns them in document
  order. No embeddings, no ranking; the `stance` argument is accepted and
  ignored. Segments built on it will trip §10's thin-grounding flag more often
  than a real retriever would — that flag firing is the signal, not a defect.
- **Langfuse configuration (§22, subsystem 7).** Tracing is emitted where the
  SDK is installed and configured, and every hook is a no-op otherwise.
  Observability must not be able to fail a learner's turn.
- **Prompt regression against golden datasets (§23, subsystem 6).** The hooks
  exist — prompts are centralised in `llm/prompts.py` and hashed onto every
  trace, so a prompt edit is detectable in the trace history. The curated input
  sets and reviewer sign-off flow are subsystem 6's.
- **Frontend rendering (§25, subsystem 4).** The SSE contract is implemented and
  tested; how a client renders `[Pn]` citation markers, the primitive buttons,
  and degradation copy is not this subsystem's.

# Divergences from Agent Runtime Specification v1.0

The implementation targets `spec/Sub System 2 - Agent Runtime/02-agent-runtime-v1.0.md`.
This file records where it differs, and why. It is the counterpart to
`DIVERGENCES.md`, which covers the data layer; the R-series numbering keeps the
two sets distinguishable in code comments.

Each entry says what the spec asks for, what the code does, and what would have
gone wrong the other way. Three of them (R1, R2, R4) are cases where following
the spec literally would produce a system that runs and is quietly wrong — those
are marked **load-bearing**.

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

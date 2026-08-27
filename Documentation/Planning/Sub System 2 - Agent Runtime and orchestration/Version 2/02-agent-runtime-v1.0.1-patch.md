# Studium — Agent Runtime and Orchestration: v1.0.1 Transition Emission Patch

**A targeted patch to subsystem 2 v1.0. Not the full v1.1 revision — that batches later with all pending items (R1, R2, R4, R7, R9, cost re-baseline, Opus 5 swap, S4). This patch addresses one specific class of defect surfaced by the frontend integration build: state transitions specified without their emission paths. That class of defect prevented real sessions from ever leaving `OPENING`, prevented primitives invoked from the classroom from firing, and left `question_resolved` and `escalate` declared and never emitted (SD6).**

*Version 1.0 of subsystem 2 remains authoritative for everything not patched here. This document supersedes v1.0's §7 transition table, adds one design principle to §3, patches §8's effect-application ordering, patches §11's client wire paths, patches §15's `let_me_try_one` wire contract, and adds a new §23.4 on test discipline. Everything else in v1.0 stands.*

---

## 1. Why this patch exists

The frontend integration build (August 21) closed F3, F4, and F15 as spec'd and, in doing so, exposed that the state machine as spec'd could not actually run a session end-to-end. The dev's report named it directly: nothing ever fired `context_ready`, so no real session left `OPENING`. Two weeks of prior verification passed against fixtures that set `machine.state` by hand, and against a mock that answered `LECTURING` when asked. Every test agreed with every other test; none of them agreed with reality.

The root cause is a single spec-writing mistake I made repeatedly across §7, §8, §11, and §15: I named transitions and their effects, but not the code paths that emit the triggering events. A state machine described as `From | Event | Guard | Effect | To` looks complete on paper. It is not complete in code — it names the destination transitions but says nothing about who fires them, when, or from which layer of the stack. That gap produced four separate defects (`context_ready` unfired, `PRIMITIVE_INVOKED` restricted to `TUTORIAL`, `question_resolved` and `escalate` declared and never fired, and the `AppliedEffects` return shape not spec'd), all of which surface only when the runtime is exercised against a real client rather than against a same-shape mock.

This patch fixes those four defects. It also adds a design principle to §3 and a test-discipline requirement to §23 so the same class of defect does not recur.

## 2. Design principle addition to §3

Add to §3 (Design principles), between the "Interruption is a first-class control flow event" and "Degradation is designed, not accidental" bullets:

**Every state transition names three things: source state, destination state, and the emission path of the triggering event.** A transition table that names only source and destination is a diagram, not a specification. In code, every event is emitted by something — an API endpoint the client posts to, an internal function that decides a condition is met, an ambient signal like a stream boundary or a timer. Spec that omits the emission path produces state machines that look complete and behave as though several transitions do not exist. The emission path must be either an internal function (named), a client API endpoint (named), or an ambient signal (specified with its trigger condition). Anything less is a defect.

## 3. §7 revised: full state machine with emission paths

The v1.0 §7 transition table had columns: From, Event, Guard, Effect, To. This patch adds an **Emission** column and revises the table to include every transition with its emission path made explicit.

Emission types:

- **Client** — the transition is triggered by an HTTP request from the frontend. The endpoint is named.
- **Internal** — the transition is triggered by an Orchestrator or agent function completing. The function is named.
- **Ambient** — the transition is triggered by a background signal (timer, stream boundary, budget cap). The signal's origin is named.

### 3.1 Full transition table (patched)

| From | Event | Emission | Guard | Effect | To |
|---|---|---|---|---|---|
| IDLE | `start_session(mode, focus)` | Client: `POST /api/session/new` | Budget cap not exceeded | Create `learning_sessions` row; assemble `SessionContext`; kick off retrieval check if prior summary exists | OPENING |
| OPENING | `context_ready` | Internal: emitted by `Orchestrator.start_session()` after `SessionContext` assembly completes AND (retrieval check either completed or was skipped because no prior summary exists) | — | Transition to mode's initial state; if lecture mode, immediately enqueue first Lecturer segment | mode's state (LECTURING / TUTORIAL / LAB / REVIEW / SUMMATIVE_ASSESSMENT / OFFICE_HOURS) |
| LECTURING | `segment_complete` | Internal: emitted by Lecturer's streaming loop when its `handle_streaming` iterator exhausts and effects commit | More segments queued | Next segment via Lecturer | LECTURING |
| LECTURING | `segment_complete` | Internal (as above) | Segments exhausted | — | TUTORIAL or LAB per Curator's next-mode decision |
| LECTURING | `learner_interrupt` | Client: `POST /api/session/{id}/interrupt` | Stream in progress | Signal stream to finish sentence via `asyncio.Event.set()` | INTERRUPTED |
| LECTURING | `primitive_invoked` | Client: `POST /api/session/{id}/primitive` with primitive name in body | Primitive is valid from LECTURING (see §3.2) | Route to per-primitive handler | Depends on primitive (see §3.2) |
| INTERRUPTED | `sentence_boundary_reached` | Internal: emitted by `orchestration/streaming.py::sentence_boundary_iter` when its detector confirms a boundary after the interrupt signal | — | Cancel remaining stream tokens; record `session_turns` row for the delivered portion | PAUSED_FOR_QUESTION |
| PAUSED_FOR_QUESTION | `learner_utterance_received` | Client: normal turn-submit endpoint (`POST /turn`) with utterance body | — | Dispatch to Tutor as `interruption_response` | PAUSED_FOR_QUESTION (stays, until resolved) |
| PAUSED_FOR_QUESTION | `question_resolved` | Client: `POST /api/session/{id}/resume` from the "Continue lecture" button on the resumption card | — | Restore `SessionContext` snapshot; resume Lecturer from interruption point with brief recap | LECTURING |
| PAUSED_FOR_QUESTION | `escalate` | Client: `POST /api/session/{id}/escalate` from the office-hours offer that appears after 3+ Tutor turns without resumption OR after 10+ minutes elapsed since the interrupt | — | Convert session mode to `office_hours`; no lecture to resume | OFFICE_HOURS |
| TUTORIAL | `primitive_invoked` | Client: `POST /api/session/{id}/primitive` | Primitive is valid from TUTORIAL (see §3.2) | Route to per-primitive handler | Depends on primitive |
| TUTORIAL | `learner_utterance_received` | Client: `POST /turn` | — | Dispatch to Tutor as `answer` kind | TUTORIAL (stays) |
| LAB | `primitive_invoked` | Client: `POST /api/session/{id}/primitive` | Primitive is valid from LAB (see §3.2) | Route to per-primitive handler | Depends on primitive |
| LAB | `answer_submitted` | Client: `POST /api/session/{id}/practice/submit` | — | Grade via Evaluator; record mastery event | LAB (if correct or attempts remain) or TUTORIAL (if all attempts exhausted) |
| OFFICE_HOURS | `learner_utterance_received` | Client: `POST /turn` | — | Dispatch to Tutor as `office_hours_response` | OFFICE_HOURS (stays) |
| OFFICE_HOURS | `primitive_invoked` | Client: `POST /api/session/{id}/primitive` | Primitive is valid from OFFICE_HOURS (see §3.2) | Route to per-primitive handler | Depends on primitive |
| REVIEW | `answer_submitted` | Client: `POST /api/session/{id}/practice/submit` (same endpoint as LAB) | — | Grade via Evaluator's `check_partial`; update FSRS card | REVIEW (if cards remain) or session close |
| any | `end_session` | Client: `POST /api/session/{id}/close` (learner-initiated) OR ambient (idle timeout, budget cap trip) | Not already CLOSING | — | CLOSING |
| CLOSING | `close_complete` | Internal: emitted by `session/lifecycle.py::close_session` after summary generation, mastery updates, and cost roll-up delta all commit | — | Update `learning_sessions.ended_at`; write `session_summaries` | IDLE |

### 3.2 The `primitive_invoked` transition matrix

The v1.0 §7 table showed `primitive_invoked` only from TUTORIAL, which was wrong — the palette is available from any interaction state where the learner might want a primitive. This subsection replaces the single-row treatment with a per-primitive per-source-state matrix.

| Primitive | Valid from | Destination state | Notes |
|---|---|---|---|
| `explain_differently` | LECTURING, TUTORIAL, OFFICE_HOURS | Source state (stays) | Two-step: Tutor diagnoses what didn't land; Lecturer regenerates with new stance. During the regenerated segment, the state is still LECTURING (or wherever we started). |
| `prove_it_to_me` | LECTURING, TUTORIAL, OFFICE_HOURS | TUTORIAL | If invoked from LECTURING, transitions to TUTORIAL for the exchange; the "return to lecture" happens via `question_resolved` per the interrupt flow. If already in TUTORIAL or OFFICE_HOURS, stays. |
| `where_does_this_fit` | LECTURING, TUTORIAL, LAB, OFFICE_HOURS | Source state (stays) | Renders as a Tutor exchange in the current context; does not shift session mode. |
| `vocabulary_check` | LECTURING, TUTORIAL, LAB, OFFICE_HOURS | Source state (stays) | Same as above. |
| `show_worked_example` | LECTURING, TUTORIAL, LAB | Source state (stays) | Lecturer produces a worked example in the current context. |
| `let_me_try_one` | LECTURING, TUTORIAL | LAB | Curator selects a practice problem; state transitions to LAB. The wire-contract split (§6 of this patch) applies here. |
| `why_does_this_matter` | LECTURING, TUTORIAL, LAB, OFFICE_HOURS | Source state (stays) | Short Tutor exchange. |
| `im_lost` | LECTURING, TUTORIAL, LAB, OFFICE_HOURS | Source state initially, then may transition | Tutor asks what the last thing that made sense was; Curator may reset session focus. If the reset means restarting a segment, transitions to LECTURING with new focus concept. |

**Validation.** When the client posts `primitive_invoked`, the Orchestrator's dispatcher checks the current state against this matrix. Invalid combinations (e.g., `let_me_try_one` from LAB) return HTTP 400 with a specific message naming the invalid combination. Silent no-op is not acceptable — the failure must be surfaced to the client so the UI can present a clear message.

### 3.3 Ambient emissions

Two ambient signals need explicit spec beyond the table:

**Idle timeout.** The Orchestrator sets an `asyncio.Task` at session start with a delay of `target_duration_minutes + 15 minutes`. If the task fires before an `end_session` event has been processed, it emits `end_session` with `end_reason = 'idle_timeout'`. The task is cancelled on every learner turn (so an active session doesn't time out) and restarted at the same interval.

**Budget hard cap.** Every LLM call in the runtime checks the pre-flight budget gate. If the gate detects that the daily hard cap has been exceeded mid-session (edge case: heavy usage in a single session), the gate raises `BudgetExceededError` which the Orchestrator catches and emits as `end_session` with `end_reason = 'budget_cap'`. The learner-visible response includes the reset time in the learner's timezone.

## 4. §8 patch: AppliedEffects return shape and end-chunk ordering

The v1.0 §8 said tool effects are applied after the agent returns, in a single transaction. It did not specify the ordering constraint that when an effect produces an id (like `artifact_id`), that id must be available for inclusion in the end chunk of the stream. The dev's implementation of F3 discovered this: `apply_effects` needs to return the produced ids, and the Orchestrator needs to hold the end chunk until the batch commits.

### 4.1 Patched effect application flow

Replace the v1.0 §8 flow description with:

1. Agent completes its main output (whether streaming or non-streaming). Returns text (or the completed stream), a list of `ToolEffect`, and cost/trace metadata.
2. **For streaming agents:** the Orchestrator has been forwarding text chunks to the SSE emitter throughout. It does not yet emit the `end` chunk.
3. Orchestrator applies the `ToolEffect` list to the database in a single transaction.
4. The application returns an `AppliedEffects` object containing the ids produced by each effect that produces one. Specifically:
   - `record_content_artifact` produces `artifact_id`
   - `update_journal` produces `journal_entry_id`
   - `record_portfolio_item` produces `portfolio_item_id`
   - `schedule_review` produces `review_card_id` (if a new card was created)
   - `flag_for_review` produces `queue_item_id`
5. Orchestrator writes the `session_turns` row for this turn, including `artifact_id` if produced (per data layer §6.6 — the column has existed since v1.1; this is the first path that populates it).
6. **For streaming agents:** Orchestrator now emits the `end` chunk. The chunk's payload includes any ids from `AppliedEffects` that the client needs to resolve downstream references — most importantly `artifact_id`, which the client uses to hit `/api/artifacts/{id}/citations`.

### 4.2 The `end` chunk schema

Add to the `StreamChunk` variants defined implicitly in v1.0 §6:

```python
class EndChunk(BaseModel):
    kind: Literal["end"] = "end"
    artifact_id: UUID | None = None      # populated when the turn produced a content_artifact
    journal_entry_id: UUID | None = None  # populated when the turn produced a journal event
    portfolio_item_id: UUID | None = None
    review_card_id: UUID | None = None
    queue_item_id: UUID | None = None
    turn_index: int                       # for client-side reconciliation on reconnect
```

All id fields are optional because not every turn produces every effect kind. The client uses `artifact_id` (when present) to enable citation resolution for the just-completed segment; other ids are for future features (client-side journal notifications, portfolio previews).

### 4.3 The `AppliedEffects` return type

The v1.0 spec had `apply_effects` returning a list of kind names. Patch it to:

```python
@dataclass
class AppliedEffects:
    kinds: list[str]                              # what was applied
    artifact_id: UUID | None = None
    journal_entry_id: UUID | None = None
    portfolio_item_id: UUID | None = None
    review_card_id: UUID | None = None
    queue_item_id: UUID | None = None

    @classmethod
    def from_effects(cls, effects: list[ToolEffect], ids: dict[str, UUID]) -> "AppliedEffects":
        return cls(
            kinds=[e.kind for e in effects],
            artifact_id=ids.get("artifact_id"),
            journal_entry_id=ids.get("journal_entry_id"),
            portfolio_item_id=ids.get("portfolio_item_id"),
            review_card_id=ids.get("review_card_id"),
            queue_item_id=ids.get("queue_item_id"),
        )
```

The `kinds` field is preserved for logging and observability; the id fields are what the end chunk needs.

### 4.4 Failure semantics

If the effect batch fails mid-transaction, all effects roll back and the Orchestrator emits an `end` chunk with a `error` field describing the failure. The learner sees a message that their input was received but couldn't be recorded; the UI offers a retry. This matches the v1.0 §21 failure mode "Database write failure during ToolEffect application" but makes the end-chunk emission explicit.

## 5. §11 patch: `question_resolved` and `escalate` wire paths

The v1.0 §11 described the "raise your hand" flow in prose but didn't spec the specific endpoints or client-side triggers for `question_resolved` and `escalate`. This patch adds them.

### 5.1 The resumption card

After a Tutor turn completes in PAUSED_FOR_QUESTION, the frontend renders a resumption card (per subsystem 4 §8.3). The card has two primary actions and, conditionally, a third:

- **"Continue lecture"** — always visible. Click emits `question_resolved`.
- **"Ask another question"** — always visible. Click focuses the input; the next learner utterance stays in PAUSED_FOR_QUESTION and dispatches to the Tutor as another `interruption_response`.
- **"Take this to office hours"** — visible only when the escalation threshold has been reached (3+ Tutor turns in this PAUSED_FOR_QUESTION span, OR 10+ minutes since the original interrupt). Click emits `escalate`.

### 5.2 API endpoints

**`POST /api/session/{session_id}/resume`.** Emits `question_resolved`. Body:

```json
{
  "resumption_note_seen": true
}
```

Server-side: verifies the session is in PAUSED_FOR_QUESTION, transitions to LECTURING, invokes the Lecturer with a brief `recap` kind that produces a two-sentence bridge from the interruption point. The bridge is streamed to the client via a new SSE stream (the interrupt closed the previous stream).

**`POST /api/session/{session_id}/escalate`.** Emits `escalate`. Body:

```json
{
  "reason": "learner_initiated"
}
```

Server-side: verifies the session is in PAUSED_FOR_QUESTION, verifies the escalation threshold has been met (defense in depth; the client should not offer the button otherwise, but the server checks). Updates `learning_sessions.mode` to `office_hours`. Emits an OFFICE_HOURS entry-state acknowledgment to the client.

### 5.3 Client-side threshold tracking

The escalation threshold (3+ Tutor turns OR 10+ minutes) is tracked client-side and server-side; the client uses it to decide whether to render the escalation button. The server verifies it on the escalate endpoint. Both use the same computation, defined in `studium.session.escalation.should_offer_escalation(session_id) -> bool`.

### 5.4 What the v1.0 spec had implicitly right

The v1.0 §11 section describing the interrupt flow in prose was directionally correct — it described the sentence-boundary detection, the Tutor takeover, the resumption. What was missing was the explicit endpoints and their triggers. This patch adds those explicit contracts; the underlying flow is unchanged.

## 6. §15 patch: `let_me_try_one` wire-contract split

The v1.0 §15 spec described `let_me_try_one` as transitioning to LAB with a practice problem. It did not spec what data goes over the wire vs. what stays server-side. The dev's implementation was sending `model_answer` and `expected_key_points` to the client. The bench UI never drew them, which is a weaker guarantee than never receiving them.

### 6.1 Server-only fields

The following fields on a practice problem must not cross the wire to the client under any circumstance:

- `model_answer` — the reference answer used for grading
- `expected_key_points` — the rubric key points the Evaluator matches against
- `hint_ladder` beyond the first hint the learner has requested

### 6.2 Client-visible fields

The following fields are what the bench renders:

- `problem_text` — the problem statement
- `setup` (optional) — any context or constraints
- `expected_minutes` (optional) — for the learner's planning
- `hint_ladder` — filtered to only the hints the learner has explicitly requested (starts empty; grows as the learner clicks "Show hint")

### 6.3 Filtering discipline

The filtering happens in the response schema, not in the caller's discretion. The endpoint that returns a practice problem to the client uses a Pydantic model `PracticeProblemForClient` that has only the visible fields; the internal representation `PracticeProblemFull` has all fields. The `for_client()` method on the full model constructs the client-visible view.

```python
class PracticeProblemFull(BaseModel):
    id: UUID
    problem_text: str
    setup: str | None
    expected_minutes: int | None
    hint_ladder: list[Hint]
    model_answer: str          # SERVER ONLY
    expected_key_points: list[str]  # SERVER ONLY

    def for_client(self, hints_shown: int = 0) -> "PracticeProblemForClient":
        return PracticeProblemForClient(
            id=self.id,
            problem_text=self.problem_text,
            setup=self.setup,
            expected_minutes=self.expected_minutes,
            hint_ladder=self.hint_ladder[:hints_shown],
        )

class PracticeProblemForClient(BaseModel):
    id: UUID
    problem_text: str
    setup: str | None = None
    expected_minutes: int | None = None
    hint_ladder: list[Hint] = []
```

### 6.4 Verification on the wire

A test at both Tier 2 and Tier 3 asserts that the JSON response body from the practice-problem endpoint does not contain the keys `model_answer` or `expected_key_points`. The assertion runs on the raw response body, not on a parsed model — parsing into the client model would silently discard extra fields and hide the leak.

## 7. §23 patch: test discipline for state transitions

The v1.0 §23 testing strategy included state machine transition tests, but those tests set `machine.state` by hand and asserted that the transition table matched the code. That is a completeness test, not a correctness test. It cannot catch a defect where a transition is declared but nothing in production code ever fires the triggering event.

Add to §23:

### 7.1 The emission-path test discipline

**No test may set `machine.state` directly.** Every state transition in a test comes from a real event emission via its emission path (per §7's Emission column). The Tier 1 test suite may mock the network and the database, but it may not mock the state machine itself — the state machine is what's being tested.

**Every transition in §7 has an integration test that:**

1. Sets up a session in a valid source state via real transitions from IDLE.
2. Emits the transition's event via its real emission path (HTTP request for client-emitted, function invocation for internal-emitted, forced condition for ambient-emitted).
3. Verifies the destination state is reached by inspecting the state machine's actual state after the emission completes.

**The full session-open path (IDLE → OPENING → LECTURING for a lecture session) is a dedicated end-to-end test that exercises real emissions at every step.** It runs at Tier 2 (against a real database) and Tier 3 (against real Anthropic). The test's assertion is that after `POST /api/session/new` returns, the session reaches LECTURING and the Lecturer produces at least one segment. This is the specific test that would have caught the `context_ready` defect.

**A Tier 1 test walks §7's transition table via introspection and asserts:**

1. Every transition's Emission column is populated.
2. For every "Client:" emission, the named endpoint exists in the FastAPI route table.
3. For every "Internal:" emission, the named function exists in the codebase.
4. For every "Ambient:" emission, the described trigger mechanism has a documented handler.

This test fails if any transition in the table lacks a live emission path. Adding a transition to the table without wiring its emission fails the test at commit time.

### 7.2 Retiring the mock-agrees-with-mock pattern

The Tier 2 mock that answered `LECTURING` when asked for state is retired. Replaced with: the Tier 2 mock exercises the same state machine transitions as Tier 3, using a mocked HTTP layer and mocked model calls but the real Orchestrator, real state machine, and real transition dispatch. The distinction between "Tier 2" and "Tier 3" becomes what's mocked at the boundary, not what's mocked internally.

This is a substantial refactor of the existing Tier 2 tests. It is the cost of the discipline; carrying it now prevents the class of defect from recurring.

## 8. What this patch does not cover

The full v1.1 revision of subsystem 2 will fold in the following items still pending, which this patch does not address:

- **R1** (API field names for cache tokens) — spec-code alignment on cache accounting
- **R2** (Confusion-Tracker cache) — accepted uncached posture, spec revision to reflect
- **R4** (trace hierarchy with `exchange_index`) — data layer v1.2 item
- **R7** (missing TUTORIAL → PAUSED_FOR_QUESTION transition) — actually this patch's §7 revision addresses this by explicitly listing every transition; if a specific missing arrow was intended by R7, it should be resurfaced against the patched table
- **R9** (review grading routing) — Evaluator vs Reviewer for review responses
- **Cost re-baseline** — updated cost estimates from measurement
- **Opus 5 swap** — model version update
- **S4** (`retrieve_passages` returns `RetrievalResult`, not bare list) — retrieval contract change

These items batch with the eventual full v1.1 revision.

## 9. Application checklist

For the dev implementing this patch:

1. Add the design principle from §2 to the runtime's inline documentation.
2. Implement `Orchestrator.start_session()` to explicitly emit `context_ready` after `SessionContext` assembly and retrieval check complete. Add a test at Tier 2 that verifies a session reaches LECTURING after `POST /api/session/new`.
3. Extend `primitive_invoked` dispatch to accept all source states listed in §3.2's matrix. Add per-primitive validation. Add tests for at least three cross-source-state cases (e.g., `explain_differently` from LECTURING, `let_me_try_one` from TUTORIAL, `where_does_this_fit` from LAB).
4. Wire the `POST /api/session/{id}/resume` and `POST /api/session/{id}/escalate` endpoints. Add a test that a PAUSED_FOR_QUESTION session returns to LECTURING after resume.
5. Add `should_offer_escalation()` to `studium.session.escalation` and use it both client-side (for rendering the button) and server-side (for verifying the escalate endpoint).
6. Refactor `apply_effects` to return `AppliedEffects` with produced ids. Update the Orchestrator's stream loop to hold the end chunk until the effect batch commits, then include the ids in the end chunk payload.
7. Extend the `EndChunk` schema. Update the client's SSE consumer to read the id fields from the end chunk (no client-side changes to citation-resolution logic — the field is already parsed per the dev's F3 note).
8. Split `PracticeProblemFull` and `PracticeProblemForClient`. Route the `let_me_try_one` response through `for_client()`. Add the wire-level assertion at Tier 2 and Tier 3.
9. Implement the Tier 1 introspection test that walks §7's transition table and verifies every Emission is live.
10. Refactor Tier 2's state-machine tests to use real transitions rather than direct state assignment. Document the change; expect a partial re-write of ~10-20% of the Tier 2 test suite.

Report expected at completion in the same discipline as prior build reports: proven vs. unproven, divergences recorded, tests failing before the patch that now pass.

## 10. Version history

**v1.0.1 — 21 August 2026.** Targeted patch addressing the transition-emission class of defect surfaced by the frontend integration build. Adds the emission-path principle to §3, revises §7 with an Emission column and per-primitive validity matrix, patches §8 for `AppliedEffects` return shape and end-chunk ordering, patches §11 for `question_resolved`/`escalate` wire paths, patches §15 for the `let_me_try_one` server-only field split, adds §23.4 test discipline.

**v1.0 — 15 August 2026.** Initial specification of subsystem 2. Superseded in the sections above by v1.0.1; authoritative elsewhere.

**v1.1 (pending, unscheduled).** Will batch the remaining R1, R2, R4, R7, R9, cost re-baseline, Opus 5 swap, S4 items when the next full revision runs. Not before the batch of pending items across all subsystems is scheduled together.

---

## End of patch

The patch covers one specific class of defect and applies to §3, §7, §8, §11, §15, and §23 only. All other sections of v1.0 remain authoritative. The full v1.1 revision, when it runs, will supersede both this patch and v1.0 with a single consolidated document.

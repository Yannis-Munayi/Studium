# Studium — Agent Runtime and Orchestration: v1.0.2 Amendment to v1.0.1 Patch

**A small amendment resolving two divergences filed during v1.0.1 application. R17 (PAUSED_FOR_QUESTION primitive scope) and R19 (path prose mismatch). Applies on top of v1.0.1; everything else in that patch stands. Total change: two updates, one to §3.2's primitive matrix and one to §7.1's prose.**

*Version 1.0 remains authoritative for everything not patched by v1.0.1 or amended here. The full v1.1 revision, when it runs, will consolidate v1.0 + v1.0.1 + v1.0.2 into a single document.*

---

## 1. R17 resolution: PAUSED_FOR_QUESTION primitive scope

The v1.0.1 patch's §3.2 matrix was silent on PAUSED_FOR_QUESTION for every primitive. R13 (settled three weeks ago, before the v1.0.1 patch) had established that the primitive palette is on-screen and functional during a paused lecture. The dev's implementation kept R13's ruling, reading v1.0.1 §3.2 as tightening the primitive dimension without revoking R13's state dimension. That reading is correct and this amendment ratifies it, with per-primitive refinement so the palette during a pause exposes only the primitives that make sense in that state.

### 1.1 The four primitives valid from PAUSED_FOR_QUESTION

These primitives reference the concept rather than the immediate stream. They make sense during a paused lecture because the learner may reasonably want conceptual scaffolding while an interrupt exchange is in progress.

- **`where_does_this_fit`.** Placing the concept in the graph doesn't depend on a live stream.
- **`vocabulary_check`.** Terminology clarification is orthogonal to the Tutor's ongoing answer.
- **`why_does_this_matter`.** Downstream utility of the concept is stateless with respect to the paused lecture.
- **`im_lost`.** The pause is often exactly when a learner would signal they've lost the thread; suppressing it here would defeat its purpose.

### 1.2 The four primitives not valid from PAUSED_FOR_QUESTION

These primitives either reference a stopped stream or would introduce a nested exchange with no clean return.

- **`explain_differently`.** Ambiguous target — re-explain the last lecture segment, or the Tutor's answer? The ambiguity produces a bad UX regardless of which the system chooses.
- **`show_worked_example`.** Requires a stream context that the pause has suspended.
- **`prove_it_to_me`.** Would open a nested Socratic exchange inside the current paused exchange, with no defined resumption semantics.
- **`let_me_try_one`.** LAB from a paused lecture would need to unwind the pause, transition to LAB, then re-establish the pause on return — three transitions where one primitive should produce one.

### 1.3 Patched matrix (replaces §3.2 in v1.0.1)

| Primitive | Valid from | Destination state |
|---|---|---|
| `explain_differently` | LECTURING, TUTORIAL, OFFICE_HOURS | Source state (stays) |
| `prove_it_to_me` | LECTURING, TUTORIAL, OFFICE_HOURS | TUTORIAL |
| `where_does_this_fit` | LECTURING, TUTORIAL, LAB, OFFICE_HOURS, **PAUSED_FOR_QUESTION** | Source state (stays) |
| `vocabulary_check` | LECTURING, TUTORIAL, LAB, OFFICE_HOURS, **PAUSED_FOR_QUESTION** | Source state (stays) |
| `show_worked_example` | LECTURING, TUTORIAL, LAB | Source state (stays) |
| `let_me_try_one` | LECTURING, TUTORIAL | LAB |
| `why_does_this_matter` | LECTURING, TUTORIAL, LAB, OFFICE_HOURS, **PAUSED_FOR_QUESTION** | Source state (stays) |
| `im_lost` | LECTURING, TUTORIAL, LAB, OFFICE_HOURS, **PAUSED_FOR_QUESTION** | Source state initially, then may transition to LECTURING if Curator resets focus |

The notes column from v1.0.1 §3.2 is unchanged and elided here for brevity.

### 1.4 What this means for the frontend

Subsystem 4's palette rendering during PAUSED_FOR_QUESTION should show four buttons rather than eight: `where_does_this_fit`, `vocabulary_check`, `why_does_this_matter`, and `im_lost`. The other four are hidden from the palette while the session is in PAUSED_FOR_QUESTION and reappear when the state transitions back to LECTURING (via `question_resolved`) or forward to OFFICE_HOURS (via `escalate`).

This does not require a subsystem 4 spec revision; the palette rendering is already state-aware per subsystem 4 §9.2 (contextual buttons vary by session state). The specific per-state visibility for PAUSED_FOR_QUESTION becomes concrete against the matrix above.

### 1.5 Server-side validation

The Orchestrator's primitive dispatcher validates against this matrix. Invalid combinations return HTTP 400 with a message naming the specific invalid pairing (e.g., "primitive `let_me_try_one` is not valid from state `PAUSED_FOR_QUESTION`"). The frontend should not offer the invalid buttons; the server rejects them regardless as defense in depth.

## 2. R19 fix: path prose in §7.1

The v1.0.1 patch's §7.1 prose referenced the session creation endpoint as `POST /api/session/new`. The actual route is `POST /api/session`. The emission column in the transition table names the route correctly; the prose in the surrounding paragraph does not.

### 2.1 Correction

In §7.1's second paragraph, replace:

> The full session-open path (IDLE → OPENING → LECTURING for a lecture session) is a dedicated end-to-end test that exercises real emissions at every step. It runs at Tier 2 (against a real database) and Tier 3 (against real Anthropic). The test's assertion is that after `POST /api/session/new` returns, the session reaches LECTURING and the Lecturer produces at least one segment.

With:

> The full session-open path (IDLE → OPENING → LECTURING for a lecture session) is a dedicated end-to-end test that exercises real emissions at every step. It runs at Tier 2 (against a real database) and Tier 3 (against real Anthropic). The test's assertion is that after `POST /api/session` returns, the session reaches LECTURING and the Lecturer produces at least one segment.

No other change to §7.1. The emission-column entry for IDLE → OPENING was already correct and remains authoritative.

### 2.2 Why the divergence existed and why the table caught it

The prose was hand-authored from memory; the table was authored against the actual FastAPI route table. The introspection test (v1.0.1 §7.1) verifies every "Client:" emission in the table names a real route in the FastAPI app; that test caught the table's correctness at commit time. The prose has no such enforcement — nothing verifies that a hand-typed path in prose matches the actual route. The prose was wrong; nothing structural detected it until the dev cross-referenced against the code.

For the eventual v1.1 revision: if we want to prevent this class of prose defect, the pattern is to reference routes symbolically in prose (e.g., "the session-creation route named in the IDLE row of §7") rather than by literal path. That's a stylistic change I'll apply in v1.1's writing; not worth a separate patch pass.

## 3. Application checklist

For the dev applying this amendment:

1. Update `state_machine.py` primitive validation to accept `PAUSED_FOR_QUESTION` as a valid source state for exactly the four primitives named in §1.1. Reject the other four with the specific 400 error message from §1.5. Add tests for both directions — the four accepted primitives from PAUSED_FOR_QUESTION should succeed; the four rejected should return 400 with the correct message.

2. Update the §7.1 prose in whatever internal documentation carries the v1.0.1 patch content. If v1.0.1 is only referenced from the spec document (not duplicated into code comments or README), this is a documentation-only change with no code impact.

Both changes are small and self-contained. Expected effort: under an hour.

## 4. Version history

**v1.0.2 — 23 August 2026.** Amendment to v1.0.1 patch. Resolves R17 (per-primitive PAUSED_FOR_QUESTION scope) by ratifying R13's state-dimension ruling and adding the four concept-referencing primitives to the matrix. Fixes R19 (path prose mismatch in §7.1).

**v1.0.1 — 21 August 2026.** Targeted patch addressing the transition-emission class of defect. Superseded in §3.2 and §7.1 by v1.0.2; authoritative elsewhere.

**v1.0 — 15 August 2026.** Initial specification. Superseded in the sections patched by v1.0.1 and v1.0.2; authoritative elsewhere.

**v1.1 (pending, unscheduled).** Will consolidate v1.0 + v1.0.1 + v1.0.2 and fold in the remaining pending items (R1, R2, R4, R7, R9, cost re-baseline, Opus 5 swap, S4). Not before the full batch of pending items across all subsystems is scheduled together.

---

## End of amendment

The amendment covers R17 and R19 only. All other v1.0.1 and v1.0 content remains authoritative. R18 was filed and resolved by the dev without needing spec input; the `bench.spec.ts` intermittent failure is recorded as frontend debt for follow-up, not a subsystem 2 concern.

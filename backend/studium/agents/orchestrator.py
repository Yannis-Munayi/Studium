"""The Orchestrator: coordination, not reasoning (agent runtime §8).

Owns the session state machine, assembles context, routes turns to agents,
applies their ``ToolEffect``s, and streams to the client.

§3: "The Orchestrator is a coordinator, not a reasoner." It calls a model for
exactly two things, both narrow, both Haiku, both bounded to a single turn:
intent classification, and sentence-boundary detection when the deterministic
detector fails (§5). Everything else is a deterministic function of the state
machine and the learner's explicit signal.

The classifier has a deterministic escape hatch. Below 0.7 confidence the
rule-based classifier decides instead -- §8 wants a floor under the model on
the one decision that routes every turn, because a misrouted turn does not
degrade gracefully, it goes to the wrong agent entirely.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

from studium.asyncdb import run_db
from studium.copy import degradation
from studium.llm import traces
from studium.llm.client import AnthropicClient, CallSpec
from studium.llm.prompts import build_prefix
from studium.llm.retries import DegradedCall
from studium.orchestration import effects as effects_module
from studium.orchestration.handoff import AgentRegistry
from studium.orchestration.primitives import dispatch_primitive
from studium.orchestration.state_machine import (
    Event,
    GuardContext,
    IllegalTransition,
    SessionStateMachine,
    State,
)
from studium.orchestration.streaming import (
    InterruptionState,
    make_llm_boundary_check,
)
from studium.retrieval import PassageRetriever
from studium.session import memory
from studium.session.budget_gate import BudgetCache, BudgetExceededError
from studium.session.context import SessionContext

from .base import AgentInput, StreamChunk, ToolEffect
from .schemas import Intent, IntentClassification

log = logging.getLogger(__name__)

#: §8: below this the rule-based classifier decides instead.
INTENT_CONFIDENCE_FLOOR = 0.7

#: Rule-based fallback patterns, checked in order. Deliberately conservative --
#: it only fires on phrasings that are unambiguous in isolation, because its
#: job is to be right when the model is unsure, not to be comprehensive.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("end_session", re.compile(r"\b(stop for (today|now)|end (the )?session|i'?m done|that'?s enough for today)\b", re.I)),
    ("interrupt", re.compile(r"\b(wait|hold on|stop|hang on|pause)\b", re.I)),
    ("next", re.compile(r"\b(next|go on|continue|keep going|carry on|move on)\b", re.I)),
    ("back", re.compile(r"\b(go back|previous|back up|repeat that)\b", re.I)),
    ("primitive:im_lost", re.compile(r"\b(i'?m lost|i don'?t follow|lost me)\b", re.I)),
    ("primitive:explain_differently", re.compile(r"\bexplain (it |that )?(differently|another way)\b", re.I)),
    ("primitive:show_worked_example", re.compile(r"\b(show me an example|worked example)\b", re.I)),
    ("primitive:let_me_try_one", re.compile(r"\b(let me try|give me (a|one) problem)\b", re.I)),
    ("primitive:why_does_this_matter", re.compile(r"\bwhy does (this|it) matter\b", re.I)),
    ("primitive:prove_it_to_me", re.compile(r"\b(prove it|why is that true)\b", re.I)),
)


def rule_based_intent(utterance: str, *, state: State) -> Intent:
    """The deterministic classifier (§8).

    Punctuation decides the residual case, matching the spec's own tie-break:
    a question mark or interrogative opener means ``question``, otherwise
    ``comment`` -- except in LAB, where an unmarked utterance is far more
    likely to be the answer they were asked for.
    """
    text = utterance.strip()
    for intent, pattern in _RULES:
        if pattern.search(text):
            return intent  # type: ignore[return-value]

    if "?" in text or re.match(r"^\s*(what|why|how|when|where|which|who|is|are|does|do|can|could|would)\b", text, re.I):
        return "question"
    if state in (State.LAB, State.REVIEW, State.SUMMATIVE_ASSESSMENT):
        return "answer"
    return "comment"


class LearnerInput:
    """One learner turn's worth of input.

    Either free-form text or an explicit primitive button press. A button press
    skips classification entirely -- there is nothing to infer, and paying a
    model call to re-derive a signal the client already sent would be waste.
    """

    def __init__(
        self,
        text: str = "",
        *,
        primitive: str | None = None,
        intent: Intent | None = None,
        answer_to: dict[str, Any] | None = None,
    ) -> None:
        self.text = text
        self.primitive = primitive
        self.intent = intent
        self.answer_to = answer_to or {}

    @property
    def is_explicit(self) -> bool:
        return self.primitive is not None or self.intent is not None


class Orchestrator:
    """One per active session. Holds state; does not persist it (§7)."""

    identity = "orchestrator"

    def __init__(
        self,
        *,
        client: AnthropicClient | None = None,
        agents: AgentRegistry | None = None,
        retriever: PassageRetriever | None = None,
    ) -> None:
        self.client = client or AnthropicClient()
        self.agents = agents or AgentRegistry.build(self.client, retriever)
        self.machine = SessionStateMachine()
        self.interruption = InterruptionState()
        self.budget = BudgetCache()
        self.exchange_index = 0
        #: Set by let_me_try_one, consumed by the LAB turn that grades it.
        self.pending_problem: dict[str, Any] | None = None
        self.failed_attempts = 0
        self._background: set[asyncio.Task[Any]] = set()

    # --- session lifecycle -------------------------------------------------

    async def start_session(
        self,
        user_id: uuid.UUID,
        mode: str,
        focus_concept_id: uuid.UUID | None = None,
        *,
        learner_subject_id: uuid.UUID | None = None,
        target_duration_minutes: int = 90,
    ) -> uuid.UUID:
        """Create a session and run OPENING (§8, §16)."""
        from studium.session.lifecycle import open_session

        gate = await self.budget.check(user_id, mode=mode)
        self.machine.fire(
            Event.START_SESSION, GuardContext(budget_ok=gate.allowed, session_mode=mode)
        )

        session_id = await open_session(
            user_id=user_id,
            mode=mode,
            focus_concept_id=focus_concept_id,
            learner_subject_id=learner_subject_id,
            target_duration_minutes=target_duration_minutes,
        )
        log.info("session %s opened in mode %s", session_id, mode)
        return session_id

    async def end_session(self, session_id: uuid.UUID, reason: str = "learner_stop") -> None:
        """Run CLOSING and release resources (§8, §16)."""
        from studium.session.lifecycle import close_session

        if self.machine.state is not State.CLOSING:
            with contextlib.suppress(IllegalTransition):
                self.machine.fire(Event.END_SESSION)

        ctx = await memory.assemble_context(session_id, exchange_index=self.exchange_index)
        await close_session(ctx, self.agents, reason=reason)

        self.machine.fire(Event.CLOSE_COMPLETE)
        await self._drain_background()

    # --- the main entry point ---------------------------------------------

    async def handle_turn(
        self, session_id: uuid.UUID, learner_input: LearnerInput
    ) -> AsyncIterator[StreamChunk]:
        """Handle one learner turn (§8). Yields chunks to the SSE emitter."""
        self.exchange_index += 1
        ctx = await memory.assemble_context(session_id, exchange_index=self.exchange_index)

        # 2. Budget gate. A hard cap ends the turn with copy, not an exception.
        try:
            gate = await self.budget.check(
                ctx.user_id,
                mode=ctx.mode,
                timezone=str(ctx.learner.get("timezone") or "America/Toronto"),
            )
        except BudgetExceededError as exc:
            log.info("budget gate blocked session %s: %s", session_id, exc)
            yield StreamChunk.degraded(exc.learner_message(), reason="budget_exceeded")
            return

        ctx = ctx.model_copy(update={"warnings": gate.warnings})
        for warning in gate.warnings:
            yield StreamChunk(kind="trace", payload={"budget_warning": warning.model_dump()})

        # 3. Classify.
        intent = await self._classify(ctx, learner_input)

        # Record the learner's own turn before any agent runs, so a failure
        # downstream still leaves their input in the log.
        await self._record_learner_turn(ctx, learner_input, intent)

        # 4-6. Transition, dispatch, stream.
        try:
            async for chunk in self._dispatch(ctx, intent, learner_input):
                yield chunk
        except DegradedCall as exc:
            log.warning("turn degraded on session %s: %s", session_id, exc)
            yield StreamChunk.degraded(degradation.for_failure(exc.kind), reason=exc.kind)
        except IllegalTransition:
            # A programmer error, never learner-visible as such (§6).
            log.exception("illegal transition on session %s", session_id)
            yield StreamChunk.degraded(degradation.for_failure("unknown"), reason="illegal_transition")

        # 9. Confusion-Tracker, fire and forget.
        if intent not in ("next", "back") and learner_input.text.strip():
            self._fire_tracker(ctx, learner_input, intent)

    # --- intent ------------------------------------------------------------

    async def _classify(self, ctx: SessionContext, learner_input: LearnerInput) -> Intent:
        if learner_input.primitive:
            return f"primitive:{learner_input.primitive}"  # type: ignore[return-value]
        if learner_input.intent:
            return learner_input.intent
        if not learner_input.text.strip():
            return "comment"

        try:
            classification = await self._classify_intent(ctx, learner_input.text)
        except DegradedCall:
            log.info("intent classifier degraded; falling back to rules")
            return rule_based_intent(learner_input.text, state=self.machine.state)

        if classification.confidence < INTENT_CONFIDENCE_FLOOR:
            fallback = rule_based_intent(learner_input.text, state=self.machine.state)
            log.debug(
                "classifier confidence %.2f below floor; rules chose %s over %s",
                classification.confidence, fallback, classification.intent,
            )
            return fallback
        return classification.intent

    async def _classify_intent(
        self, ctx: SessionContext, utterance: str
    ) -> IntentClassification:
        """One Haiku call (§5, §8)."""
        from studium.acl import wrap_user_content

        last_system = ""
        for turn in reversed(ctx.recent_turns):
            if turn.get("actor") != "learner":
                last_system = str((turn.get("output") or {}).get("text", ""))[:200]
                break

        prefix = build_prefix("orchestrator", ctx, model=self._intent_model())
        spec = CallSpec(
            agent=self.identity,
            kind="classify_intent",
            prefix=prefix,
            suffix=(
                f"Session state: {self.machine.state}\n"
                f"Last system message (truncated 200 chars): {last_system}\n"
                f"Learner utterance: {wrap_user_content(utterance)}"
            ),
            session_id=ctx.session_id,
            user_id=ctx.user_id,
            exchange_index=ctx.exchange_index,
            concept_id=ctx.focus_concept_id,
            langfuse_trace_id=str(ctx.session_id),
        )
        result = await self.client.parse(spec, IntentClassification, thinking=False)
        return result.structured  # type: ignore[return-value]

    def _intent_model(self) -> str:
        from studium.llm.client import route

        return route(self.identity, "classify_intent")

    async def _record_learner_turn(
        self, ctx: SessionContext, learner_input: LearnerInput, intent: Intent
    ) -> None:
        await run_db(
            lambda s: traces.write_learner_turn_sync(
                s,
                session_id=ctx.session_id,
                text=learner_input.text,
                intent=intent,
                exchange_index=ctx.exchange_index,
                primitive=learner_input.primitive,
                concept_id=ctx.focus_concept_id,
            )
        )

    # --- dispatch ----------------------------------------------------------

    async def _dispatch(
        self, ctx: SessionContext, intent: Intent, learner_input: LearnerInput
    ) -> AsyncIterator[StreamChunk]:
        if intent == "end_session":
            self.machine.fire(Event.END_SESSION)
            yield StreamChunk.ended(next_state=State.CLOSING.value)
            return

        if intent == "interrupt":
            async for chunk in self._handle_interrupt(ctx, learner_input):
                yield chunk
            return

        if intent.startswith("primitive:"):
            async for chunk in self._handle_primitive(ctx, intent, learner_input):
                yield chunk
            return

        if intent == "answer" and self.machine.state is State.LAB:
            async for chunk in self._handle_lab_answer(ctx, learner_input):
                yield chunk
            return

        async for chunk in self._handle_conversational(ctx, intent, learner_input):
            yield chunk

    async def _handle_conversational(
        self, ctx: SessionContext, intent: Intent, learner_input: LearnerInput
    ) -> AsyncIterator[StreamChunk]:
        """Question, answer, comment, next, back -- routed by current state."""
        state = self.machine.state

        if state is State.LECTURING and intent == "next":
            agent, kind = self.agents.lecturer, "deliver_segment"
            payload: dict[str, Any] = {
                "stance": ctx.learner_subject.get("preferences", {}).get(
                    "preferred_stance", "default"
                ),
                "segment_index": int(ctx.session.get("last_segment", 0)) + 1,
            }
        elif state is State.PAUSED_FOR_QUESTION:
            agent, kind = self.agents.tutor, "interruption_response"
            payload = {
                "utterance": learner_input.text,
                "lecture_context": self.machine.interruption_point.get("delivered", ""),
            }
        elif state is State.OFFICE_HOURS:
            agent, kind = self.agents.tutor, "office_hours_response"
            payload = {"utterance": learner_input.text}
        else:
            agent, kind = self.agents.tutor, "answer"
            payload = {"utterance": learner_input.text}

        effects: list[ToolEffect] = []
        turn_id: uuid.UUID | None = None

        async for chunk in agent.handle_streaming(
            AgentInput(session_context=ctx, kind=kind, payload=payload)
        ):
            if chunk.kind == "tool_effect":
                effects.append(ToolEffect(**chunk.payload))
            elif chunk.kind == "end":
                turn_id = _as_uuid(chunk.payload.get("turn_id"))
                yield chunk
            else:
                yield chunk

        await self._apply(effects, turn_id)

    async def _handle_primitive(
        self, ctx: SessionContext, intent: Intent, learner_input: LearnerInput
    ) -> AsyncIterator[StreamChunk]:
        name = intent.split(":", 1)[1]

        # §7: only let_me_try_one changes state, but the transition is applied
        # through the table rather than special-cased here.
        if self.machine.state is State.TUTORIAL:
            with contextlib.suppress(IllegalTransition):
                self.machine.fire(Event.PRIMITIVE_INVOKED, GuardContext(primitive=name))

        effects: list[ToolEffect] = []
        turn_id: uuid.UUID | None = None

        async for chunk in dispatch_primitive(
            name, ctx, self.agents, utterance=learner_input.text
        ):
            if chunk.kind == "tool_effect":
                effects.append(ToolEffect(**chunk.payload))
                continue
            if chunk.kind == "end":
                turn_id = _as_uuid(chunk.payload.get("turn_id")) or turn_id
                self._apply_primitive_outcome(chunk.payload)
            yield chunk

        await self._apply(effects, turn_id)

    def _apply_primitive_outcome(self, payload: dict[str, Any]) -> None:
        """Absorb the state changes a primitive reported."""
        if payload.get("problem"):
            self.pending_problem = payload["problem"]
            self.failed_attempts = 0
        if payload.get("refocus_concept_id"):
            # Recorded for the next context assembly; the concept change is
            # persisted by close_session's focus update, not mid-turn.
            self.machine.interruption_point["refocus"] = payload["refocus_concept_id"]

    async def _handle_lab_answer(
        self, ctx: SessionContext, learner_input: LearnerInput
    ) -> AsyncIterator[StreamChunk]:
        """Grade a practice attempt and route on the verdict (§7)."""
        problem = self.pending_problem or {}
        graded = await self.agents.evaluator.handle(
            AgentInput(
                session_context=ctx,
                kind="grade_practice",
                payload={
                    "answer": learner_input.text,
                    "question": problem.get("prompt", ""),
                    "expected_key_points": problem.get("expected_key_points", []),
                    "attempt": self.failed_attempts + 1,
                },
            )
        )
        await self._apply(graded.tool_effects, graded.turn_id)

        verdict = getattr(graded.structured, "verdict", "incorrect")
        correct = verdict == "correct"
        if not correct:
            self.failed_attempts += 1

        yield StreamChunk.text_chunk(graded.text)

        target, _ = self.machine.fire(
            Event.ANSWER_SUBMITTED,
            GuardContext(
                evaluator_correct=correct,
                failed_attempts=self.failed_attempts,
                curator_next_state=State.TUTORIAL,
            ),
        )

        if target is State.TUTORIAL and not correct:
            # §7: the Tutor takes over after N failed attempts.
            self.pending_problem = None
            async for chunk in self.agents.tutor.handle_streaming(
                AgentInput(
                    session_context=ctx,
                    kind="remediation",
                    payload={
                        "utterance": learner_input.text,
                        "failed_attempts": self.failed_attempts,
                    },
                )
            ):
                if chunk.kind != "end":
                    yield chunk

        if correct:
            self.pending_problem = None
            self.failed_attempts = 0

        yield StreamChunk.ended(next_state=target.value, verdict=verdict)

    # --- interruption ------------------------------------------------------

    def signal_interrupt(self) -> bool:
        """Called by the interrupt endpoint (§20).

        Returns whether the signal was meaningful -- an interrupt arriving when
        nothing is streaming is a no-op, not an error.
        """
        if not self.machine.is_interruptible:
            return False
        self.interruption.signal()
        return True

    async def _handle_interrupt(
        self, ctx: SessionContext, learner_input: LearnerInput
    ) -> AsyncIterator[StreamChunk]:
        """The learner raised their hand: hand off to the Tutor (§11, §20)."""
        if self.machine.state is State.LECTURING:
            self.machine.fire(
                Event.LEARNER_INTERRUPT, GuardContext(stream_in_progress=True)
            )
            self.machine.fire(Event.SENTENCE_BOUNDARY_REACHED)

        delivered = self.machine.interruption_point.get("delivered", "")
        async for chunk in self.agents.tutor.handle_streaming(
            AgentInput(
                session_context=ctx,
                kind="interruption_response",
                payload={
                    "utterance": learner_input.text,
                    "lecture_context": delivered,
                },
            )
        ):
            yield chunk

    def boundary_check(self, ctx: SessionContext) -> Any:
        """The Haiku sentence-boundary fallback (§5, §20)."""

        def spec_factory(buffered: str) -> CallSpec:
            return CallSpec(
                agent=self.identity,
                kind="detect_boundary",
                prefix=build_prefix("orchestrator", ctx, model=self._intent_model()),
                suffix=(
                    "The rule-based sentence detector failed on this text. Return "
                    "the character index just past the end of the first complete "
                    "sentence, as end_index.\n\n"
                    f"TEXT:\n{buffered}"
                ),
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                exchange_index=ctx.exchange_index,
            )

        return make_llm_boundary_check(self.client, spec_factory)

    # --- effects and background work --------------------------------------

    async def _apply(
        self, effects: list[ToolEffect], turn_id: uuid.UUID | None
    ) -> None:
        """Apply a turn's effects, degrading rather than failing the turn (§21)."""
        if not effects:
            return
        try:
            applied = await effects_module.apply_effects(effects, session_turn_id=turn_id)
            log.debug("applied effects: %s", applied)
        except effects_module.EffectApplicationError:
            # §21: the batch rolls back, the trace still stands, the learner is
            # told their answer was received but not recorded.
            log.exception("effect batch failed; state unchanged")
            self.budget.invalidate()

    def _fire_tracker(
        self, ctx: SessionContext, learner_input: LearnerInput, intent: Intent
    ) -> None:
        """Run the Confusion-Tracker off the critical path (§13)."""

        async def run() -> None:
            try:
                output = await self.agents.confusion_tracker.handle(
                    AgentInput(
                        session_context=ctx,
                        kind="evaluate_turn",
                        payload={
                            "turn": {
                                "actor": "learner",
                                "kind": intent,
                                "input": learner_input.text,
                                "output": "",
                            }
                        },
                    )
                )
                await self._apply(output.tool_effects, output.turn_id)
            except Exception:  # noqa: BLE001 -- must never surface to the learner
                log.exception("confusion tracker failed on session %s", ctx.session_id)

        task = asyncio.create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _drain_background(self) -> None:
        """Let fire-and-forget work finish before the session closes.

        Without this, closing a session cancels an in-flight tracker call and
        loses a journal entry that had already been paid for.
        """
        if self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value in (None, "", "None"):
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

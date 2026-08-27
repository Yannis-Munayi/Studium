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
        #: The mode :meth:`start_session` actually opened in, and whether it
        #: resumed an existing session rather than creating one (R15).
        self.opened_mode: str | None = None
        self.resumed = False
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

        opened = await open_session(
            user_id=user_id,
            mode=mode,
            focus_concept_id=focus_concept_id,
            learner_subject_id=learner_subject_id,
            target_duration_minutes=target_duration_minutes,
        )
        # The mode the session *has*, which is not the requested one when an
        # active session was resumed (R15). Held so the endpoint can report it:
        # every routing decision from here follows this value, so a client told
        # otherwise would render a lecture that is really a tutorial.
        self.opened_mode = opened.mode
        self.resumed = opened.resumed
        log.info("session %s opened in mode %s", opened.session_id, opened.mode)

        # v1.0.1 §3.1 names this function as `context_ready`'s emission path:
        # the session leaves OPENING once the context is assembled and the
        # retrieval check has either run or been skipped. Doing it here rather
        # than on the first turn is the difference the patch asks for -- the
        # `POST /api/session` response now reports the state the session is
        # actually in, so a client that navigates on it is not navigating on a
        # state the runtime has not reached yet.
        ctx = await memory.assemble_context(opened.session_id)
        self._leave_opening(ctx)

        return opened.session_id

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

        # 1b. Leave OPENING now that there is a context to leave it with (§16).
        #
        # §7's table has this row and nothing in the runtime fired it, so a
        # freshly-opened session sat in OPENING for its entire life. That is not
        # a cosmetic state error: OPENING is not in `is_interruptible`, so every
        # interrupt was refused; it has no PRIMITIVE_INVOKED row, so no primitive
        # could move the session; and `_handle_conversational` falls through to
        # the Tutor's generic `answer` for any state it does not name, so a
        # lecture session never reached the Lecturer at all.
        #
        # Invisible to every tier below this one, because each supplies the state
        # the runtime was failing to reach: the Tier 1 and paid tests set
        # `machine.state` by hand, and Tier 2's mock reports LECTURING. Found by
        # a real session in a real browser. See DIVERGENCES-RUNTIME (R14).
        self._leave_opening(ctx)

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

    def _leave_opening(self, ctx: SessionContext) -> None:
        """Fire §7's ``context_ready`` once, at the end of ``start_session``.

        Still called from ``handle_turn`` as well, and deliberately: a session
        rebuilt after a process restart (§7's persistence note) never ran this
        ``start_session``, and an Orchestrator reconstructed in OPENING would
        otherwise be stuck there exactly as R14 described. The guard below makes
        the second call free.

        Both branches of the row land on the mode's entry state -- the
        prior-summary branch differs by running the retrieval check as the
        transition's *effect*, not by going somewhere else (R12). The guard is
        still passed honestly rather than hard-coded to the simple branch, so
        the transition log records which path a session took.
        """
        if self.machine.state is not State.OPENING:
            return

        target, effect = self.machine.fire(
            Event.CONTEXT_READY,
            GuardContext(
                has_prior_summary=bool(ctx.prior_session_summary),
                session_mode=ctx.mode,
            ),
        )
        log.info(
            "session %s left OPENING for %s via %s", ctx.session_id, target, effect
        )

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
        end: StreamChunk | None = None

        async for chunk in agent.handle_streaming(
            AgentInput(session_context=ctx, kind=kind, payload=payload)
        ):
            if chunk.kind == "tool_effect":
                effects.append(ToolEffect(**chunk.payload))
            elif chunk.kind == "end":
                # Held back rather than forwarded here. The artifact this
                # segment became has no id until the effects below commit, and
                # the `end` chunk is what carries that id to the client (SD5).
                # The cost is that the terminator now waits on one transaction
                # after the prose has fully arrived; the learner is reading by
                # then, and the alternative is a citation nothing can resolve.
                turn_id = _as_uuid(chunk.payload.get("turn_id"))
                end = chunk
            else:
                yield chunk

        applied = await self._apply(effects, turn_id)

        if end is not None:
            yield _with_produced_ids(end, applied)

    async def _handle_primitive(
        self, ctx: SessionContext, intent: Intent, learner_input: LearnerInput
    ) -> AsyncIterator[StreamChunk]:
        name = intent.split(":", 1)[1]

        # §7: only let_me_try_one changes state, and the transition is applied
        # through the table rather than special-cased here.
        #
        # Fired wherever the table has a row rather than only from TUTORIAL.
        # The palette is on the classroom (frontend §9.3), which is LECTURING
        # for a lecture session -- and gating on TUTORIAL meant a learner asking
        # for a problem mid-lecture got one while the machine stayed in
        # LECTURING. The `end` chunk said LAB, the runtime disagreed, and their
        # answer routed to the Tutor instead of the Evaluator. See
        # DIVERGENCES-RUNTIME (R13).
        guards = GuardContext(primitive=name)
        if self.machine.can(Event.PRIMITIVE_INVOKED, guards):
            self.machine.fire(Event.PRIMITIVE_INVOKED, guards)

        effects: list[ToolEffect] = []
        turn_id: uuid.UUID | None = None
        ends: list[StreamChunk] = []

        async for chunk in dispatch_primitive(
            name, ctx, self.agents, utterance=learner_input.text
        ):
            if chunk.kind == "tool_effect":
                effects.append(ToolEffect(**chunk.payload))
                continue
            if chunk.kind == "end":
                # Same hold-back as _handle_conversational: explain_differently
                # and show_worked_example both run the Lecturer, so both write
                # an artifact whose id belongs on this chunk (SD5).
                turn_id = _as_uuid(chunk.payload.get("turn_id")) or turn_id
                ends.append(chunk)
                continue
            yield chunk

        applied = await self._apply(effects, turn_id)

        for chunk in ends:
            yield StreamChunk(
                kind="end", payload=self._absorb_end(chunk.payload, applied)
            )

    def _absorb_end(
        self, payload: dict[str, Any], applied: effects_module.AppliedEffects
    ) -> dict[str, Any]:
        """Take what a primitive reported; return what the client may see.

        Two jobs, deliberately in one place because they read the same fields.

        The Orchestrator keeps the **whole** practice problem -- the Evaluator
        grades next turn against its key points and model answer, and
        re-deriving them would be a second Curator call for a problem already
        chosen. What goes down the wire keeps only what the bench renders.
        ``problem_private`` is where the handler puts the rest, and this is the
        only place it is unpacked: a model answer sitting in a chunk the browser
        receives is the answer key delivered alongside the question, whether or
        not any component draws it.
        """
        public = dict(payload)
        private = public.pop("problem_private", None)

        problem = public.get("problem")
        if problem is not None or private is not None:
            self.pending_problem = {**(problem or {}), **(private or {})}
            self.failed_attempts = 0

        if public.get("refocus_concept_id"):
            # Recorded for the next context assembly; the concept change is
            # persisted by close_session's focus update, not mid-turn.
            self.machine.interruption_point["refocus"] = public["refocus_concept_id"]

        # v1.0.1 §4.2: every id the batch produced, so a later feature can
        # reach a row that only exists once the transaction committed. A value
        # the handler already put on the chunk wins -- it knew something more
        # specific than "the batch wrote one of these".
        for name, value in applied.end_chunk_ids().items():
            public.setdefault(name, value)
        return public

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
    ) -> effects_module.AppliedEffects:
        """Apply a turn's effects, degrading rather than failing the turn (§21).

        Returns what the batch wrote. An empty result on failure is not a
        silent one: the `end` chunk then carries no ``artifact_id``, and the
        client renders the citation markers with the card that says the source
        is not linked -- which is true, because the artifact was rolled back.
        """
        if not effects:
            return effects_module.AppliedEffects()
        try:
            applied = await effects_module.apply_effects(effects, session_turn_id=turn_id)
            log.debug("applied effects: %s", applied.kinds)
            return applied
        except effects_module.EffectApplicationError:
            # §21: the batch rolls back, the trace still stands, the learner is
            # told their answer was received but not recorded.
            log.exception("effect batch failed; state unchanged")
            self.budget.invalidate()
            return effects_module.AppliedEffects()

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


def _with_produced_ids(
    chunk: StreamChunk, applied: effects_module.AppliedEffects
) -> StreamChunk:
    """Copy an ``end`` chunk with the ids its turn's effects produced (§4.2).

    A copy rather than a mutation: the chunk came from an agent, and an agent's
    output being edited in place is how a value ends up meaning two things
    depending on where you read it.

    Ids the agent already set win. It knew which artifact its own prose came
    from; this only knows which rows the batch wrote.
    """
    produced = applied.end_chunk_ids()
    if not produced:
        return chunk
    return StreamChunk(
        kind="end", payload={**produced, **chunk.payload}
    )


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value in (None, "", "None"):
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

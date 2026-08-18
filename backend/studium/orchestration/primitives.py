"""Tutorial primitive dispatch (agent runtime §15).

Eight primitives, each a small async generator composing one or two agent
calls. The composition is the interesting part: ``explain_differently`` is a
Tutor diagnosis followed by a Lecturer regeneration in a different stance,
``let_me_try_one`` is a Curator selection that moves the session into LAB.

Every handler yields :class:`StreamChunk`s, so the Orchestrator streams a
primitive exactly as it streams an ordinary turn -- the client cannot tell the
difference, which is what lets a primitive be invoked mid-lecture without a
separate client code path.

Handlers also yield the state change they imply, as an ``end`` chunk carrying
``next_state``. Returning it rather than mutating the machine keeps the
state transition where §7 puts it: in the Orchestrator, applied through the
transition table.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable

from studium.agents.base import AgentInput, StreamChunk
from studium.agents.schemas import PRIMITIVE_NAMES, TutorDiagnostic, VocabularyVerdict
from studium.orchestration.handoff import AgentRegistry
from studium.orchestration.state_machine import State
from studium.session.context import SessionContext

log = logging.getLogger(__name__)


class UnknownPrimitive(ValueError):
    """A primitive name with no handler."""


async def _stream_tutor(
    agents: AgentRegistry, ctx: SessionContext, name: str, utterance: str
) -> AsyncIterator[StreamChunk]:
    """The common case: one streamed Tutor turn under the primitive's task."""
    async for chunk in agents.tutor.handle_streaming(
        AgentInput(
            session_context=ctx,
            kind=f"primitive:{name}",
            payload={"utterance": utterance},
        )
    ):
        if chunk.kind != "end":
            yield chunk


async def _handle_explain_differently(
    agents: AgentRegistry, ctx: SessionContext, utterance: str
) -> AsyncIterator[StreamChunk]:
    """Tutor diagnoses what is not landing, Lecturer regenerates in a new stance."""
    diagnostic = await agents.tutor.handle(
        AgentInput(
            session_context=ctx,
            kind="primitive:explain_differently",
            payload={"phase": "diagnose", "utterance": utterance},
        )
    )
    if diagnostic.text:
        yield StreamChunk.text_chunk(diagnostic.text + "\n\n")

    structured = diagnostic.structured
    stance = (
        structured.chosen_stance
        if isinstance(structured, TutorDiagnostic)
        else "intuitive"
    )
    not_landing = (
        structured.what_is_not_landing if isinstance(structured, TutorDiagnostic) else ""
    )

    async for chunk in agents.lecturer.handle_streaming(
        AgentInput(
            session_context=ctx,
            kind="re_explain",
            payload={
                "stance": stance,
                "what_is_not_landing": not_landing,
                "segment_index": ctx.session.get("last_segment", 0),
            },
        )
    ):
        if chunk.kind != "end":
            yield chunk

    yield StreamChunk.ended(primitive="explain_differently", stance=stance)


async def _handle_vocabulary_check(
    agents: AgentRegistry, ctx: SessionContext, utterance: str
) -> AsyncIterator[StreamChunk]:
    """Terminology or substance -- the answer decides which reply follows."""
    verdict_out = await agents.tutor.handle(
        AgentInput(
            session_context=ctx,
            kind="primitive:vocabulary_check",
            payload={"utterance": utterance},
        )
    )
    if verdict_out.text:
        yield StreamChunk.text_chunk(verdict_out.text)

    verdict = verdict_out.structured
    if isinstance(verdict, VocabularyVerdict) and verdict.kind == "substance":
        # Not a wording problem: open a Socratic sub-thread on the idea itself.
        async for chunk in agents.tutor.handle_streaming(
            AgentInput(
                session_context=ctx,
                kind="answer",
                payload={
                    "utterance": (
                        f"{utterance}\n\n[The learner's difficulty with "
                        f"'{verdict.term}' is substantive, not terminological.]"
                    )
                },
            )
        ):
            if chunk.kind != "end":
                yield chunk

    yield StreamChunk.ended(primitive="vocabulary_check")


async def _handle_show_worked_example(
    agents: AgentRegistry, ctx: SessionContext, utterance: str
) -> AsyncIterator[StreamChunk]:
    """Straight to the Lecturer (§15): this primitive is exposition, not dialogue."""
    async for chunk in agents.lecturer.handle_streaming(
        AgentInput(
            session_context=ctx,
            kind="worked_example",
            payload={"stance": "applied", "utterance": utterance},
        )
    ):
        if chunk.kind != "end":
            yield chunk
    yield StreamChunk.ended(primitive="show_worked_example")


async def _handle_let_me_try_one(
    agents: AgentRegistry, ctx: SessionContext, utterance: str
) -> AsyncIterator[StreamChunk]:
    """Curator selects a problem; the session moves to LAB (§7, §15)."""
    problem = await agents.curator.handle(
        AgentInput(
            session_context=ctx,
            kind="select_practice",
            payload={"utterance": utterance},
        )
    )
    yield StreamChunk.text_chunk(problem.text)

    structured = problem.structured
    yield StreamChunk.ended(
        primitive="let_me_try_one",
        next_state=State.LAB.value,
        # The Orchestrator holds this for the LAB turn that follows: the
        # Evaluator needs the key points and the hint to grade against, and
        # re-deriving them would be a second Curator call for the same problem.
        problem=structured.model_dump() if structured else None,
    )


async def _handle_im_lost(
    agents: AgentRegistry, ctx: SessionContext, utterance: str
) -> AsyncIterator[StreamChunk]:
    """Tutor asks what last made sense; Curator resets focus behind it (§15)."""
    async for chunk in _stream_tutor(agents, ctx, "im_lost", utterance):
        yield chunk

    anchor = _last_high_mastery_concept(ctx)
    if anchor is not None:
        yield StreamChunk.ended(
            primitive="im_lost",
            refocus_concept_id=anchor,
            note=(
                "Focus reset to the nearest concept the learner still holds; "
                "the Curator bridges from there once they answer."
            ),
        )
    else:
        yield StreamChunk.ended(primitive="im_lost")


def _last_high_mastery_concept(ctx: SessionContext) -> str | None:
    """The strongest concept in the focus neighbourhood.

    §15 says the Curator resets focus to "the last high-mastery concept in the
    neighbourhood". Chosen deterministically rather than by a model call: the
    learner has just said they are lost, and the right response to that is a
    fast bridge, not another round trip.
    """
    candidates = [
        (ctx.mastery_of(c["id"]), str(c["id"]))
        for c in ctx.focus_neighborhood
        if ctx.mastery_of(c["id"]) >= 0.85
    ]
    return max(candidates)[1] if candidates else None


def _simple(name: str) -> Callable[..., AsyncIterator[StreamChunk]]:
    """Build a handler for a primitive that is one streamed Tutor turn."""

    async def handler(
        agents: AgentRegistry, ctx: SessionContext, utterance: str
    ) -> AsyncIterator[StreamChunk]:
        async for chunk in _stream_tutor(agents, ctx, name, utterance):
            yield chunk
        yield StreamChunk.ended(primitive=name)

    return handler


PRIMITIVE_HANDLERS: dict[str, Callable[..., AsyncIterator[StreamChunk]]] = {
    "explain_differently": _handle_explain_differently,
    "prove_it_to_me": _simple("prove_it_to_me"),
    "where_does_this_fit": _simple("where_does_this_fit"),
    "vocabulary_check": _handle_vocabulary_check,
    "show_worked_example": _handle_show_worked_example,
    "let_me_try_one": _handle_let_me_try_one,
    "why_does_this_matter": _simple("why_does_this_matter"),
    "im_lost": _handle_im_lost,
}

# Every primitive the intent classifier can emit must have a handler, or a
# valid classification would dead-end at dispatch.
assert set(PRIMITIVE_HANDLERS) == set(PRIMITIVE_NAMES), (
    "primitive handler table does not match the classifier's primitive set"
)


async def dispatch_primitive(
    name: str,
    session_context: SessionContext,
    agents: AgentRegistry,
    *,
    utterance: str = "",
) -> AsyncIterator[StreamChunk]:
    """Route a primitive invocation to the correct agent(s) (§15)."""
    handler = PRIMITIVE_HANDLERS.get(name)
    if handler is None:
        raise UnknownPrimitive(f"Unknown primitive: {name}")

    log.debug("dispatching primitive %s on session %s", name, session_context.session_id)
    async for chunk in handler(agents, session_context, utterance):
        yield chunk

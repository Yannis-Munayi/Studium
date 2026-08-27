"""Offline fixtures for the agent runtime (agent runtime §23, "Fixture agents").

§23 asks for "a ``FakeAgent(kind -> canned output)`` class" for Tier 1. What is
here is that plus a fake SDK, because several Tier 1 requirements -- prefix
byte-stability, cost computation, primitive dispatch -- exercise code paths that
run *through* the client wrapper rather than around it. Faking at the SDK
boundary keeps the real routing, prefix assembly, and cost arithmetic under
test; faking at the agent boundary would skip exactly the code most worth
covering.

Nothing here touches the network or the database.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from studium.agents.base import Agent, AgentInput, AgentOutput, StreamChunk, ToolEffect
from studium.orchestration.state_machine import (
    Event,
    GuardContext,
    SessionStateMachine,
    State,
)
from studium.session.context import Passage, SessionContext

# Stable ids so a failure message names the same row twice in a row.
USER_ID = uuid.UUID("00000000-0000-7000-8000-00000000a001")
SESSION_ID = uuid.UUID("00000000-0000-7000-8000-00000000b001")
ENROLLMENT_ID = uuid.UUID("00000000-0000-7000-8000-00000000c001")
SUBJECT_ID = uuid.UUID("00000000-0000-7000-8000-00000000d001")
CONCEPT_ID = uuid.UUID("00000000-0000-7000-8000-00000000e001")
PREREQ_ID = uuid.UUID("00000000-0000-7000-8000-00000000e002")
CHUNK_ONE = uuid.UUID("00000000-0000-7000-8000-00000000f001")
CHUNK_TWO = uuid.UUID("00000000-0000-7000-8000-00000000f002")


def make_context(**overrides: Any) -> SessionContext:
    """A fully-populated context, shaped like what ``assemble_context`` returns.

    Deliberately complete rather than minimal: the prefix builders read a wide
    slice of it, and a fixture that omits fields would make byte-stability tests
    pass against prefixes the real system never produces.
    """
    base: dict[str, Any] = {
        "session": {
            "id": str(SESSION_ID),
            "user_id": str(USER_ID),
            "learner_subject_id": str(ENROLLMENT_ID),
            "mode": "lecture",
            "focus_concept_id": str(CONCEPT_ID),
            "target_duration_minutes": 90,
            "started_at": "2026-08-16T14:00:00+00:00",
            "last_segment": 2,
        },
        "learner": {
            "id": str(USER_ID),
            "display_name": "Test Learner",
            "timezone": "America/Toronto",
            "profile": {
                "stated_goals": "Understand the Church-Rosser theorem properly.",
                "preferences": {"pace": "standard"},
            },
        },
        "learner_subject": {
            "id": str(ENROLLMENT_ID),
            "user_id": str(USER_ID),
            "subject_id": str(SUBJECT_ID),
            "subject_version": 1,
            "preferences": {"pace": "standard", "preferred_stance": "formal"},
        },
        "subject": {
            "id": str(SUBJECT_ID),
            "slug": "lambda-calculus",
            "title": "The Lambda Calculus",
            "version": 1,
            "long_description": "Syntax, reduction, and the Church-Rosser theorem.",
            "updated_at": "2026-08-01T00:00:00+00:00",
        },
        "focus_concept": {
            "id": str(CONCEPT_ID),
            "slug": "beta-reduction",
            "title": "Beta-reduction",
            "long_description": "The computation rule of the lambda calculus.",
            "depth": 2,
            "is_load_bearing": True,
            "position": 2,
            "updated_at": "2026-08-01T00:00:00+00:00",
            "metadata": {
                "learning_objectives": [
                    "State the beta rule precisely.",
                    "Reduce a term to normal form.",
                ],
                "common_misconceptions": [
                    "Confusing alpha-equivalence with beta-equivalence.",
                ],
            },
        },
        "focus_neighborhood": [
            {
                "id": str(PREREQ_ID),
                "slug": "alpha-equivalence",
                "title": "Alpha-equivalence",
                "depth": 2,
                "is_load_bearing": True,
                "metadata": {},
            }
        ],
        "subject_concepts": [
            {
                "id": str(PREREQ_ID),
                "slug": "alpha-equivalence",
                "title": "Alpha-equivalence",
                "depth": 2,
                "position": 1,
                "is_load_bearing": True,
                "estimated_minutes": 30,
                "metadata": {},
                "prerequisite_slugs": ["syntax"],
            },
            {
                "id": str(CONCEPT_ID),
                "slug": "beta-reduction",
                "title": "Beta-reduction",
                "depth": 2,
                "position": 2,
                "is_load_bearing": True,
                "estimated_minutes": 45,
                "metadata": {},
                "prerequisite_slugs": ["alpha-equivalence", "syntax"],
            },
        ],
        "recent_turns": [
            {
                "turn_index": 0,
                "actor": "learner",
                "input": {"text": "why does order not matter here?", "exchange_index": 1},
                "output": {},
                "primitive": None,
                "concept_id": str(CONCEPT_ID),
                "created_at": "2026-08-16T14:05:00+00:00",
            },
            {
                "turn_index": 1,
                "actor": "tutor",
                "input": {"kind": "answer"},
                "output": {"text": "What would happen if you reduced the inner redex first?"},
                "primitive": None,
                "concept_id": str(CONCEPT_ID),
                "created_at": "2026-08-16T14:05:20+00:00",
            },
        ],
        "mastery_snapshot": {str(PREREQ_ID): 0.91, str(CONCEPT_ID): 0.42},
        "open_journal_entries": [
            {
                "id": str(uuid.UUID("00000000-0000-7000-8000-000000001001")),
                "concept_id": str(CONCEPT_ID),
                "concept_slug": "beta-reduction",
                "status": "open",
                "summary": "Treats reduction order as significant for the result.",
                "hypothesis": "Has not internalised confluence.",
                "origin": "tracker_inferred",
                "first_seen_at": "2026-08-15T10:00:00+00:00",
                "last_touched_at": "2026-08-16T14:00:00+00:00",
            }
        ],
        "prior_session_summary": None,
        "retrieval_check_result": None,
        "passages": [
            Passage(
                chunk_id=CHUNK_TWO,
                text="A redex is a term of the form (\\x. M) N.",
                source_title="Barendregt",
                page_start=54,
            ),
            Passage(
                chunk_id=CHUNK_ONE,
                text="Beta-reduction replaces the bound variable with the argument.",
                source_title="Barendregt",
                page_start=52,
            ),
        ],
        "concepts_seen": ["alpha-equivalence", "syntax"],
        "grounding_version": "gv-test-0001",
        "warnings": [],
        "exchange_index": 1,
    }
    base.update(overrides)
    return SessionContext(**base)


# --- fake agents -----------------------------------------------------------


class FakeAgent(Agent):
    """``kind -> canned output``, per §23.

    Records every call so a dispatch test can assert *which* agent was invoked
    with *which* kind -- the primitive-dispatch requirement in §23 is about the
    routing, not the content.
    """

    def __init__(
        self,
        identity: str,
        outputs: dict[str, AgentOutput] | None = None,
        *,
        stream_text: str = "canned streamed text.",
        kinds: Sequence[str] | None = None,
    ) -> None:
        super().__init__(client=None)  # type: ignore[arg-type]
        self.identity = identity
        self.kinds = frozenset(kinds) if kinds else frozenset()
        self.outputs = outputs or {}
        self.stream_text = stream_text
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def handle(self, input: AgentInput) -> AgentOutput:
        self.calls.append((input.kind, dict(input.payload)))
        return self.outputs.get(input.kind, AgentOutput(text=f"{self.identity}:{input.kind}"))

    async def handle_streaming(self, input: AgentInput) -> AsyncIterator[StreamChunk]:
        self.calls.append((input.kind, dict(input.payload)))
        canned = self.outputs.get(input.kind)
        text = canned.text if canned else self.stream_text
        for word in text.split(" "):
            yield StreamChunk.text_chunk(word + " ")
        if canned:
            for effect in canned.tool_effects:
                yield StreamChunk.effect(effect)
        yield StreamChunk.ended(turn_id=None, kind=input.kind)


@dataclass
class FakeRegistry:
    """Stand-in for :class:`studium.orchestration.handoff.AgentRegistry`."""

    curator: FakeAgent = field(default_factory=lambda: FakeAgent("curator"))
    lecturer: FakeAgent = field(default_factory=lambda: FakeAgent("lecturer"))
    tutor: FakeAgent = field(default_factory=lambda: FakeAgent("tutor"))
    evaluator: FakeAgent = field(default_factory=lambda: FakeAgent("evaluator"))
    confusion_tracker: FakeAgent = field(
        default_factory=lambda: FakeAgent("confusion_tracker")
    )
    reviewer: FakeAgent = field(default_factory=lambda: FakeAgent("reviewer"))

    def by_identity(self, identity: str) -> FakeAgent:
        return getattr(self, identity)


# --- fake SDK --------------------------------------------------------------


class FakeUsage:
    """The usage shape the SDK returns, including the nested cache breakdown."""

    def __init__(
        self,
        *,
        input_tokens: int = 100,
        output_tokens: int = 50,
        cache_read: int = 0,
        cache_write_5m: int = 0,
        cache_write_1h: int = 0,
        nested: bool = True,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_write_5m + cache_write_1h
        self.cache_creation = (
            _CacheBreakdown(cache_write_5m, cache_write_1h) if nested else None
        )


@dataclass
class _CacheBreakdown:
    ephemeral_5m_input_tokens: int
    ephemeral_1h_input_tokens: int


class FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeResponse:
    def __init__(
        self,
        text: str = "",
        parsed: BaseModel | None = None,
        usage: FakeUsage | None = None,
        stop_reason: str = "end_turn",
    ) -> None:
        self.content = [FakeTextBlock(text)] if text else []
        self.parsed_output = parsed
        self.usage = usage or FakeUsage()
        self.stop_reason = stop_reason


class FakeStream:
    """Async context manager mimicking ``client.messages.stream``.

    Accumulates usage into ``current_message_snapshot`` as deltas are consumed,
    the way the real SDK does -- which is what lets a test cover the abandoned
    stream case, where ``get_final_message`` is never reached.
    """

    def __init__(self, chunks: Sequence[str], final: FakeResponse) -> None:
        self._chunks = list(chunks)
        self._final = final
        self._consumed = 0

    @property
    def current_message_snapshot(self) -> FakeResponse:
        """Usage as far as the stream actually got."""
        if not self._chunks:
            return FakeResponse(usage=FakeUsage(input_tokens=0, output_tokens=0))
        share = self._consumed / len(self._chunks)
        usage = self._final.usage
        return FakeResponse(
            text="".join(self._chunks[: self._consumed]),
            usage=FakeUsage(
                input_tokens=usage.input_tokens,
                output_tokens=int(usage.output_tokens * share),
                cache_read=usage.cache_read_input_tokens,
            ),
        )

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    @property
    def text_stream(self) -> AsyncIterator[str]:
        async def gen() -> AsyncIterator[str]:
            for chunk in self._chunks:
                self._consumed += 1
                yield chunk

        return gen()

    async def get_final_message(self) -> FakeResponse:
        return self._final


class FakeMessages:
    def __init__(self, owner: FakeSDK) -> None:
        self._owner = owner

    async def parse(self, **kwargs: Any) -> FakeResponse:
        self._owner.calls.append(("parse", kwargs))
        return self._owner.next_parse_response(kwargs)

    def stream(self, **kwargs: Any) -> FakeStream:
        self._owner.calls.append(("stream", kwargs))
        return self._owner.next_stream(kwargs)

    async def count_tokens(self, **kwargs: Any) -> Any:
        self._owner.calls.append(("count_tokens", kwargs))
        return type("Count", (), {"input_tokens": 1234})()


class FakeSDK:
    """A stand-in for ``anthropic.AsyncAnthropic``.

    Records every request so a test can assert on the *request* -- which model
    was routed to, whether ``effort`` was sent to a model that rejects it,
    whether the system block carried a cache marker. Those are the properties
    §18 and §17 actually specify.
    """

    def __init__(
        self,
        *,
        parsed: BaseModel | None = None,
        stream_chunks: Sequence[str] = ("Hello ", "world."),
        usage: FakeUsage | None = None,
    ) -> None:
        self.messages = FakeMessages(self)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._parsed = parsed
        self._stream_chunks = list(stream_chunks)
        self._usage = usage

    def next_parse_response(self, kwargs: dict[str, Any]) -> FakeResponse:
        return FakeResponse(text="", parsed=self._parsed, usage=self._usage or FakeUsage())

    def next_stream(self, kwargs: dict[str, Any]) -> FakeStream:
        text = "".join(self._stream_chunks)
        return FakeStream(
            self._stream_chunks,
            FakeResponse(text=text, usage=self._usage or FakeUsage()),
        )

    # --- assertions helpers -------------------------------------------------

    def requests(self, method: str | None = None) -> list[dict[str, Any]]:
        return [kw for name, kw in self.calls if method is None or name == method]

    def models_used(self) -> list[str]:
        return [kw["model"] for _, kw in self.calls if "model" in kw]


def effect(kind: str, **payload: Any) -> ToolEffect:
    return ToolEffect(kind=kind, payload=payload)


# --- reaching a state the way production does -------------------------------
#
# v1.0.1 §7.1: "No test may set `machine.state` directly. Every state
# transition in a test comes from a real event emission via its emission path."
#
# The rule exists because of R14. A test that assigns `machine.state =
# LECTURING` is asserting against a session that production could not have
# produced -- and R14 was precisely a session production could not produce,
# sitting in OPENING forever while every test that mattered had already skipped
# past it. Walking the real transitions means a broken path breaks the tests
# that depend on it, which is the whole point.

#: How each state is reached from IDLE, as (event, guard-overrides) pairs.
#: Guards are spelled out rather than defaulted so the walk reads as the
#: sequence a session actually performs.
_ROUTE_TO: dict[State, list[tuple[Event, dict[str, Any]]]] = {
    State.IDLE: [],
    State.OPENING: [(Event.START_SESSION, {})],
    State.LECTURING: [
        (Event.START_SESSION, {}),
        (Event.CONTEXT_READY, {"session_mode": "lecture"}),
    ],
    State.TUTORIAL: [
        (Event.START_SESSION, {}),
        (Event.CONTEXT_READY, {"session_mode": "tutorial"}),
    ],
    State.LAB: [
        (Event.START_SESSION, {}),
        (Event.CONTEXT_READY, {"session_mode": "lab"}),
    ],
    State.REVIEW: [
        (Event.START_SESSION, {}),
        (Event.CONTEXT_READY, {"session_mode": "review"}),
    ],
    State.OFFICE_HOURS: [
        (Event.START_SESSION, {}),
        (Event.CONTEXT_READY, {"session_mode": "office_hours"}),
    ],
    State.SUMMATIVE_ASSESSMENT: [
        (Event.START_SESSION, {}),
        (Event.CONTEXT_READY, {"session_mode": "summative_assessment"}),
    ],
    State.INTERRUPTED: [
        (Event.START_SESSION, {}),
        (Event.CONTEXT_READY, {"session_mode": "lecture"}),
        (Event.LEARNER_INTERRUPT, {"stream_in_progress": True}),
    ],
    State.PAUSED_FOR_QUESTION: [
        (Event.START_SESSION, {}),
        (Event.CONTEXT_READY, {"session_mode": "lecture"}),
        (Event.LEARNER_INTERRUPT, {"stream_in_progress": True}),
        (Event.SENTENCE_BOUNDARY_REACHED, {}),
    ],
    State.CLOSING: [
        (Event.START_SESSION, {}),
        (Event.CONTEXT_READY, {"session_mode": "tutorial"}),
        (Event.END_SESSION, {}),
    ],
}


def drive_to(machine: SessionStateMachine, target: State) -> SessionStateMachine:
    """Walk ``machine`` from IDLE to ``target`` by firing real transitions.

    Raises rather than shortcutting if the walk does not arrive: a state that
    can no longer be reached through the table is exactly the defect §7.1 is
    written against, and a helper that quietly assigned the state instead would
    hide it again.
    """
    route = _ROUTE_TO.get(target)
    if route is None:
        raise AssertionError(
            f"no documented route to {target.value}; add one to _ROUTE_TO "
            f"rather than assigning machine.state directly (v1.0.1 §7.1)"
        )

    machine.state = State.IDLE
    for event, overrides in route:
        machine.fire(Event(event), GuardContext(**overrides))

    if machine.state is not target:
        raise AssertionError(
            f"the route to {target.value} arrived at {machine.state.value} -- "
            f"the transition table changed under this helper"
        )
    return machine


def orchestrator_in(orchestrator: Any, target: State) -> Any:
    """Put an Orchestrator's machine in ``target`` via real transitions."""
    drive_to(orchestrator.machine, target)
    return orchestrator

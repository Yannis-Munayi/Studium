"""The agent contract (agent runtime §6).

Every agent implements one interface. That uniformity is what lets the
Orchestrator route without special-casing, lets Langfuse trace uniformly, and
lets a future agent be added by writing one class rather than editing every
caller.

Side effects are *returned*, not applied. An agent decides that mastery moved
or a journal entry is warranted and says so with a :class:`ToolEffect`; the
Orchestrator applies the batch in one transaction after the call completes.
That ordering is what makes a retry safe -- a call that runs twice does not
write twice, because nothing was written the first time.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, Field

from studium.llm.client import AnthropicClient, CallSpec
from studium.llm.traces import TraceRecord
from studium.session.context import SessionContext

#: §8's ToolEffect dispatch table. An effect whose kind is not here is a
#: programmer error, caught at apply time rather than silently dropped.
TOOL_EFFECT_KINDS = frozenset(
    {
        "record_mastery_evidence",
        "update_journal",
        "record_content_artifact",
        "schedule_review",
        "record_portfolio_item",
        "flag_for_review",
    }
)


class AgentDispatchError(RuntimeError):
    """An agent was asked for a ``kind`` it does not accept (§6).

    Always a programmer error, never learner-visible: the Orchestrator catches
    it, logs it, and degrades rather than surfacing the dispatch failure.
    """


class ToolEffect(BaseModel):
    """A side-effect the agent decided should happen.

    Applied by the Orchestrator after the call completes, so retries do not
    double-apply.
    """

    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, _context: Any) -> None:
        if self.kind not in TOOL_EFFECT_KINDS:
            raise ValueError(
                f"unknown ToolEffect kind {self.kind!r}; "
                f"expected one of {sorted(TOOL_EFFECT_KINDS)}"
            )


class AgentInput(BaseModel):
    """Standard input to any agent method call.

    ``session_context`` is the full read side: session row, focus concept,
    recent turns, mastery snapshot, journal entries. Agents receive this
    already assembled; they never query the database themselves.
    """

    model_config = {"arbitrary_types_allowed": True}

    session_context: SessionContext
    #: Method dispatch: 'deliver_segment', 'answer', 'grade', ...
    kind: str
    #: Method-specific arguments, per-agent schema.
    payload: dict[str, Any] = Field(default_factory=dict)

    def require(self, key: str) -> Any:
        try:
            return self.payload[key]
        except KeyError as exc:  # noqa: TRY003
            raise AgentDispatchError(
                f"{self.kind!r} requires payload key {key!r}"
            ) from exc


class AgentOutput(BaseModel):
    """Standard output for non-streaming calls."""

    model_config = {"arbitrary_types_allowed": True}

    #: What would be shown to the learner.
    text: str = ""
    #: Method-specific structured data.
    structured: BaseModel | None = None
    #: Cost, tokens, latency, model, cache stats.
    trace: TraceRecord | None = None
    tool_effects: list[ToolEffect] = Field(default_factory=list)
    #: The turn this call wrote, so effects can be anchored to it.
    turn_id: uuid.UUID | None = None


class EndPayload(BaseModel):
    """The ``end`` chunk's payload contract (§20).

    Every field is optional because six paths emit an ``end`` chunk -- a
    Lecturer segment, a Tutor turn, each primitive handler, a graded lab
    answer, and the Orchestrator's own ``end_session`` -- and each says a
    different subset. There is no field every one of them carries, so a
    required field here would be a lie about at least one caller.

    ``extra="allow"`` for the same reason: the Tutor puts its own ``kind`` on
    the chunk, and a model that rejected it would turn a documentation aid into
    a runtime failure. What this *does* buy is a typed home for the fields the
    client parses, so a caller passing ``segment_index="two"`` fails at the
    emitting agent rather than at a zod parse in a browser.
    """

    model_config = {"extra": "allow"}

    #: The ``session_turns`` row this call wrote.
    #:
    #: ``UUID`` as well as ``str`` because the SSE encoder already serialises
    #: either (``orchestration.streaming``'s json default), and a model that
    #: refused the raw form here would reject chunks the wire handles fine.
    turn_id: str | uuid.UUID | None = None
    segment_index: int | None = None
    anchor: str | None = None
    #: A :class:`~studium.orchestration.state_machine.State` value, when the
    #: turn moved the session. Most ``end`` chunks carry none (see F9).
    next_state: str | None = None
    primitive: str | None = None
    stance: str | None = None
    verdict: str | None = None
    refocus_concept_id: str | uuid.UUID | None = None
    #: The practice problem ``let_me_try_one`` selected, as the bench renders
    #: it. Learner-facing: the model answer and key points are stripped before
    #: this leaves the Orchestrator (see ``Orchestrator._absorb_end``).
    problem: dict[str, Any] | None = None
    note: str | None = None
    #: The ``content_artifacts`` row this turn produced, once the Orchestrator
    #: has applied the turn's effects. This is what lets the client call
    #: ``GET /api/artifacts/{id}/citations`` and resolve the segment's ``[Pn]``
    #: markers; without it every hover card in the product spins forever.
    #:
    #: Optional, not required: a Tutor turn produces no artifact, and declaring
    #: it required would make the common case the exception.
    artifact_id: str | uuid.UUID | None = None

    # v1.0.1 §4.2's remaining produced ids. Present only on the turns that
    # actually wrote the row -- see ``AppliedEffects.end_chunk_ids``, which
    # omits the absent ones rather than sending nulls. The client uses none of
    # these yet; they are here because the id exists only inside the effect
    # transaction, so a later feature that wants one cannot recover it from the
    # stream afterwards.
    journal_entry_id: str | uuid.UUID | None = None
    portfolio_item_id: str | uuid.UUID | None = None
    review_card_id: str | uuid.UUID | None = None
    queue_item_id: str | uuid.UUID | None = None

    #: §4.2: "for client-side reconciliation on reconnect".
    #:
    #: Optional rather than required as §4.2 writes it, for the reason the rest
    #: of this model is: a degraded turn emits an ``end`` chunk having written
    #: no turn row at all, and a required field would make the failure path
    #: unrepresentable.
    turn_index: int | None = None


class StreamChunk(BaseModel):
    """One unit of streamed output.

    ``kind`` is one of 'text', 'tool_effect', 'trace', 'end', or 'degraded'.
    The last carries learner-visible copy when a call failed past its retries;
    it is a normal chunk rather than an exception so the SSE stream closes
    cleanly with the learner told something coherent (§21).

    An ``end`` chunk's payload follows :class:`EndPayload`.
    """

    kind: Literal["text", "tool_effect", "trace", "end", "degraded"]
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def text_chunk(cls, text: str) -> StreamChunk:
        return cls(kind="text", payload={"text": text})

    @classmethod
    def effect(cls, effect: ToolEffect) -> StreamChunk:
        return cls(kind="tool_effect", payload=effect.model_dump())

    @classmethod
    def degraded(cls, message: str, *, reason: str) -> StreamChunk:
        return cls(kind="degraded", payload={"text": message, "reason": reason})

    @classmethod
    def ended(cls, **payload: Any) -> StreamChunk:
        """Build a terminal chunk, checking it against :class:`EndPayload`.

        The caller's dict is what goes on the wire, not the validated model's
        dump. Round-tripping through the model would add every unset field as
        an explicit null and change the shape of every ``end`` chunk in the
        product to buy nothing; validating and discarding catches the wrong
        *type* on a field that is present, which is the mistake worth catching.
        """
        EndPayload.model_validate(payload)
        return cls(kind="end", payload=payload)


class Agent(ABC):
    """Base class for the seven agents.

    ``identity`` matches a value of the ``agent_identity`` enum (data layer
    §6.0), which is what ties a trace row back to the agent that produced it.
    """

    identity: str
    #: Accepted ``AgentInput.kind`` values. Enforced by :meth:`check_kind`.
    kinds: frozenset[str] = frozenset()

    def __init__(self, client: AnthropicClient) -> None:
        self.client = client

    @abstractmethod
    async def handle(self, input: AgentInput) -> AgentOutput:
        """Non-streaming call. Returns fully-formed output."""

    async def handle_streaming(self, input: AgentInput) -> AsyncIterator[StreamChunk]:
        """Streaming call.

        Agents that stream (Lecturer, Tutor) override. The default refuses
        rather than silently falling back to a buffered call, which would look
        to the learner like the system stalling.
        """
        raise NotImplementedError(f"{self.identity} does not support streaming")
        yield  # pragma: no cover -- makes this an async generator for typing

    # --- shared helpers ----------------------------------------------------

    def check_kind(self, input: AgentInput) -> None:
        if input.kind not in self.kinds:
            raise AgentDispatchError(
                f"{self.identity} does not accept kind {input.kind!r}; "
                f"accepts {sorted(self.kinds)}"
            )

    def call_spec(
        self,
        input: AgentInput,
        *,
        prefix: Any,
        suffix: str,
        **overrides: Any,
    ) -> CallSpec:
        """Assemble the :class:`CallSpec` for a call from this agent.

        Pulls session, user, concept and exchange identity off the context so
        no agent has to remember which fields a trace needs.
        """
        ctx = input.session_context
        payload = input.payload
        return CallSpec(
            agent=self.identity,
            kind=input.kind,
            prefix=prefix,
            suffix=suffix,
            session_id=ctx.session_id,
            user_id=ctx.user_id,
            exchange_index=ctx.exchange_index,
            concept_id=ctx.focus_concept_id,
            model_override=payload.get("model_override"),
            override_reason=payload.get("override_reason"),
            langfuse_trace_id=str(ctx.session_id),
            **overrides,
        )

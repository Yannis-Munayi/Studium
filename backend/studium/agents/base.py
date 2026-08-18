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


class StreamChunk(BaseModel):
    """One unit of streamed output.

    ``kind`` is one of 'text', 'tool_effect', 'trace', 'end', or 'degraded'.
    The last carries learner-visible copy when a call failed past its retries;
    it is a normal chunk rather than an exception so the SSE stream closes
    cleanly with the learner told something coherent (§21).
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

"""Anthropic client wrapper: routing, streaming, structured output, tracing.

Agent runtime §18 (model routing) and §19 (cost accounting). Two invariants
this module exists to enforce:

1. **No agent picks its own model.** :func:`route` maps ``(agent, kind)`` to a
   model id. A per-call override is possible but must carry a reason, which
   flows to the trace -- so "why is this on Opus" is always answerable.
2. **No billable call escapes a trace.** The ``agent_traces`` write happens
   here, in the wrapper, not in the caller. §19 is explicit that making it a
   caller responsibility is how cost ledgers end up wrong.

The SDK's own retry loop is disabled (``max_retries=0``); retries belong to
:mod:`studium.llm.retries`, which owns the §21 policy and records attempts.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anthropic
from pydantic import BaseModel

from . import traces
from .models import HAIKU, OPUS, TTL, compute_cost, request_overrides, usage_from_response
from .prompts import CachedPrefix
from .retries import DegradedCall, with_retry
from .traces import TraceRecord

log = logging.getLogger(__name__)

#: §18. Keyed by agent, then by kind; ``None`` is the per-agent default.
#:
#: Haiku 4.5 carries every high-frequency, bounded call (classification,
#: single-criterion grading, gap monitoring); Opus 4.8 carries everything the
#: learner reads or that steers the curriculum.
ROUTING: dict[str, dict[str | None, str]] = {
    "orchestrator": {None: HAIKU.id},
    "curator": {None: OPUS.id},
    "lecturer": {None: OPUS.id},
    "tutor": {None: OPUS.id},
    "evaluator": {
        None: HAIKU.id,
        "grade_assessment": OPUS.id,
        # Evaluation §7.2's meta-grading. Opus, not Haiku: it judges qualities
        # the deterministic checks could not express -- Socratic discipline,
        # stance adherence, whether feedback names an actionable gap -- and a
        # cheap judge of those produces §19 open question 2's failure directly,
        # where the same output scores differently run to run and the
        # regression signal is noise. It runs ~20 times per dataset, off a
        # cached prefix, and not on a learner's turn.
        "meta_grade": OPUS.id,
    },
    "confusion_tracker": {None: HAIKU.id},
    "reviewer": {
        None: OPUS.id,
        "grade_response": HAIKU.id,
    },
}

#: Per-kind output ceilings. §10 caps a lecture segment at 400 words and §11
#: caps a Tutor turn at ~200; the ceilings sit well above those so a hard limit
#: never truncates mid-sentence, but far below the model maximum so a runaway
#: generation is bounded in both cost and latency.
MAX_TOKENS: dict[str, int] = {
    "classify_intent": 512,
    "evaluate_turn": 1024,
    "revise_hypothesis": 1024,
    "resolve_check": 1024,
    "generate_prompt": 1024,
    "grade_response": 1024,
    "check_partial": 1024,
    "grade_check": 2048,
    "grade_practice": 2048,
    "grade_assessment": 8192,
    # A score, a verdict sentence, and a few evidence quotes.
    "meta_grade": 2048,
    "next_topic": 2048,
    "select_stance": 1024,
    "open_session": 4096,
    "revise_syllabus": 4096,
    "summarize_session": 4096,
    "retrieval_check": 2048,
    "generate_check": 2048,
}
DEFAULT_MAX_TOKENS = 4096

#: §18 routes the Curator and Tutor to Opus because their output quality *is*
#: the product; effort is set to match. Kinds absent here run at the API
#: default. Haiku ignores effort entirely (models.request_overrides drops it).
EFFORT: dict[str, str] = {
    "deliver_segment": "high",
    "re_explain": "high",
    "worked_example": "high",
    "answer": "high",
    "open_tutorial": "high",
    "interruption_response": "high",
    "office_hours_response": "high",
    "remediation": "high",
    "grade_assessment": "high",
    "meta_grade": "high",
    "next_topic": "medium",
    "open_session": "medium",
    "generate_check": "medium",
}


class ModelRoutingError(KeyError):
    """No routing entry for an agent."""


def route(agent: str, kind: str | None = None) -> str:
    """Resolve ``(agent, kind)`` to a model id (§18)."""
    try:
        table = ROUTING[agent]
    except KeyError as exc:  # noqa: TRY003
        raise ModelRoutingError(f"no routing entry for agent {agent!r}") from exc
    return table.get(kind, table[None])


@dataclass(slots=True)
class CallSpec:
    """Everything one model call needs, assembled by the agent."""

    agent: str
    kind: str
    prefix: CachedPrefix
    suffix: str
    session_id: uuid.UUID
    user_id: uuid.UUID
    exchange_index: int = 0
    concept_id: uuid.UUID | None = None
    artifact_id: uuid.UUID | None = None
    primitive: str | None = None
    model_override: str | None = None
    override_reason: str | None = None
    max_tokens: int | None = None
    langfuse_trace_id: str | None = None
    #: Extra user-turn messages, for kinds that carry a conversation.
    history: Sequence[dict[str, Any]] = field(default_factory=tuple)

    def model(self) -> str:
        if self.model_override:
            if not self.override_reason:
                raise ValueError(
                    "model_override requires override_reason (§18: overrides are "
                    "'used sparingly, always with a reason tag that flows to the trace')"
                )
            return self.model_override
        return route(self.agent, self.kind)

    def token_ceiling(self) -> int:
        return self.max_tokens or MAX_TOKENS.get(self.kind, DEFAULT_MAX_TOKENS)

    def messages(self) -> list[dict[str, Any]]:
        return [*self.history, {"role": "user", "content": self.suffix}]


@dataclass(slots=True)
class CallResult:
    """A completed non-streaming call."""

    text: str
    structured: BaseModel | None
    record: TraceRecord
    turn_id: uuid.UUID


class AnthropicClient:
    """The single door to the model API.

    Every method writes a trace before returning. There is no path through this
    class that produces a billable call without one.
    """

    def __init__(self, sdk: Any | None = None) -> None:
        # max_retries=0: studium.llm.retries owns the §21 policy. Leaving the
        # SDK's default 2 in place would compound to 6 attempts on an outage.
        self._sdk = sdk or anthropic.AsyncAnthropic(max_retries=0)

    # --- structured / non-streaming ---------------------------------------

    async def parse(
        self,
        spec: CallSpec,
        output_format: type[BaseModel],
        *,
        thinking: bool = True,
    ) -> CallResult:
        """A structured-output call, validated against a Pydantic model (§3).

        Structured output is used wherever a downstream consumer -- the schema,
        another agent, the frontend -- has to make a decision on part of the
        output. Extracting it from prose after the fact is what this replaces.
        """
        model = spec.model()
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": spec.token_ceiling(),
            "system": spec.prefix.system_blocks(),
            "messages": spec.messages(),
            "output_format": output_format,
            **request_overrides(model, effort=EFFORT.get(spec.kind), thinking=thinking),
        }

        started = time.monotonic()

        async def call() -> Any:
            return await self._sdk.messages.parse(**kwargs)

        response = await with_retry(call, what=f"{spec.agent}.{spec.kind}")
        latency_ms = int((time.monotonic() - started) * 1000)

        text = _first_text(response)
        record = self._record(
            spec,
            model=model,
            kwargs=kwargs,
            response=response,
            completion=text,
            latency_ms=latency_ms,
            learner_visible_text="",
        )
        turn_id = await traces.write(record)
        return CallResult(
            text=text,
            structured=getattr(response, "parsed_output", None),
            record=record,
            turn_id=turn_id,
        )

    async def count_tokens(self, spec: CallSpec) -> int:
        """Prompt size for one call. Used by the budget gate's estimate path."""
        model = spec.model()
        response = await self._sdk.messages.count_tokens(
            model=model,
            system=spec.prefix.system_blocks(),
            messages=spec.messages(),
        )
        return int(response.input_tokens)

    # --- streaming ---------------------------------------------------------

    @asynccontextmanager
    async def stream(
        self, spec: CallSpec, *, thinking: bool = True
    ) -> AsyncIterator[ManagedStream]:
        """Open a streaming call.

        The trace is written on exit -- including when the caller stopped
        consuming early. An interrupted lecture still cost real tokens (§20:
        "the tokens emitted after the interrupt signal ... are wasted (paid
        for, not delivered)"), so it still gets a trace.
        """
        model = spec.model()
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": spec.token_ceiling(),
            "system": spec.prefix.system_blocks(),
            "messages": spec.messages(),
            **request_overrides(model, effort=EFFORT.get(spec.kind), thinking=thinking),
        }

        managed = ManagedStream(spec=spec, model=model, kwargs=kwargs)
        started = time.monotonic()
        try:
            async with self._sdk.messages.stream(**kwargs) as raw:
                managed.attach(raw)
                yield managed
        except anthropic.APIError as exc:
            raise DegradedCall(_classify_stream_error(exc), 1, exc) from exc
        finally:
            managed.latency_ms = int((time.monotonic() - started) * 1000)
            record = self._record(
                spec,
                model=model,
                kwargs=kwargs,
                response=managed.usage_source(),
                completion=managed.text,
                latency_ms=managed.latency_ms,
                learner_visible_text=managed.delivered_text,
            )
            managed.record = record
            managed.turn_id = await traces.write(record)

    # --- trace assembly ----------------------------------------------------

    def _record(
        self,
        spec: CallSpec,
        *,
        model: str,
        kwargs: dict[str, Any],
        response: Any,
        completion: str,
        latency_ms: int,
        learner_visible_text: str,
    ) -> TraceRecord:
        usage = usage_from_response(
            getattr(response, "usage", None),
            requested_ttl=_requested_ttl(kwargs),
        )
        tools_used: list[Any] = []
        if spec.override_reason:
            tools_used.append({"model_override_reason": spec.override_reason})

        return TraceRecord(
            agent=spec.agent,
            kind=spec.kind,
            model=model,
            session_id=spec.session_id,
            user_id=spec.user_id,
            prompt_messages=kwargs["messages"],
            system_prompt_hash=spec.prefix.sha256,
            completion=completion,
            usage=usage,
            latency_ms=latency_ms,
            cost_usd=compute_cost(model, usage),
            stop_reason=getattr(response, "stop_reason", None),
            tools_used=tools_used,
            langfuse_trace_id=spec.langfuse_trace_id,
            concept_id=spec.concept_id,
            artifact_id=spec.artifact_id,
            primitive=spec.primitive,
            exchange_index=spec.exchange_index,
            learner_visible_text=learner_visible_text,
        )


@dataclass(slots=True)
class ManagedStream:
    """A live stream plus the accounting that has to happen when it ends."""

    spec: CallSpec
    model: str
    kwargs: dict[str, Any]
    _raw: Any = None
    #: Everything the model generated, including tokens emitted after an
    #: interrupt. This is what was billed.
    text: str = ""
    #: What actually reached the learner. Diverges from ``text`` only on an
    #: interruption, and the gap is the §20 waste figure.
    delivered_text: str = ""
    final: Any = None
    latency_ms: int = 0
    record: TraceRecord | None = None
    turn_id: uuid.UUID | None = None

    def attach(self, raw: Any) -> None:
        self._raw = raw

    async def text_deltas(self) -> AsyncIterator[str]:
        """Yield text as it arrives, accumulating for the trace."""
        async for delta in self._raw.text_stream:
            self.text += delta
            yield delta
        self.final = await self._raw.get_final_message()

    def usage_source(self) -> Any:
        """The object the trace should read usage from.

        A stream the caller abandoned -- the interruption case, which §20
        expects to be common -- never reaches ``get_final_message``, so
        ``final`` is None. Reading zero usage there would record an interrupted
        lecture as *free*, understating spend exactly where §3 says there is no
        reconcile-later pathway.

        The SDK accumulates usage into ``current_message_snapshot`` as events
        arrive, so the partial call's real cost is available without consuming
        the tokens the learner already decided to skip. Falling back to the
        snapshot is what makes an interrupted turn cost what it actually cost.
        """
        if self.final is not None:
            return self.final
        return getattr(self._raw, "current_message_snapshot", None)

    def mark_delivered(self, text: str) -> None:
        """Record what the learner actually received.

        Called by the sentence-boundary iterator, which is the only thing that
        knows where an interrupted stream was cut.
        """
        self.delivered_text = text

    @property
    def wasted_tokens_estimate(self) -> int:
        """Characters generated but not delivered, as a token estimate.

        §20 budgets 20-80 tokens per interruption; this is how that projection
        gets checked against reality.
        """
        if not self.delivered_text or len(self.text) <= len(self.delivered_text):
            return 0
        return int((len(self.text) - len(self.delivered_text)) / 3.6)


def _requested_ttl(kwargs: dict[str, Any]) -> TTL | None:
    """Recover the TTL we asked for, to attribute an unsplit cache-write total."""
    for block in kwargs.get("system") or []:
        cache_control = block.get("cache_control") if isinstance(block, dict) else None
        if cache_control:
            return cache_control.get("ttl", "5m")
    return None


def _first_text(response: Any) -> str:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


def _classify_stream_error(exc: anthropic.APIError) -> str:
    from .retries import classify

    return classify(exc)

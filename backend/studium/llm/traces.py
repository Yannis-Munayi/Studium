"""The ``agent_traces`` writer and Langfuse bridge (agent runtime §19, §22).

§3: "Every LLM call is accounted for at the point of the call." The write is
part of :class:`studium.llm.client.AnthropicClient`, not a caller
responsibility, so there is no code path that produces a billable call without
a trace row.

**One turn row per LLM call.** ``agent_traces.session_turn_id`` is NOT NULL and
UNIQUE -- the data layer models a trace as one-to-one with a turn. §22's
Langfuse hierarchy shows four generations under a single ``turn_index``, which
that constraint cannot express. The resolution is the one the data layer's own
docstring points at: ``session_turns`` is "the message log: every LLM
interaction, learner input, tool invocation", so each LLM call gets its own
turn row and the persistent ``turn_index`` advances per call rather than per
learner-facing exchange. The learner-facing grouping survives as
``exchange_index`` in the turn's ``output`` JSONB and as the Langfuse span, so
"what did the learner see as one reply" is still answerable without a schema
change. See DIVERGENCES (R4); recorded as a data-layer v1.2 candidate.

Langfuse is optional. Where the SDK is absent or unconfigured every hook here
is a no-op, because a missing observability backend must not be able to fail a
learner's turn. Configuration belongs to the Infrastructure spec (§22).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from studium.asyncdb import jsonable, run_db
from studium.models import AgentTrace, SessionTurn
from studium.queries import next_turn_index

from .models import TokenUsage

log = logging.getLogger(__name__)


@dataclass(slots=True)
class TraceRecord:
    """One LLM call, as persisted.

    Mirrors ``agent_traces`` (data layer §6.6) plus the turn fields needed to
    write both rows in one transaction.
    """

    agent: str
    model: str
    session_id: uuid.UUID
    user_id: uuid.UUID
    prompt_messages: list[Any]
    system_prompt_hash: str
    completion: str
    usage: TokenUsage
    latency_ms: int
    cost_usd: Decimal
    kind: str = ""
    stop_reason: str | None = None
    tools_used: list[Any] = field(default_factory=list)
    server_tool_use: dict[str, Any] = field(default_factory=dict)
    langfuse_trace_id: str | None = None
    concept_id: uuid.UUID | None = None
    artifact_id: uuid.UUID | None = None
    primitive: str | None = None
    #: Which learner-facing exchange this call belongs to. Carries the grouping
    #: §22's Langfuse hierarchy shows, which turn_index can no longer express
    #: now that it advances per call.
    exchange_index: int = 0
    #: Text shown to the learner, when this call produced learner-visible
    #: output. Empty for internal calls (intent classification, the tracker).
    learner_visible_text: str = ""
    #: Populated after the write, so callers can attach ToolEffects and review
    #: queue items to the right turn.
    session_turn_id: uuid.UUID | None = None

    @property
    def cache_hit_rate(self) -> float:
        return self.usage.cache_hit_rate


def _write_sync(session: Session, record: TraceRecord) -> uuid.UUID:
    """Write the turn and its trace in one transaction."""
    turn = SessionTurn(
        session_id=record.session_id,
        turn_index=next_turn_index(session, record.session_id),
        actor=record.agent,
        input={
            "kind": record.kind,
            "system_prompt_hash": record.system_prompt_hash,
            "exchange_index": record.exchange_index,
        },
        output=jsonable(
            {
                "text": record.learner_visible_text,
                "model": record.model,
                "stop_reason": record.stop_reason,
                "cache_hit_rate": round(record.cache_hit_rate, 4),
            }
        ),
        primitive=record.primitive,
        concept_id=record.concept_id,
        artifact_id=record.artifact_id,
        latency_ms=record.latency_ms,
    )
    session.add(turn)
    session.flush()  # assign turn.id

    session.add(
        AgentTrace(
            session_turn_id=turn.id,
            user_id=record.user_id,
            agent=record.agent,
            model=record.model,
            prompt_messages=jsonable(record.prompt_messages),
            system_prompt_hash=record.system_prompt_hash,
            completion=record.completion,
            tools_used=jsonable(record.tools_used),
            stop_reason=record.stop_reason,
            tokens_in=record.usage.tokens_in,
            tokens_out=record.usage.tokens_out,
            cache_read_tokens=record.usage.cache_read_tokens,
            cache_write_5m_tokens=record.usage.cache_write_5m_tokens,
            cache_write_1h_tokens=record.usage.cache_write_1h_tokens,
            server_tool_use=jsonable(record.server_tool_use),
            latency_ms=record.latency_ms,
            cost_usd=record.cost_usd,
            langfuse_trace_id=record.langfuse_trace_id,
        )
    )
    return turn.id


async def write(record: TraceRecord) -> uuid.UUID:
    """Persist a trace and return its ``session_turns.id``.

    Failures propagate. A trace that cannot be written means a billable call
    went unaccounted for, and §3 has no "reconcile later" pathway -- the caller
    degrades the turn rather than proceeding as if the call were free.
    """
    turn_id = await run_db(lambda s: _write_sync(s, record))
    record.session_turn_id = turn_id
    _emit_langfuse(record)
    return turn_id


def write_learner_turn_sync(
    session: Session,
    *,
    session_id: uuid.UUID,
    text: str,
    intent: str,
    exchange_index: int,
    primitive: str | None = None,
    concept_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Record the learner's own input. No trace: no model call happened.

    ``agent_traces`` carries a CHECK that ``agent <> 'learner'``, so this is the
    only turn shape with no accompanying trace row.
    """
    turn = SessionTurn(
        session_id=session_id,
        turn_index=next_turn_index(session, session_id),
        actor="learner",
        input={
            "kind": "primitive" if primitive else "utterance",
            "text": text,
            "intent": intent,
            "exchange_index": exchange_index,
        },
        output={},
        primitive=primitive,
        concept_id=concept_id,
    )
    session.add(turn)
    session.flush()
    return turn.id


# --- Langfuse (optional) ---------------------------------------------------

_langfuse_client: Any = None
_langfuse_checked = False


def _langfuse() -> Any:
    """Resolve a Langfuse client once, tolerating its absence."""
    global _langfuse_client, _langfuse_checked
    if _langfuse_checked:
        return _langfuse_client
    _langfuse_checked = True
    try:
        from langfuse import Langfuse  # type: ignore[import-not-found]

        _langfuse_client = Langfuse()
        log.info("Langfuse tracing enabled")
    except Exception as exc:  # noqa: BLE001 -- absence and misconfiguration both fine
        log.debug("Langfuse tracing disabled (%s)", exc)
        _langfuse_client = None
    return _langfuse_client


def _emit_langfuse(record: TraceRecord) -> None:
    """Mirror the trace into Langfuse. Never raises.

    Metadata matches §22: agent, kind, session, user, concept, cache hit rate,
    cost, latency, stop reason.
    """
    client = _langfuse()
    if client is None:
        return
    try:
        client.generation(
            name=f"{record.agent}.{record.kind}" if record.kind else record.agent,
            trace_id=record.langfuse_trace_id or str(record.session_id),
            model=record.model,
            usage={
                "input": record.usage.tokens_in,
                "output": record.usage.tokens_out,
                "cache_read_input_tokens": record.usage.cache_read_tokens,
            },
            metadata={
                "agent": record.agent,
                "kind": record.kind,
                "session_id": str(record.session_id),
                "user_id": str(record.user_id),
                "focus_concept_id": str(record.concept_id) if record.concept_id else None,
                "exchange_index": record.exchange_index,
                "cache_hit_rate": round(record.cache_hit_rate, 4),
                "cost_usd": str(record.cost_usd),
                "latency_ms": record.latency_ms,
                "stop_reason": record.stop_reason,
            },
        )
    except Exception:  # noqa: BLE001
        # Observability must never fail a learner's turn.
        log.debug("Langfuse emit failed", exc_info=True)

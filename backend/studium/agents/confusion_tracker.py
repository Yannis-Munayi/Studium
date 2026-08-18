"""The Confusion-Tracker: asynchronous gap monitoring (agent runtime §13).

Never appears to the learner. Watches completed turns and decides whether a
pattern of confusion warrants a journal entry the Tutor should pick up later --
the point being patterns a single turn does not reveal.

Runs off the critical path. The Orchestrator fires it after a turn completes
and does not wait, so a slow or failed tracker call costs the learner nothing.
Its decisions are applied as ``ToolEffect``s in a background task.

**Its cached prefix probably does not cache.** §17 gives this agent a 1-hour
TTL, but it routes to Haiku 4.5 (§18, because it runs on every learner turn and
cost sensitivity is high) and Haiku 4.5's minimum cacheable prefix is 4096
tokens. A per-concept tracker prefix is typically 600-900 tokens, well under
that floor -- so the marker is accepted and nothing caches, and every call pays
full input price. :meth:`CachedPrefix.will_cache` reports this and the prefix
builder omits the marker rather than paying a doomed cache-write premium. The
projected saving in §17 does not apply to this agent. See DIVERGENCES (R2).
"""

from __future__ import annotations

import logging
from typing import Any

from studium.acl import wrap_user_content
from studium.llm.prompts import build_prefix, fmt_float

from .base import Agent, AgentInput, AgentOutput, ToolEffect
from .schemas import TrackerDecision

log = logging.getLogger(__name__)


class ConfusionTracker(Agent):
    identity = "confusion_tracker"
    kinds = frozenset({"evaluate_turn", "revise_hypothesis", "resolve_check"})

    async def handle(self, input: AgentInput) -> AgentOutput:
        self.check_kind(input)
        ctx = input.session_context

        prefix = build_prefix("confusion_tracker", ctx, model=self._model(input))
        spec = self.call_spec(input, prefix=prefix, suffix=self._suffix(input))
        result = await self.client.parse(spec, TrackerDecision, thinking=False)
        decision: TrackerDecision = result.structured  # type: ignore[assignment]

        effects: list[ToolEffect] = []
        if not decision.is_valid():
            # §13 states per-action required fields that the output schema
            # cannot express as a conditional. An entry with a null summary is
            # worse than no entry -- it shows the learner a blank journal card.
            log.warning(
                "tracker returned an invalid %s decision on session %s; dropping",
                decision.action, ctx.session_id,
            )
        elif decision.action != "none":
            effects.append(self._journal_effect(input, decision))

        return AgentOutput(
            text="",  # never learner-visible
            structured=decision,
            trace=result.record,
            turn_id=result.turn_id,
            tool_effects=effects,
        )

    def _journal_effect(self, input: AgentInput, decision: TrackerDecision) -> ToolEffect:
        ctx = input.session_context
        return ToolEffect(
            kind="update_journal",
            payload={
                "action": decision.action,
                "entry_id": str(decision.entry_id) if decision.entry_id else None,
                "user_id": str(ctx.user_id),
                "learner_subject_id": str(ctx.learner_subject_id),
                "concept_id": str(ctx.focus_concept_id) if ctx.focus_concept_id else None,
                "session_id": str(ctx.session_id),
                "summary": decision.summary,
                "hypothesis": decision.hypothesis,
                "origin": decision.origin,
                "reasoning": decision.reasoning,
            },
        )

    def _suffix(self, input: AgentInput) -> str:
        ctx = input.session_context
        payload = input.payload

        turn = payload.get("turn") or {}
        turn_text = (
            f"actor={turn.get('actor')}, kind={turn.get('kind')}\n"
            f"learner said: {wrap_user_content(str(turn.get('input', '')))}\n"
            f"system replied: {str(turn.get('output', ''))[:1500]}"
        )

        prior = "\n".join(
            f"[{t.get('actor')}] {_turn_text(t)}" for t in ctx.recent_turns[-8:]
        ) or "(no prior turns)"

        entries = "\n".join(
            f"- id={e.get('id')} status={e.get('status')} summary={e.get('summary', '')} "
            f"hypothesis={e.get('hypothesis', '')}"
            for e in ctx.journal_entries_for_focus()
        ) or "(none open)"

        task = {
            "evaluate_turn": (
                "Decide whether this turn warrants a new journal entry, a "
                "hypothesis revision on an existing entry, or no action. Most "
                "turns warrant no action -- say so rather than manufacturing a gap."
            ),
            "revise_hypothesis": (
                "New evidence has arrived on an existing entry. Refine its "
                "hypothesis to account for the evidence, or leave it if the "
                "evidence does not change the picture."
            ),
            "resolve_check": (
                "This entry looks resolved. Verify the underlying gap is actually "
                "closed rather than merely quiet -- a learner avoiding a topic "
                "produces the same silence as a learner who has mastered it."
            ),
        }[input.kind]

        return f"""Turn under evaluation:
{turn_text}

Prior turns (last 8):
{prior}

Open journal entries for this learner+concept:
{entries}

Current mastery: {fmt_float(ctx.focus_mastery)}

Task: {task}"""

    def _model(self, input: AgentInput) -> str:
        from studium.llm.client import route

        return input.payload.get("model_override") or route(self.identity, input.kind)


def _turn_text(turn: dict[str, Any]) -> str:
    output = turn.get("output") or {}
    if isinstance(output, dict) and output.get("text"):
        return str(output["text"])
    payload = turn.get("input") or {}
    return str(payload.get("text", "")) if isinstance(payload, dict) else ""

"""The Curator: curriculum sequencing (agent runtime §9).

Answers three questions -- what is next, what is the plan, how should this
session open. It does not teach; it decides what teaching happens next.

The safety net matters more than the model here. A Curator that picks a locked
concept would put a learner in front of material they cannot follow, so every
``next_topic`` is post-processed against the deterministic unlock check, retried
once with the prerequisites spelled out, and then abandoned in favour of
``mastery.suggest_next_unlocked``. Two failures on one session flag the trace.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from studium.asyncdb import read_db
from studium.graph import unlock_status
from studium.llm.prompts import build_prefix, fmt_float
from studium.mastery import suggest_next_unlocked
from studium.session.context import SessionContext

from .base import Agent, AgentInput, AgentOutput, ToolEffect
from .schemas import (
    NextTopic,
    PracticeProblem,
    SessionOpening,
    SessionSummary,
    StanceChoice,
)

log = logging.getLogger(__name__)


class Curator(Agent):
    identity = "curator"
    kinds = frozenset(
        {
            "open_session",
            "next_topic",
            "revise_syllabus",
            "select_stance",
            "summarize_session",
            "select_practice",
        }
    )

    async def handle(self, input: AgentInput) -> AgentOutput:
        self.check_kind(input)
        handler = {
            "open_session": self._open_session,
            "next_topic": self._next_topic,
            "revise_syllabus": self._revise_syllabus,
            "select_stance": self._select_stance,
            "summarize_session": self._summarize_session,
            "select_practice": self._select_practice,
        }[input.kind]
        return await handler(input)

    # --- prompt assembly ---------------------------------------------------

    def _prefix(self, ctx: SessionContext, model: str) -> Any:
        return build_prefix("curator", ctx, model=model)

    def _suffix(self, ctx: SessionContext, task: str) -> str:
        """The per-call suffix (§9).

        Mastery is rendered ascending -- weakest first -- because that is the
        order the decision principles read in: the Curator is looking for what
        needs work, not what is already solid.
        """
        mastery_lines = "\n".join(
            f"- {slug}: {fmt_float(score)}"
            for slug, score in self._mastery_by_slug(ctx)
        ) or "(no mastery evidence yet)"

        journal_lines = "\n".join(
            f"- [{e.get('concept_slug', 'unknown concept')}] {e.get('summary', '')} "
            f"(first seen {e.get('first_seen_at', 'unknown')})"
            for e in ctx.open_journal_entries[:8]
        ) or "(no open journal entries)"

        check = ctx.retrieval_check_result
        check_text = (
            f"overall_score={fmt_float(check.get('overall_score'))}; "
            f"weak_prompts={check.get('weak_prompts', [])}"
            if check
            else "(no retrieval check this session)"
        )

        return f"""Current session mode: {ctx.mode}
Session duration remaining: {ctx.minutes_remaining()} minutes

CURRENT MASTERY SNAPSHOT (concept -> p_known_decayed)
{mastery_lines}

RECENT CONFUSION (open journal entries)
{journal_lines}

RETRIEVAL CHECK RESULT (if opening a session with prior context)
{check_text}

Your task: {task}"""

    def _mastery_by_slug(self, ctx: SessionContext) -> list[tuple[str, float]]:
        by_id = {str(c["id"]): c["slug"] for c in ctx.subject_concepts}
        pairs = [
            (by_id.get(cid, cid), score) for cid, score in ctx.mastery_snapshot.items()
        ]
        return sorted(pairs, key=lambda p: (p[1], p[0]))

    # --- kinds -------------------------------------------------------------

    async def _open_session(self, input: AgentInput) -> AgentOutput:
        ctx = input.session_context
        prior = ctx.prior_session_summary
        prior_text = (
            f"Last session covered: {prior.get('summary', '')}\n"
            f"Key points: {prior.get('key_points', [])}\n"
            f"Open threads: {prior.get('open_threads', [])}"
            if prior
            else "(no prior session within the last 7 days)"
        )

        task = (
            "Assemble this session's opening. Choose the focus concept, write a "
            "3-6 item learner-visible agenda, and say in one or two sentences what "
            "to tell the learner about last time. If there is prior context, "
            "produce 2-3 retrieval-check prompts drawn from it.\n\n"
            f"PRIOR SESSION\n{prior_text}"
        )

        spec = self.call_spec(
            input,
            prefix=self._prefix(ctx, self._model(input)),
            suffix=self._suffix(ctx, task),
        )
        result = await self.client.parse(spec, SessionOpening)
        opening: SessionOpening = result.structured  # type: ignore[assignment]

        return AgentOutput(
            text=self._agenda_text(opening),
            structured=opening,
            trace=result.record,
            turn_id=result.turn_id,
        )

    def _agenda_text(self, opening: SessionOpening) -> str:
        lines = []
        if opening.prior_session_note:
            lines.append(opening.prior_session_note)
        if opening.session_agenda:
            lines.append("Today:")
            lines.extend(f"  {i}. {item}" for i, item in enumerate(opening.session_agenda, 1))
        return "\n".join(lines)

    async def _next_topic(self, input: AgentInput) -> AgentOutput:
        """Pick the next concept, then verify it is actually unlocked (§9).

        The model is asked once. If its choice is locked it is asked again with
        the unmet prerequisites named. If the second choice is also locked, the
        deterministic fallback decides and the trace is flagged -- §9 wants two
        failures in a row visible to a reviewer, because that is a prompt
        problem, not a one-off.
        """
        ctx = input.session_context
        effects: list[ToolEffect] = []

        task = (
            "Given current mastery, pick the next concept to work on. Return its "
            "concept_id, the mode to work in, and the pedagogical stance."
        )
        spec = self.call_spec(
            input,
            prefix=self._prefix(ctx, self._model(input)),
            suffix=self._suffix(ctx, task),
        )
        result = await self.client.parse(spec, NextTopic)
        choice: NextTopic = result.structured  # type: ignore[assignment]

        unmet = await self._unmet_prerequisites(ctx, choice.concept_id)
        if unmet:
            log.info(
                "curator picked locked concept %s (unmet: %s); retrying",
                choice.concept_id, unmet,
            )
            retry_spec = self.call_spec(
                input,
                prefix=self._prefix(ctx, self._model(input)),
                suffix=self._suffix(ctx, task)
                + (
                    f"\n\nYour previous choice was locked. Concept "
                    f"{choice.concept_id} has unmet prerequisites: "
                    f"{', '.join(unmet)}. Pick a concept whose prerequisites are "
                    f"all above 0.85, or pick one of those prerequisites."
                ),
            )
            result = await self.client.parse(retry_spec, NextTopic)
            choice = result.structured  # type: ignore[assignment]
            unmet = await self._unmet_prerequisites(ctx, choice.concept_id)

        if unmet:
            fallback = await read_db(
                lambda s: suggest_next_unlocked(s, ctx.learner_subject_id)
            )
            log.warning(
                "curator failed the unlock check twice on session %s; "
                "falling back to %s",
                ctx.session_id, fallback,
            )
            effects.append(
                ToolEffect(
                    kind="flag_for_review",
                    payload={
                        "source": "system_confidence",
                        "severity": 2,
                        "reason": (
                            "Curator produced a locked next_topic twice in one "
                            f"session; unmet prerequisites {unmet}. §9 treats two "
                            "consecutive failures as a prompt defect."
                        ),
                    },
                )
            )
            if fallback is not None:
                choice = NextTopic(
                    concept_id=fallback,
                    mode="lecture",
                    stance="default",
                    reason="deterministic fallback: lowest-depth unlocked concept",
                )

        return AgentOutput(
            text=choice.reason,
            structured=choice,
            trace=result.record,
            turn_id=result.turn_id,
            tool_effects=effects,
        )

    async def _unmet_prerequisites(
        self, ctx: SessionContext, concept_id: uuid.UUID
    ) -> list[str]:
        """Names of prerequisites the learner has not cleared.

        Names rather than ids: the retry prompt is more useful to the model
        when it says ``beta-reduction`` than a UUID it cannot resolve.
        """

        def query(session: Any) -> list[str]:
            status = unlock_status(
                session,
                learner_subject_id=ctx.learner_subject_id,
                concept_id=concept_id,
            )
            if status.unlocked:
                return []
            from studium.graph import prerequisites

            required = prerequisites(session, concept_id)
            by_id = {str(c["id"]): c["slug"] for c in ctx.subject_concepts}
            return sorted(
                by_id.get(str(p), str(p))
                for p in required
                if ctx.mastery_of(p) < 0.85
            )

        return await read_db(query)

    async def _select_stance(self, input: AgentInput) -> AgentOutput:
        ctx = input.session_context
        existing = input.payload.get("existing_artifacts", [])
        task = (
            "Choose the pedagogical stance for the next Lecturer call on the "
            "focus concept, and decide whether to reuse an existing artifact or "
            "regenerate. Consider artifact age, review status, and how this "
            "learner has responded so far.\n\n"
            f"EXISTING ARTIFACTS: {existing or '(none)'}"
        )
        spec = self.call_spec(
            input,
            prefix=self._prefix(ctx, self._model(input)),
            suffix=self._suffix(ctx, task),
        )
        result = await self.client.parse(spec, StanceChoice)
        return AgentOutput(
            text=result.text,
            structured=result.structured,
            trace=result.record,
            turn_id=result.turn_id,
        )

    async def _select_practice(self, input: AgentInput) -> AgentOutput:
        """Pick a practice problem at the learner's current level (§15)."""
        ctx = input.session_context
        task = (
            "Select a practice problem for the focus concept, pitched at this "
            "learner's current mastery: hard enough to be worth doing, not so "
            "hard it needs a prerequisite they have not met. Provide the problem, "
            "a model answer, the key points a correct answer must contain, and "
            "one hint to release if they stall."
        )
        spec = self.call_spec(
            input,
            prefix=self._prefix(ctx, self._model(input)),
            suffix=self._suffix(ctx, task),
        )
        result = await self.client.parse(spec, PracticeProblem)
        problem: PracticeProblem = result.structured  # type: ignore[assignment]
        return AgentOutput(
            text=problem.prompt,
            structured=problem,
            trace=result.record,
            turn_id=result.turn_id,
        )

    async def _revise_syllabus(self, input: AgentInput) -> AgentOutput:
        ctx = input.session_context
        task = (
            "Mastery has moved significantly. Regenerate this learner's ordered "
            "plan through the subject: return the next concept, mode, and stance "
            "for the head of the revised plan."
        )
        spec = self.call_spec(
            input,
            prefix=self._prefix(ctx, self._model(input)),
            suffix=self._suffix(ctx, task),
        )
        result = await self.client.parse(spec, NextTopic)
        return AgentOutput(
            text=result.text,
            structured=result.structured,
            trace=result.record,
            turn_id=result.turn_id,
        )

    async def _summarize_session(self, input: AgentInput) -> AgentOutput:
        """§16 closing step 2. Written to ``session_summaries``."""
        ctx = input.session_context
        transcript = "\n".join(
            f"[{t.get('actor')}] {_turn_text(t)}" for t in ctx.recent_turns
        ) or "(no turns recorded)"

        task = (
            "Summarise this session for the learner's next one: what was covered, "
            "what was mastered, what remains open, what to expect next time. Key "
            "points should be specific enough to build a retrieval check from.\n\n"
            f"SESSION TRANSCRIPT (most recent turns)\n{transcript}"
        )
        spec = self.call_spec(
            input,
            prefix=self._prefix(ctx, self._model(input)),
            suffix=self._suffix(ctx, task),
        )
        result = await self.client.parse(spec, SessionSummary)
        summary: SessionSummary = result.structured  # type: ignore[assignment]
        return AgentOutput(
            text=summary.summary,
            structured=summary,
            trace=result.record,
            turn_id=result.turn_id,
        )

    def _model(self, input: AgentInput) -> str:
        from studium.llm.client import route

        return input.payload.get("model_override") or route(self.identity, input.kind)


def _turn_text(turn: dict[str, Any]) -> str:
    output = turn.get("output") or {}
    if isinstance(output, dict) and output.get("text"):
        return str(output["text"])
    payload = turn.get("input") or {}
    return str(payload.get("text", "")) if isinstance(payload, dict) else ""

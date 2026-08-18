"""The Tutor: Socratic dialogue (agent runtime §11).

"This is the heart of the product ... its behavior distinguishes Studium from
every chatbot with a system prompt that has come before."

The diagnostic sequence lives in the cached prefix (§17) because it is the same
on every turn; what changes per turn is the learner's utterance, their mastery,
and the open journal entries the Tutor is meant to be working against. Each
tutorial primitive contributes only its own task instruction to the suffix --
one prompt template per primitive would multiply the prefix by eight and
destroy the cache the whole design depends on.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from studium.acl import wrap_user_content
from studium.llm.prompts import build_prefix, fmt_float
from studium.retrieval import DEFAULT_K, PassageRetriever
from studium.session.context import Passage

from .base import Agent, AgentInput, AgentOutput, StreamChunk
from .schemas import PRIMITIVE_NAMES, TutorDiagnostic, VocabularyVerdict

log = logging.getLogger(__name__)

#: §11's kinds, plus one per primitive.
BASE_KINDS = frozenset(
    {
        "open_tutorial",
        "answer",
        "interruption_response",
        "office_hours_response",
        "remediation",
    }
)
PRIMITIVE_KINDS = frozenset(f"primitive:{name}" for name in PRIMITIVE_NAMES)

#: Primitives whose first move is a structured decision rather than prose.
#: ``explain_differently`` must choose a stance for the Lecturer;
#: ``vocabulary_check`` must decide terminology-vs-substance before it knows
#: which kind of reply to write.
STRUCTURED_PRIMITIVES: dict[str, type] = {
    "primitive:explain_differently": TutorDiagnostic,
    "primitive:vocabulary_check": VocabularyVerdict,
}

#: §11: an interruption answer is bounded unless the learner escalates.
INTERRUPTION_WORD_LIMIT = 150

#: Per-primitive task instructions (§15). Appended to the shared suffix.
PRIMITIVE_TASKS: dict[str, str] = {
    "explain_differently": (
        "The learner says the current explanation is not landing. First find out "
        "what specifically is not landing -- the notation, a step, the motivation, "
        "the scope -- then choose the stance a re-explanation should take. Ask "
        "the diagnostic question in your reply; return the stance in the "
        "structured field."
    ),
    "prove_it_to_me": (
        "The learner wants justification. Do not hand them the derivation. Ask "
        "what they would expect the argument to look like and where they think it "
        "would start. If they genuinely cannot begin, walk the first step only, "
        "then hand it back."
    ),
    "where_does_this_fit": (
        "Place this concept in its neighbourhood. Name what it depends on, what "
        "depends on it, and -- concretely -- what it lets them do that they could "
        "not do before. Render the relationships as text; the client may draw a "
        "graph fragment from the concept ids."
    ),
    "vocabulary_check": (
        "Decide whether the learner is stuck on a word or on the idea behind it. "
        "If terminology: give the paraphrase, plainly, and move on. If substance: "
        "do not paraphrase -- open a Socratic sub-thread on the idea itself. "
        "Return which it was in the structured field."
    ),
    "show_worked_example": (
        "Hand off to the Lecturer for a full worked example. Say in one sentence "
        "what you are about to show them and why this example."
    ),
    "let_me_try_one": (
        "The learner wants to attempt a problem. Say in one sentence what they "
        "are about to try and what a good answer will demonstrate. The Curator is "
        "selecting the problem; do not invent one."
    ),
    "why_does_this_matter": (
        "Explain downstream utility. Name the specific thing this concept unlocks "
        "-- a problem they could not previously state, a proof they could not "
        "previously follow, a program they could not previously write. Avoid "
        "generalities about the field's importance."
    ),
    "im_lost": (
        "The learner has lost the thread. Do not re-explain. Ask what the last "
        "thing was that made sense, and wait. The Curator will reset the focus "
        "from their answer."
    ),
}


class Tutor(Agent):
    identity = "tutor"
    kinds = BASE_KINDS | PRIMITIVE_KINDS

    def __init__(self, client: Any, retriever: PassageRetriever) -> None:
        super().__init__(client)
        self.retriever = retriever

    async def handle(self, input: AgentInput) -> AgentOutput:
        """Non-streaming: the two primitives that begin with a decision."""
        self.check_kind(input)
        schema = STRUCTURED_PRIMITIVES.get(input.kind)
        if schema is None:
            raise ValueError(
                f"{input.kind!r} streams; call handle_streaming instead"
            )

        prefix = await self._prefix(input)
        spec = self.call_spec(
            input,
            prefix=prefix,
            suffix=self._suffix(input),
            primitive=_primitive_of(input.kind),
        )
        result = await self.client.parse(spec, schema)
        return AgentOutput(
            text=result.text,
            structured=result.structured,
            trace=result.record,
            turn_id=result.turn_id,
        )

    async def handle_streaming(self, input: AgentInput) -> AsyncIterator[StreamChunk]:
        self.check_kind(input)
        if input.kind in STRUCTURED_PRIMITIVES:
            raise ValueError(f"{input.kind!r} is non-streaming; call handle instead")

        prefix = await self._prefix(input)
        spec = self.call_spec(
            input,
            prefix=prefix,
            suffix=self._suffix(input),
            primitive=_primitive_of(input.kind),
        )

        async with self.client.stream(spec) as stream:
            async for delta in stream.text_deltas():
                yield StreamChunk.text_chunk(delta)

        yield StreamChunk.ended(
            turn_id=str(stream.turn_id) if stream.turn_id else None,
            kind=input.kind,
        )

    # --- prompt assembly ---------------------------------------------------

    async def _prefix(self, input: AgentInput) -> Any:
        ctx = input.session_context
        passages: list[Passage] = ctx.passages
        if not passages and ctx.focus_concept_id is not None:
            passages = await self.retriever.retrieve_passages(
                ctx.focus_concept_id, k=DEFAULT_K
            )
        return build_prefix(
            "tutor",
            ctx.model_copy(update={"passages": passages}),
            model=self._model(input),
        )

    def _suffix(self, input: AgentInput) -> str:
        ctx = input.session_context

        journal = "\n".join(
            f"- {e.get('summary', '')} (hypothesis: {e.get('hypothesis', '')}; "
            f"first seen {e.get('first_seen_at', 'unknown')})"
            for e in ctx.journal_entries_for_focus()[:5]
        ) or "(none open for this concept)"

        turns = "\n".join(
            f"[{t.get('actor')}] {_turn_text(t)}" for t in ctx.recent_turns[-6:]
        ) or "(this is the first turn)"

        return f"""Mode: {ctx.mode}
Current concept mastery: {fmt_float(ctx.focus_mastery)} (0=unknown, 1=known cold)
Open journal entries for this concept:
{journal}

Recent session turns (last 6):
{turns}

Task: {self._task(input)}"""

    def _task(self, input: AgentInput) -> str:
        payload = input.payload
        utterance = wrap_user_content(str(payload.get("utterance", "")))

        if input.kind.startswith("primitive:"):
            name = _primitive_of(input.kind) or ""
            task = PRIMITIVE_TASKS[name]
            if utterance.strip():
                task = f"{task}\n\nThe learner also said: {utterance}"
            return task

        if input.kind == "open_tutorial":
            return (
                "Open a tutorial on this concept. Begin with a question that "
                "reveals where they actually are -- not a definition, and not a "
                "summary of what you are about to cover."
            )

        if input.kind == "interruption_response":
            delivered = payload.get("lecture_context", "")[-800:]
            return (
                f"The learner raised their hand mid-lecture and said: {utterance}\n\n"
                f"The lecture had just delivered:\n{delivered}\n\n"
                f"Answer per the diagnostic sequence, in under "
                f"{INTERRUPTION_WORD_LIMIT} words. They are waiting to get back to "
                f"the lecture; do not open a new thread unless their question "
                f"genuinely requires one."
            )

        if input.kind == "office_hours_response":
            return (
                f"This is office hours -- learner-driven, no lecture to return to. "
                f"They said: {utterance}\n\nFollow where they want to go, but keep "
                f"the diagnostic sequence running underneath."
            )

        if input.kind == "remediation":
            attempts = payload.get("failed_attempts", 2)
            return (
                f"The learner has failed this practice problem {attempts} times. "
                f"Take over from the lab. Do not give them the answer. Find the "
                f"specific step where their model of the concept breaks, and work "
                f"on that step alone.\n\nTheir most recent attempt: {utterance}"
            )

        # 'answer'
        return f"The learner just said: {utterance}\n\nRespond per the diagnostic sequence."

    def _model(self, input: AgentInput) -> str:
        from studium.llm.client import route

        return input.payload.get("model_override") or route(self.identity, input.kind)


def _primitive_of(kind: str) -> str | None:
    return kind.split(":", 1)[1] if kind.startswith("primitive:") else None


def _turn_text(turn: dict[str, Any]) -> str:
    output = turn.get("output") or {}
    if isinstance(output, dict) and output.get("text"):
        return str(output["text"])
    payload = turn.get("input") or {}
    return str(payload.get("text", "")) if isinstance(payload, dict) else ""

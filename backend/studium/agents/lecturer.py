"""The Lecturer: structured exposition (agent runtime §10).

Works from a concept node, pulls source material through ``retrieve_passages``,
and streams a segment grounded in the corpus. Every non-trivial claim carries a
``[Pn]`` marker; those markers are resolved back to ``source_chunks`` rows when
the segment is persisted, which is what makes the grounding claim checkable
rather than decorative.

Segments are persisted as ``content_artifacts`` precisely so they can be
reused: the next learner on the same concept at the same stance reads the
cached artifact instead of paying for regeneration (§10).
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any

from studium.llm.prompts import build_prefix, fmt_float, sorted_passages
from studium.retrieval import DEFAULT_K, PassageRetriever, grounding_is_thin
from studium.session.context import Passage, SessionContext

from .base import Agent, AgentInput, AgentOutput, StreamChunk, ToolEffect
from .schemas import ComprehensionCheck

log = logging.getLogger(__name__)

#: Matches [P3] and [P7-P8]. Ranges are inclusive, per §10's example.
CITATION_PATTERN = re.compile(r"\[P(\d+)(?:\s*-\s*P?(\d+))?\]")

STREAMING_KINDS = frozenset({"deliver_segment", "re_explain", "worked_example"})


def resolve_citations(text: str, passages: Sequence[Passage]) -> list[Passage]:
    """Map ``[Pn]`` markers in generated text back to the passages they cite.

    The numbering must match how the prefix rendered them, which is
    ``sorted_passages`` order (§17 sorts by ``chunk_id`` for byte stability).
    Resolving against retrieval order instead would attach every citation to
    the wrong chunk -- silently, since both lists are the same length.

    Out-of-range markers are dropped rather than raising: a model that invents
    ``[P9]`` when six passages were supplied has produced an ungrounded claim,
    and the caller's job is to flag it for review, not to fail the turn the
    learner is watching.
    """
    ordered = sorted_passages([p.model_dump() for p in passages])
    cited: dict[str, Passage] = {}

    for match in CITATION_PATTERN.finditer(text):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        for number in range(start, end + 1):
            index = number - 1
            if 0 <= index < len(ordered):
                passage = Passage(**ordered[index])
                cited[str(passage.chunk_id)] = passage

    return list(cited.values())


def has_invalid_citation(text: str, passage_count: int) -> bool:
    """Whether the text cites a passage number that was never supplied."""
    for match in CITATION_PATTERN.finditer(text):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if start < 1 or end > passage_count:
            return True
    return False


class Lecturer(Agent):
    identity = "lecturer"
    kinds = frozenset({"deliver_segment", "generate_check", "re_explain", "worked_example"})

    def __init__(self, client: Any, retriever: PassageRetriever) -> None:
        super().__init__(client)
        self.retriever = retriever

    async def handle(self, input: AgentInput) -> AgentOutput:
        """Non-streaming: only ``generate_check`` (§10)."""
        self.check_kind(input)
        if input.kind in STREAMING_KINDS:
            raise ValueError(
                f"{input.kind!r} streams; call handle_streaming instead"
            )
        return await self._generate_check(input)

    async def handle_streaming(self, input: AgentInput) -> AsyncIterator[StreamChunk]:
        self.check_kind(input)
        if input.kind not in STREAMING_KINDS:
            raise ValueError(f"{input.kind!r} does not stream; call handle instead")

        ctx = input.session_context
        stance = str(input.payload.get("stance", "default"))
        passages = await self._ground(ctx, stance)

        prefix = build_prefix(
            "lecturer",
            _with_passages(ctx, passages),
            model=self._model(input),
            stance=stance,
        )
        spec = self.call_spec(input, prefix=prefix, suffix=self._suffix(input, passages))

        async with self.client.stream(spec) as stream:
            async for delta in stream.text_deltas():
                yield StreamChunk.text_chunk(delta)
            body = stream.text

        # Effects are emitted after the stream closes so a cancelled stream
        # never persists a half-written segment as a reusable artifact.
        for effect in self._segment_effects(input, body, passages, stance, stream):
            yield StreamChunk.effect(effect)

        yield StreamChunk.ended(
            turn_id=str(stream.turn_id) if stream.turn_id else None,
            segment_index=input.payload.get("segment_index", 0),
            anchor=_last_sentence(body),
        )

    # --- grounding ---------------------------------------------------------

    async def _ground(self, ctx: SessionContext, stance: str) -> list[Passage]:
        if ctx.focus_concept_id is None:
            return []
        return await self.retriever.retrieve_passages(
            ctx.focus_concept_id, stance=stance, k=DEFAULT_K
        )

    def _segment_effects(
        self,
        input: AgentInput,
        body: str,
        passages: list[Passage],
        stance: str,
        stream: Any,
    ) -> list[ToolEffect]:
        """Persist the segment and flag anything a reviewer should see (§10)."""
        ctx = input.session_context
        effects: list[ToolEffect] = []

        if not body.strip():
            return effects

        cited = resolve_citations(body, passages)
        effects.append(
            ToolEffect(
                kind="record_content_artifact",
                payload={
                    "concept_id": str(ctx.focus_concept_id),
                    "kind": "lecture_segment",
                    "stance": stance,
                    "body": body,
                    "generated_by": self.identity,
                    "model": stream.model,
                    "prompt_hash": stream.spec.prefix.sha256,
                    "session_id": str(ctx.session_id),
                    "cost_usd": str(stream.record.cost_usd) if stream.record else None,
                    "metadata": {
                        "segment_index": int(input.payload.get("segment_index", 0)),
                        "of": int(input.payload.get("segment_total", 0)),
                    },
                    "citations": [
                        {"source_chunk_id": str(p.chunk_id)} for p in cited
                    ],
                },
            )
        )

        if grounding_is_thin(passages):
            effects.append(
                ToolEffect(
                    kind="flag_for_review",
                    payload={
                        "source": "system_confidence",
                        "severity": 2,
                        "session_turn_id": str(stream.turn_id) if stream.turn_id else None,
                        "reason": (
                            f"Thin grounding: retrieval returned {len(passages)} "
                            f"passage(s) for concept {ctx.focus_concept_id}; §10 "
                            f"expects at least 3."
                        ),
                    },
                )
            )

        if has_invalid_citation(body, len(passages)):
            effects.append(
                ToolEffect(
                    kind="flag_for_review",
                    payload={
                        "source": "system_confidence",
                        "severity": 3,
                        "session_turn_id": str(stream.turn_id) if stream.turn_id else None,
                        "reason": (
                            "Segment cites a passage number that was not supplied "
                            "-- an invented citation reaching a learner."
                        ),
                    },
                )
            )

        return effects

    # --- suffix ------------------------------------------------------------

    def _suffix(self, input: AgentInput, passages: list[Passage]) -> str:
        payload = input.payload
        index = int(payload.get("segment_index", 0))
        total = int(payload.get("segment_total", 0))
        interruption = payload.get("interruption_context") or "none"

        task = {
            "deliver_segment": (
                "Deliver the next segment of this concept. One idea, 200-400 words, "
                "ending with a brief anchor."
            ),
            "re_explain": (
                "The previous explanation did not land. Re-explain the same idea in "
                "the stance above, taking a genuinely different route through it -- "
                "not a reworded version of what was already said.\n"
                f"What did not land: {payload.get('what_is_not_landing', 'unspecified')}"
            ),
            "worked_example": (
                "Produce one fully worked example. State the setup, do each step, "
                "and name what each step accomplishes. Do not skip algebra the "
                "learner would have to reconstruct."
            ),
        }[input.kind]

        return f"""Segment index: {index} of {total} in the current sequence
Previous segment ended with: {payload.get('last_segment_anchor', '(this is the first segment)')}
Learner interruption (if resuming): {interruption}
Learner's current mastery of this concept: {fmt_float(input.session_context.focus_mastery)}
Passages available for citation: {len(passages)}

Task: {task}"""

    async def _generate_check(self, input: AgentInput) -> AgentOutput:
        """A comprehension check after every second segment (§10)."""
        ctx = input.session_context
        stance = str(input.payload.get("stance", "default"))
        passages = await self._ground(ctx, stance)
        prefix = build_prefix(
            "lecturer", _with_passages(ctx, passages), model=self._model(input), stance=stance
        )

        suffix = (
            f"Segment just delivered:\n{input.payload.get('segment_body', '')}\n\n"
            "Task: Produce one comprehension check on what this segment taught. "
            "The question must require the learner to produce an answer, not "
            "recognise one. Give 1-3 key points a correct answer contains, one "
            "hint, and a model answer -- the model answer is withheld until the "
            "learner has attempted."
        )
        spec = self.call_spec(input, prefix=prefix, suffix=suffix)
        result = await self.client.parse(spec, ComprehensionCheck)
        check: ComprehensionCheck = result.structured  # type: ignore[assignment]

        return AgentOutput(
            text=check.question,
            structured=check,
            trace=result.record,
            turn_id=result.turn_id,
        )

    def _model(self, input: AgentInput) -> str:
        from studium.llm.client import route

        return input.payload.get("model_override") or route(self.identity, input.kind)


def _with_passages(ctx: SessionContext, passages: list[Passage]) -> SessionContext:
    """Context copy carrying this call's passages.

    The prefix builder reads ``ctx.passages``; retrieval happens per call
    because ``k`` and stance vary. Copying rather than mutating keeps the
    caller's context clean, which matters when the Orchestrator reuses one
    context across several agents in a turn.
    """
    return ctx.model_copy(update={"passages": passages})


def _last_sentence(text: str) -> str:
    """The segment's closing anchor, fed to the next segment's prompt."""
    stripped = text.strip()
    if not stripped:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", stripped)
    return parts[-1][:300] if parts else ""

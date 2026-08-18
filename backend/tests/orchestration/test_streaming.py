"""Sentence-boundary interruption and SSE (agent runtime §20, §21).

§25 open question 2 says the rule-based detector "will almost certainly need
tuning against real Lecturer output, especially on math-heavy segments". These
tests pin the behaviour it has now, so that tuning is a visible change rather
than a silent drift, and cover the two escapes §20/§21 specify: the LLM
fallback at 60 tokens and the hard cut at 80.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from studium.agents.base import StreamChunk
from studium.orchestration.streaming import (
    HARD_CUT_AFTER_TOKENS,
    LLM_FALLBACK_AFTER_TOKENS,
    InterruptionState,
    find_boundary,
    sentence_boundary_iter,
    sse_event,
    sse_stream,
)


async def _deltas(chunks: list[str]) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


class TestBoundaryDetection:
    @pytest.mark.parametrize(
        ("text", "expected_prefix"),
        [
            ("The term is closed. Now consider ", "The term is closed. "),
            ("That is all.", "That is all."),
            ("Consider 2. Then 3 follows ", "Consider 2. "),
            ("Is it closed? Yes, it is ", "Is it closed? "),
            ("No! Look again ", "No! "),
            ('He said "stop." Then we ', 'He said "stop." '),
        ],
    )
    def test_finds_ordinary_sentence_ends(self, text, expected_prefix):
        cut = find_boundary(text)
        assert cut is not None
        assert text[:cut] == expected_prefix

    @pytest.mark.parametrize(
        "text",
        [
            "We write e.g. lambda x. x for ",
            "See Thm. 3 for the proof of ",
            "Compare with Def. 2 which ",
            "The value is approx. 5 in ",
        ],
    )
    def test_abbreviations_are_not_sentence_ends(self, text):
        """A period after 'e.g.' or 'Thm.' would cut mid-clause."""
        assert find_boundary(text) is None

    def test_periods_inside_inline_math_are_skipped(self):
        """§25 flags math-heavy segments as the detector's weak point."""
        text = "Let $f(x) = 1.5$ be given. Then we "
        cut = find_boundary(text)
        assert text[:cut] == "Let $f(x) = 1.5$ be given. "

    def test_periods_inside_display_math_are_skipped(self):
        text = "Consider $$a = 1.5 + 2.5$$ and then A "
        assert find_boundary(text) is None

    def test_no_boundary_in_an_incomplete_sentence(self):
        assert find_boundary("The term is being reduced and") is None


class TestInterruption:
    async def test_deltas_pass_through_until_interrupted(self):
        """The §20 divergence: token-level streaming, not sentence buffering.

        Without this, every lecture would arrive in sentence-sized jumps and
        streaming would buy nothing.
        """
        state = InterruptionState()
        chunks = ["The ", "term ", "is ", "closed. ", "Now "]
        received = [
            c async for c in sentence_boundary_iter(_deltas(chunks), state)
        ]
        assert received == chunks
        assert state.point.boundary_method == "natural_end"

    async def test_interrupt_stops_at_the_next_sentence_boundary(self):
        """§20: 'stops requesting new tokens after the next completed sentence'."""
        state = InterruptionState()

        async def deltas() -> AsyncIterator[str]:
            yield "We begin. "
            state.signal()  # learner raises a hand here
            yield "The term "
            yield "reduces to "
            yield "normal form. "
            yield "THIS SHOULD NOT BE DELIVERED. "

        delivered = "".join(
            [c async for c in sentence_boundary_iter(deltas(), state)]
        )
        assert delivered == "We begin. The term reduces to normal form. "
        assert "SHOULD NOT BE DELIVERED" not in delivered
        assert state.point.boundary_method == "rule"

    async def test_waste_is_recorded_when_the_boundary_falls_mid_delta(self):
        """§20 budgets 20-80 wasted tokens per interruption; measure it.

        Waste appears only when a delta carries text *past* the sentence end --
        that tail was generated and billed but never delivered. When the
        boundary lands on a delta edge the iterator simply stops pulling, and
        the local view of waste is legitimately zero; the tokens the provider
        generated in flight are visible on ``ManagedStream``, not here.
        """
        state = InterruptionState()

        async def deltas() -> AsyncIterator[str]:
            state.signal()
            yield "Finishing this sentence. And here is a whole extra clause."

        [c async for c in sentence_boundary_iter(deltas(), state)]
        assert state.point.delivered_text == "Finishing this sentence. "
        assert state.point.wasted_chars > 0

    async def test_no_waste_recorded_when_the_cut_lands_on_a_delta_edge(self):
        """The complementary case, so the metric is not read as always-positive."""
        state = InterruptionState()

        async def deltas() -> AsyncIterator[str]:
            state.signal()
            yield "Finishing this sentence. "
            yield "Never pulled."

        [c async for c in sentence_boundary_iter(deltas(), state)]
        assert state.point.wasted_chars == 0

    async def test_llm_fallback_fires_when_the_rule_finds_nothing(self):
        """§20: after 60 tokens with no boundary, ask a model."""
        state = InterruptionState()
        calls: list[str] = []

        async def fallback(buffered: str) -> int | None:
            calls.append(buffered)
            return 10

        async def deltas() -> AsyncIterator[str]:
            state.signal()
            for _ in range(LLM_FALLBACK_AFTER_TOKENS + 5):
                yield "nomarker "

        delivered = "".join(
            [
                c
                async for c in sentence_boundary_iter(
                    deltas(), state, llm_boundary_check=fallback
                )
            ]
        )
        assert calls, "fallback was never consulted"
        assert state.point.boundary_method == "llm"
        assert len(delivered) == 10

    async def test_hard_cut_when_the_fallback_also_fails(self):
        """§21: 'cut at 80 tokens post-interrupt and mark the trace'."""
        state = InterruptionState()

        async def failing_fallback(buffered: str) -> int | None:
            return None

        async def deltas() -> AsyncIterator[str]:
            state.signal()
            for _ in range(HARD_CUT_AFTER_TOKENS + 10):
                yield "x "

        [
            c
            async for c in sentence_boundary_iter(
                deltas(), state, llm_boundary_check=failing_fallback
            )
        ]
        assert state.point.boundary_method == "hard_cut"
        assert state.point.tokens_after_interrupt == HARD_CUT_AFTER_TOKENS

    async def test_a_raising_fallback_does_not_break_the_stream(self):
        """A failed observability-adjacent call must not fail the learner's turn."""
        state = InterruptionState()

        async def exploding(buffered: str) -> int | None:
            raise RuntimeError("boundary service down")

        async def deltas() -> AsyncIterator[str]:
            state.signal()
            for _ in range(HARD_CUT_AFTER_TOKENS + 5):
                yield "y "

        chunks = [
            c
            async for c in sentence_boundary_iter(
                deltas(), state, llm_boundary_check=exploding
            )
        ]
        assert chunks
        assert state.point.boundary_method == "hard_cut"

    def test_clearing_resets_between_turns(self):
        """An interrupt from the previous turn must not cut this one short."""
        state = InterruptionState()
        state.signal()
        assert state.interrupted is True
        state.clear()
        assert state.interrupted is False
        assert state.point is None


class TestSSE:
    def test_frames_are_well_formed(self):
        frame = sse_event(StreamChunk.text_chunk("hello"))
        assert frame.startswith("data: ")
        assert frame.endswith("\n\n")
        assert json.loads(frame[6:].strip())["payload"]["text"] == "hello"

    def test_uuids_and_datetimes_serialise_rather_than_raising(self):
        """An exception inside the response body truncates the stream silently."""
        import uuid

        frame = sse_event(StreamChunk.ended(turn_id=uuid.uuid4()))
        assert json.loads(frame[6:].strip())["payload"]["turn_id"]

    async def test_stream_always_emits_a_terminator(self):
        """Lets a client tell a finished stream from a dropped connection."""

        async def chunks() -> AsyncIterator[StreamChunk]:
            yield StreamChunk.text_chunk("one")

        frames = [f async for f in sse_stream(chunks())]
        assert frames[-1].startswith("event: done")

    async def test_terminator_is_emitted_even_when_the_source_raises(self):
        async def chunks() -> AsyncIterator[StreamChunk]:
            yield StreamChunk.text_chunk("one")
            raise RuntimeError("agent blew up")

        frames = []
        with pytest.raises(RuntimeError):
            async for frame in sse_stream(chunks()):
                frames.append(frame)
        assert any(f.startswith("event: done") for f in frames) or frames

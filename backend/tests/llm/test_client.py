"""The client wrapper: routing, tracing, and streaming (agent runtime §18, §19).

§3: "Every LLM call is accounted for at the point of the call ... There is no
'we'll reconcile costs later' pathway."

These tests run the *real* client against a fake SDK, so what is under test is
the actual request construction and the actual cost arithmetic. Faking one level
higher would skip exactly the code that decides whether Haiku gets an ``effort``
parameter it rejects.
"""

from __future__ import annotations

import uuid

import anthropic
import pytest

from studium.agents.schemas import IntentClassification
from studium.llm import traces
from studium.llm.client import AnthropicClient, CallSpec
from studium.llm.models import HAIKU, OPUS
from studium.llm.prompts import build_prefix
from studium.llm.retries import DegradedCall
from tests.fixtures.runtime import (
    SESSION_ID,
    USER_ID,
    FakeSDK,
    FakeUsage,
    make_context,
)


@pytest.fixture
def captured(monkeypatch):
    """Capture trace writes instead of hitting the database."""
    written: list[traces.TraceRecord] = []

    async def fake_write(record: traces.TraceRecord) -> uuid.UUID:
        written.append(record)
        turn_id = uuid.uuid4()
        record.session_turn_id = turn_id
        return turn_id

    monkeypatch.setattr(traces, "write", fake_write)
    monkeypatch.setattr("studium.llm.client.traces.write", fake_write)
    return written


def spec(agent: str = "tutor", kind: str = "answer", model: str = OPUS.id) -> CallSpec:
    ctx = make_context()
    return CallSpec(
        agent=agent,
        kind=kind,
        prefix=build_prefix(agent, ctx, model=model, **({"stance": "formal"} if agent == "lecturer" else {})),
        suffix="the learner said something",
        session_id=SESSION_ID,
        user_id=USER_ID,
        exchange_index=3,
    )


class TestTracingInvariant:
    async def test_a_structured_call_writes_exactly_one_trace(self, captured):
        sdk = FakeSDK(
            parsed=IntentClassification(intent="question", confidence=0.9, reasoning="?")
        )
        await AnthropicClient(sdk).parse(spec("orchestrator", "classify_intent", HAIKU.id), IntentClassification)
        assert len(captured) == 1
        assert captured[0].agent == "orchestrator"
        assert captured[0].kind == "classify_intent"

    async def test_a_streaming_call_writes_exactly_one_trace(self, captured):
        sdk = FakeSDK(stream_chunks=["Beta ", "reduction ", "is."])
        client = AnthropicClient(sdk)
        async with client.stream(spec()) as stream:
            [c async for c in stream.text_deltas()]
        assert len(captured) == 1
        assert captured[0].completion == "Beta reduction is."

    async def test_an_abandoned_stream_still_writes_a_trace(self, captured):
        """§20: tokens emitted before an interrupt were paid for.

        A stream the caller stopped consuming still cost money, so dropping the
        trace would understate spend -- the one failure §3 says must not happen.
        """
        chunks = ["one ", "two ", "three ", "four"]
        client = AnthropicClient(FakeSDK(stream_chunks=chunks))
        async with client.stream(spec()) as stream:
            async for _ in stream.text_deltas():
                break  # learner interrupted after the first delta
        assert len(captured) == 1
        partial_cost = captured[0].cost_usd

        captured.clear()
        client = AnthropicClient(FakeSDK(stream_chunks=chunks))
        async with client.stream(spec()) as stream:
            [c async for c in stream.text_deltas()]
        full_cost = captured[0].cost_usd

        # Billed for what was generated, not for nothing and not for the whole
        # segment: the trace has to land between the two.
        assert 0 < partial_cost < full_cost

    async def test_the_trace_carries_the_prefix_hash_for_clustering(self, captured):
        """§6.6: system_prompt_hash clusters traces by prompt version."""
        call = spec()
        sdk = FakeSDK(parsed=IntentClassification(intent="question", confidence=1.0, reasoning=""))
        await AnthropicClient(sdk).parse(call, IntentClassification)
        assert captured[0].system_prompt_hash == call.prefix.sha256
        assert len(captured[0].system_prompt_hash) == 64

    async def test_cost_is_computed_from_the_reported_usage(self, captured):
        from studium.llm.models import TokenUsage, compute_cost

        usage = FakeUsage(input_tokens=1000, output_tokens=500, cache_read=9000)
        sdk = FakeSDK(
            parsed=IntentClassification(intent="question", confidence=1.0, reasoning=""),
            usage=usage,
        )
        await AnthropicClient(sdk).parse(spec(), IntentClassification)

        expected = compute_cost(
            OPUS.id, TokenUsage(tokens_in=1000, tokens_out=500, cache_read_tokens=9000)
        )
        assert captured[0].cost_usd == expected
        assert captured[0].cache_hit_rate == pytest.approx(9000 / 10000)


class TestRequestConstruction:
    async def test_routes_to_the_model_the_table_names(self, captured):
        sdk = FakeSDK(parsed=IntentClassification(intent="question", confidence=1.0, reasoning=""))
        await AnthropicClient(sdk).parse(spec("evaluator", "grade_check", HAIKU.id), IntentClassification)
        assert sdk.models_used() == [HAIKU.id]

    async def test_effort_is_never_sent_to_haiku(self, captured):
        """output_config.effort is a 400 on Haiku 4.5."""
        sdk = FakeSDK(parsed=IntentClassification(intent="question", confidence=1.0, reasoning=""))
        await AnthropicClient(sdk).parse(
            spec("confusion_tracker", "evaluate_turn", HAIKU.id), IntentClassification
        )
        assert "output_config" not in sdk.requests("parse")[0]

    async def test_adaptive_thinking_is_set_explicitly_on_opus(self, captured):
        """Omitting `thinking` on Opus 4.8 runs without thinking at all."""
        sdk = FakeSDK(stream_chunks=["x"])
        client = AnthropicClient(sdk)
        async with client.stream(spec("lecturer", "deliver_segment")) as stream:
            [c async for c in stream.text_deltas()]
        assert sdk.requests("stream")[0]["thinking"] == {"type": "adaptive"}

    async def test_no_sampling_parameters_are_ever_sent(self, captured):
        """temperature / top_p / top_k are rejected outright by Opus 4.8."""
        sdk = FakeSDK(stream_chunks=["x"])
        client = AnthropicClient(sdk)
        async with client.stream(spec()) as stream:
            [c async for c in stream.text_deltas()]
        request = sdk.requests("stream")[0]
        for banned in ("temperature", "top_p", "top_k"):
            assert banned not in request

    async def test_the_system_block_carries_the_cache_marker_when_it_will_cache(self, captured):
        sdk = FakeSDK(stream_chunks=["x"])
        client = AnthropicClient(sdk)
        call = spec("tutor", "answer")
        async with client.stream(call) as stream:
            [c async for c in stream.text_deltas()]

        block = sdk.requests("stream")[0]["system"][0]
        if call.prefix.will_cache():
            assert block["cache_control"]["type"] == "ephemeral"
        else:
            assert "cache_control" not in block

    async def test_max_tokens_is_bounded_per_kind(self, captured):
        sdk = FakeSDK(parsed=IntentClassification(intent="question", confidence=1.0, reasoning=""))
        await AnthropicClient(sdk).parse(
            spec("orchestrator", "classify_intent", HAIKU.id), IntentClassification
        )
        assert sdk.requests("parse")[0]["max_tokens"] == 512


class TestModelOverride:
    def test_an_override_without_a_reason_is_refused(self):
        """§18: overrides are 'always with a reason tag that flows to the trace'."""
        call = spec()
        call.model_override = HAIKU.id
        with pytest.raises(ValueError, match="override_reason"):
            call.model()

    def test_an_override_with_a_reason_is_honoured(self):
        call = spec()
        call.model_override = OPUS.id
        call.override_reason = "reviewer-flagged session; higher-signal hypothesis"
        assert call.model() == OPUS.id

    async def test_the_reason_reaches_the_trace(self, captured):
        call = spec("confusion_tracker", "evaluate_turn", HAIKU.id)
        call.model_override = OPUS.id
        call.override_reason = "demonstrably-hard concept"
        sdk = FakeSDK(parsed=IntentClassification(intent="question", confidence=1.0, reasoning=""))
        await AnthropicClient(sdk).parse(call, IntentClassification)
        assert captured[0].tools_used == [{"model_override_reason": "demonstrably-hard concept"}]


class TestRetryPolicy:
    async def test_the_sdk_retry_loop_is_disabled(self):
        """One retry policy, in one place (§21). Both on would give 6 attempts."""
        client = AnthropicClient()
        assert client._sdk.max_retries == 0

    async def test_a_transient_failure_is_retried_once_then_degrades(self, captured, monkeypatch):
        monkeypatch.setattr("studium.llm.retries.asyncio.sleep", _no_sleep)
        attempts = {"n": 0}

        class Failing(FakeSDK):
            def next_parse_response(self, kwargs):
                attempts["n"] += 1
                raise anthropic.APITimeoutError(request=None)  # type: ignore[arg-type]

        with pytest.raises(DegradedCall) as exc:
            await AnthropicClient(Failing()).parse(spec(), IntentClassification)

        assert attempts["n"] == 2  # try, then retry once
        assert exc.value.kind == "timeout"

    async def test_a_client_error_is_not_retried(self, captured, monkeypatch):
        """Retrying a malformed request just bills for it twice."""
        monkeypatch.setattr("studium.llm.retries.asyncio.sleep", _no_sleep)
        attempts = {"n": 0}

        class BadRequest(FakeSDK):
            def next_parse_response(self, kwargs):
                attempts["n"] += 1
                raise anthropic.BadRequestError(
                    "bad", response=_FakeHTTPResponse(400), body=None
                )

        with pytest.raises(DegradedCall) as exc:
            await AnthropicClient(BadRequest()).parse(spec(), IntentClassification)

        assert attempts["n"] == 1
        assert exc.value.kind == "bad_request"


async def _no_sleep(seconds: float) -> None:
    return None


class _FakeHTTPResponse:
    """Minimal stand-in for the httpx response the SDK errors carry."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.request = None

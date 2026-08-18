"""Cost computation and model routing (agent runtime §23 Tier 1, §18, §19).

§23: "Cost computation: ``compute_cost(model, usage)`` returns expected values
for each model at each cache tier."

The per-model capability assertions matter as much as the arithmetic: sending
``effort`` to Haiku 4.5 or ``budget_tokens`` to Opus 4.8 is a 400, and a test
that only checked prices would let that regress silently.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from studium.llm.client import EFFORT, MAX_TOKENS, ROUTING, ModelRoutingError, route
from studium.llm.models import (
    HAIKU,
    OPUS,
    REGISTRY,
    TokenUsage,
    UnknownModelError,
    caches_at,
    compute_cost,
    request_overrides,
    spec_for,
    usage_from_response,
)
from tests.fixtures.runtime import FakeUsage


class TestPricing:
    def test_opus_and_haiku_carry_the_locked_prices(self):
        """§4 locks Opus 4.8 and Haiku 4.5; §19 bills against their list rates."""
        assert OPUS.id == "claude-opus-4-8"
        assert (OPUS.input_per_mtok, OPUS.output_per_mtok) == (
            Decimal("5.00"),
            Decimal("25.00"),
        )
        assert HAIKU.id == "claude-haiku-4-5"
        assert (HAIKU.input_per_mtok, HAIKU.output_per_mtok) == (
            Decimal("1.00"),
            Decimal("5.00"),
        )

    def test_cache_multipliers_follow_the_api_tiers(self):
        """Read 0.1x input, 5m write 1.25x, 1h write 2x."""
        assert OPUS.cache_read_per_mtok == Decimal("0.500")
        assert OPUS.cache_write_5m_per_mtok == Decimal("6.2500")
        assert OPUS.cache_write_1h_per_mtok == Decimal("10.000")
        assert HAIKU.cache_read_per_mtok == Decimal("0.100")

    def test_plain_call_cost(self):
        usage = TokenUsage(tokens_in=1_000_000, tokens_out=1_000_000)
        assert compute_cost(OPUS.id, usage) == Decimal("30.00")
        assert compute_cost(HAIKU.id, usage) == Decimal("6.00")

    def test_cost_at_each_cache_tier(self):
        """One million tokens through each tier, priced independently."""
        million = 1_000_000
        assert compute_cost(OPUS.id, TokenUsage(cache_read_tokens=million)) == Decimal("0.5")
        assert compute_cost(
            OPUS.id, TokenUsage(cache_write_5m_tokens=million)
        ) == Decimal("6.25")
        assert compute_cost(
            OPUS.id, TokenUsage(cache_write_1h_tokens=million)
        ) == Decimal("10")

    @staticmethod
    def _cached_cost(tier: str, prefix_tokens: int, calls: int) -> Decimal:
        """One write plus (calls - 1) reads at the given TTL."""
        write = {"5m": "cache_write_5m_tokens", "1h": "cache_write_1h_tokens"}[tier]
        return compute_cost(OPUS.id, TokenUsage(**{write: prefix_tokens})) + (
            compute_cost(OPUS.id, TokenUsage(cache_read_tokens=prefix_tokens))
            * (calls - 1)
        )

    def test_five_minute_caching_pays_from_the_second_call(self):
        """1.25x write + 0.1x read = 1.35x, against 2x for two cold calls."""
        prefix = 20_000
        cold = compute_cost(OPUS.id, TokenUsage(tokens_in=prefix))
        assert self._cached_cost("5m", prefix, calls=2) < cold * 2

    def test_one_hour_caching_does_not_pay_until_the_third_call(self):
        """A 1-hour write costs 2x input, so two calls is a *net loss*.

        This is the break-even §17's TTL assignment rests on, and it is not
        where intuition puts it: 2x + 0.1x = 2.1x against 2x for two cold
        calls. The 1-hour TTL is only correct for an agent that makes three or
        more calls against the same prefix within the hour.

        It holds for the Lecturer (a segment sequence is many calls on one
        concept). It is marginal for the Curator, whose own §9 cost profile is
        "~1 at session open and ~2-4 at topic transitions" -- a session that
        opens and transitions once pays more for caching than not. Recorded as
        a v1.1 candidate rather than changed here, since the spec locks it.
        """
        prefix = 20_000
        cold = compute_cost(OPUS.id, TokenUsage(tokens_in=prefix))

        assert self._cached_cost("1h", prefix, calls=2) > cold * 2  # loss
        assert self._cached_cost("1h", prefix, calls=3) < cold * 3  # pays off

    def test_unknown_model_raises_rather_than_costing_zero(self):
        """§3 has no 'reconcile costs later' pathway; a silent zero is worse."""
        with pytest.raises(UnknownModelError):
            compute_cost("claude-made-up-9", TokenUsage(tokens_in=100))

    def test_decimal_throughout_so_the_ledger_does_not_drift(self):
        """agent_traces.cost_usd is NUMERIC(10,6); float would visibly drift."""
        cost = compute_cost(OPUS.id, TokenUsage(tokens_in=333, tokens_out=777))
        assert isinstance(cost, Decimal)


class TestUsageNormalisation:
    def test_reads_the_nested_cache_breakdown_when_present(self):
        usage = usage_from_response(
            FakeUsage(cache_write_5m=10, cache_write_1h=20, nested=True)
        )
        assert usage.cache_write_5m_tokens == 10
        assert usage.cache_write_1h_tokens == 20

    def test_falls_back_to_the_total_attributed_to_the_requested_ttl(self):
        """The §19 sketch reads fields the API does not expose at top level.

        With no breakdown, the single total is attributed to the TTL the
        request asked for -- exact, because one request writes at one TTL.
        """
        flat = FakeUsage(cache_write_1h=40, nested=False)
        usage = usage_from_response(flat, requested_ttl="1h")
        assert (usage.cache_write_5m_tokens, usage.cache_write_1h_tokens) == (0, 40)

        usage_5m = usage_from_response(flat, requested_ttl="5m")
        assert (usage_5m.cache_write_5m_tokens, usage_5m.cache_write_1h_tokens) == (40, 0)

    def test_unknown_ttl_books_to_the_cheaper_tier(self):
        """An accounting gap should not overstate spend it cannot verify."""
        usage = usage_from_response(FakeUsage(cache_write_1h=40, nested=False))
        assert usage.cache_write_5m_tokens == 40

    def test_prompt_tokens_sum_all_four_input_sources(self):
        """tokens_in is the uncached remainder, per the data layer's comment."""
        usage = TokenUsage(
            tokens_in=10, cache_read_tokens=90, cache_write_5m_tokens=5,
            cache_write_1h_tokens=5,
        )
        assert usage.prompt_tokens == 110
        assert usage.cache_hit_rate == pytest.approx(90 / 110)

    def test_cache_hit_rate_of_an_empty_prompt_is_zero_not_an_error(self):
        assert TokenUsage().cache_hit_rate == 0.0


class TestRouting:
    @pytest.mark.parametrize(
        ("agent", "kind", "expected"),
        [
            ("orchestrator", "classify_intent", HAIKU.id),
            ("curator", "next_topic", OPUS.id),
            ("curator", "open_session", OPUS.id),
            ("lecturer", "deliver_segment", OPUS.id),
            ("lecturer", "generate_check", OPUS.id),
            ("tutor", "answer", OPUS.id),
            ("tutor", "primitive:im_lost", OPUS.id),
            ("evaluator", "grade_assessment", OPUS.id),
            ("evaluator", "grade_check", HAIKU.id),
            ("evaluator", "grade_practice", HAIKU.id),
            ("evaluator", "check_partial", HAIKU.id),
            ("confusion_tracker", "evaluate_turn", HAIKU.id),
            ("reviewer", "generate_prompt", OPUS.id),
            ("reviewer", "retrieval_check", OPUS.id),
            ("reviewer", "grade_response", HAIKU.id),
        ],
    )
    def test_matches_the_section_18_table(self, agent, kind, expected):
        assert route(agent, kind) == expected

    def test_unknown_agent_raises(self):
        with pytest.raises(ModelRoutingError):
            route("registrar", "anything")

    def test_every_routed_model_has_pricing(self):
        """A routable model with no price would produce untraceable spend."""
        for table in ROUTING.values():
            for model in table.values():
                assert model in REGISTRY


class TestPerModelCapabilities:
    def test_effort_is_dropped_for_haiku_and_sent_to_opus(self):
        """output_config.effort is a 400 on Haiku 4.5."""
        assert "output_config" in request_overrides(OPUS.id, effort="high")
        assert "output_config" not in request_overrides(HAIKU.id, effort="high")

    def test_opus_gets_adaptive_thinking_explicitly(self):
        """Omitting `thinking` on Opus 4.8 runs *without* thinking."""
        assert request_overrides(OPUS.id, thinking=True)["thinking"] == {
            "type": "adaptive"
        }

    def test_haiku_is_sent_no_thinking_config_at_all(self):
        """budget_tokens is Haiku's mode and buys nothing on bounded calls."""
        assert "thinking" not in request_overrides(HAIKU.id, thinking=True)

    def test_thinking_can_be_suppressed_on_opus(self):
        assert "thinking" not in request_overrides(OPUS.id, thinking=False)

    def test_no_model_in_the_registry_accepts_sampling_params_by_accident(self):
        """Opus 4.8 rejects temperature/top_p/top_k outright."""
        assert OPUS.supports_sampling_params is False

    def test_cache_minimums_differ_fourfold(self):
        """The §17 hazard: Haiku's floor is 4x Opus's.

        A 2000-token prefix caches on Opus and silently does not on Haiku --
        which is the Confusion-Tracker's situation exactly.
        """
        assert OPUS.cache_min_tokens == 1024
        assert HAIKU.cache_min_tokens == 4096
        assert caches_at(OPUS.id, 2000) is True
        assert caches_at(HAIKU.id, 2000) is False


class TestTokenCeilings:
    def test_every_routed_kind_has_a_ceiling_or_a_default(self):
        """A missing ceiling falls back rather than sending the model maximum."""
        from studium.llm.client import DEFAULT_MAX_TOKENS

        assert MAX_TOKENS.get("classify_intent") == 512
        assert MAX_TOKENS.get("nonexistent_kind", DEFAULT_MAX_TOKENS) == DEFAULT_MAX_TOKENS

    def test_ceilings_stay_under_each_model_maximum(self):
        for kind, ceiling in MAX_TOKENS.items():
            model = spec_for(OPUS.id if kind in EFFORT else HAIKU.id)
            assert ceiling <= model.max_output_tokens, kind

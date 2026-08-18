"""Model registry, pricing, and per-model capability flags (agent runtime §18).

The runtime spec locks two models: Claude Opus 4.8 for learner-visible
generation and sequencing, Claude Haiku 4.5 for classification and
single-criterion grading. They are *not* interchangeable at the request level,
and the differences are load-bearing enough to be data rather than scattered
``if model ==`` checks:

* **Effort.** ``output_config.effort`` is accepted by Opus 4.8 and rejected by
  Haiku 4.5. Sending it to Haiku is a 400, so :func:`request_overrides` filters
  it out rather than trusting every call site to remember.
* **Thinking.** Opus 4.8 takes ``thinking={"type": "adaptive"}`` and rejects
  ``budget_tokens``; Haiku 4.5 is the reverse. Omitting ``thinking`` on Opus 4.8
  runs *without* thinking, so the on-mode has to be set explicitly.
* **Cache minimum.** A prefix shorter than the model's minimum silently does not
  cache -- no error, ``cache_creation_input_tokens: 0``. The minimum is 1024
  tokens on Opus 4.8 but **4096 on Haiku 4.5**, which matters because §17 gives
  the Confusion-Tracker (Haiku) a 1-hour cached prefix. See DIVERGENCES (R2).
* **Sampling.** ``temperature`` / ``top_p`` / ``top_k`` are rejected outright by
  Opus 4.8. Nothing here sends them; the flag exists so a future call site
  cannot quietly reintroduce one.

Prices are USD per million tokens, current as of 14 August 2026. Cache
multipliers are API-wide rather than per-model: a read bills at 0.1x base
input, a 5-minute write at 1.25x, a 1-hour write at 2x.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

#: Cache pricing multipliers against the model's base input price.
CACHE_READ_MULTIPLIER = Decimal("0.1")
CACHE_WRITE_5M_MULTIPLIER = Decimal("1.25")
CACHE_WRITE_1H_MULTIPLIER = Decimal("2.0")

TTL = Literal["5m", "1h"]

ThinkingMode = Literal["adaptive", "budget", "none"]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Everything the runtime needs to know about one model."""

    id: str
    #: USD per million tokens.
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    #: Shortest prefix that will actually cache. Below this the API accepts the
    #: cache_control marker and silently caches nothing.
    cache_min_tokens: int
    #: Whether output_config.effort is accepted.
    supports_effort: bool
    #: How this model wants extended thinking configured, if at all.
    thinking_mode: ThinkingMode
    #: Whether temperature / top_p / top_k are accepted.
    supports_sampling_params: bool
    max_output_tokens: int

    @property
    def cache_read_per_mtok(self) -> Decimal:
        return self.input_per_mtok * CACHE_READ_MULTIPLIER

    @property
    def cache_write_5m_per_mtok(self) -> Decimal:
        return self.input_per_mtok * CACHE_WRITE_5M_MULTIPLIER

    @property
    def cache_write_1h_per_mtok(self) -> Decimal:
        return self.input_per_mtok * CACHE_WRITE_1H_MULTIPLIER


#: Spec §4 "Model choices (locked at v1.0 of this spec)".
OPUS = ModelSpec(
    id="claude-opus-4-8",
    input_per_mtok=Decimal("5.00"),
    output_per_mtok=Decimal("25.00"),
    cache_min_tokens=1024,
    supports_effort=True,
    thinking_mode="adaptive",
    supports_sampling_params=False,
    max_output_tokens=128_000,
)

HAIKU = ModelSpec(
    id="claude-haiku-4-5",
    input_per_mtok=Decimal("1.00"),
    output_per_mtok=Decimal("5.00"),
    # Four times Opus's minimum. The Confusion-Tracker and Reviewer prefixes
    # are the ones at risk; build_prefix warns when a prefix lands under it.
    cache_min_tokens=4096,
    supports_effort=False,
    thinking_mode="budget",
    supports_sampling_params=True,
    max_output_tokens=64_000,
)

REGISTRY: dict[str, ModelSpec] = {OPUS.id: OPUS, HAIKU.id: HAIKU}


class UnknownModelError(KeyError):
    """A model id with no pricing entry.

    Raised rather than defaulted: a silent zero would make the cost ledger
    understate spend, which is the one failure §3 says the runtime must not
    have.
    """


def spec_for(model: str) -> ModelSpec:
    try:
        return REGISTRY[model]
    except KeyError as exc:  # noqa: TRY003 -- message carries the bad id
        raise UnknownModelError(
            f"no pricing entry for model {model!r}; add it to studium.llm.models.REGISTRY"
        ) from exc


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """The token counts a trace needs, normalised across SDK shapes.

    ``tokens_in`` is the *uncached remainder*, matching both the API's
    ``input_tokens`` and the data layer's column comment: total prompt size is
    ``tokens_in + cache_read + cache_write_5m + cache_write_1h``.
    """

    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        return (
            self.tokens_in
            + self.cache_read_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
        )

    @property
    def cache_hit_rate(self) -> float:
        """Cache-read tokens as a share of the whole prompt.

        §17 wants 60-80% after the first few turns of a session. Zero-prompt
        calls return 0.0 rather than dividing by zero.
        """
        total = self.prompt_tokens
        return self.cache_read_tokens / total if total else 0.0


def usage_from_response(usage: Any, *, requested_ttl: TTL | None = None) -> TokenUsage:
    """Normalise an SDK usage object into :class:`TokenUsage`.

    The spec's §19 sketch reads ``usage.cache_creation_5m_tokens`` and
    ``usage.cache_creation_1h_tokens`` directly. Those are not top-level fields:
    the API reports a single ``cache_creation_input_tokens`` total, with the
    per-TTL split under a nested ``cache_creation`` object where the SDK version
    exposes it. Both paths are handled --

    1. the nested breakdown when present, which is exact;
    2. otherwise the total, attributed to ``requested_ttl``, which is correct
       because a single request only ever writes at one TTL.

    Falling back rather than raising keeps cost accounting working across SDK
    versions; the attribution is exact either way. See DIVERGENCES (R1).
    """
    read = _int(usage, "cache_read_input_tokens")
    write_5m, write_1h = _split_cache_writes(usage, requested_ttl)

    return TokenUsage(
        tokens_in=_int(usage, "input_tokens"),
        tokens_out=_int(usage, "output_tokens"),
        cache_read_tokens=read,
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
    )


def _split_cache_writes(usage: Any, requested_ttl: TTL | None) -> tuple[int, int]:
    breakdown = getattr(usage, "cache_creation", None)
    if breakdown is not None:
        five = _int(breakdown, "ephemeral_5m_input_tokens")
        hour = _int(breakdown, "ephemeral_1h_input_tokens")
        if five or hour:
            return five, hour

    total = _int(usage, "cache_creation_input_tokens")
    if not total:
        return 0, 0
    # One request writes at one TTL. Unknown TTL books to 5m, the cheaper
    # multiplier, so an accounting gap understates nothing it can avoid.
    return (0, total) if requested_ttl == "1h" else (total, 0)


def _int(obj: Any, name: str) -> int:
    value = getattr(obj, name, None)
    if value is None and isinstance(obj, dict):
        value = obj.get(name)
    return int(value or 0)


def compute_cost(model: str, usage: TokenUsage) -> Decimal:
    """Cost in USD for one call.

    Decimal throughout: ``agent_traces.cost_usd`` is ``NUMERIC(10, 6)`` and
    float accumulation across tens of thousands of turns visibly drifts against
    the ledger it is reconciled with.
    """
    ms = spec_for(model)
    per_token = Decimal(1_000_000)
    return (
        usage.tokens_in * ms.input_per_mtok
        + usage.tokens_out * ms.output_per_mtok
        + usage.cache_read_tokens * ms.cache_read_per_mtok
        + usage.cache_write_5m_tokens * ms.cache_write_5m_per_mtok
        + usage.cache_write_1h_tokens * ms.cache_write_1h_per_mtok
    ) / per_token


def request_overrides(
    model: str,
    *,
    effort: str | None = None,
    thinking: bool = True,
) -> dict[str, Any]:
    """Build the per-model request kwargs for thinking and effort.

    The point of this function is that no agent should have to remember which
    model tolerates which parameter. Ask for what you want; unsupported knobs
    are dropped rather than sent and rejected.
    """
    ms = spec_for(model)
    kwargs: dict[str, Any] = {}

    # Opus 4.8 runs *without* thinking when the field is omitted, so the on-mode
    # is explicit. 'budget' models (Haiku 4.5) are left alone: every runtime
    # call to Haiku is a bounded classification or single-criterion grade,
    # where a thinking budget buys latency and no accuracy.
    if thinking and ms.thinking_mode == "adaptive":
        kwargs["thinking"] = {"type": "adaptive"}

    if effort is not None and ms.supports_effort:
        kwargs["output_config"] = {"effort": effort}

    return kwargs


def caches_at(model: str, prefix_tokens: int) -> bool:
    """Whether a prefix of this size will actually cache on this model."""
    return prefix_tokens >= spec_for(model).cache_min_tokens

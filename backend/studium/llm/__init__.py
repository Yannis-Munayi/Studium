"""LLM access: routing, cached prefixes, retries, and trace writing (§18, §19)."""

from .client import AnthropicClient, CallSpec, route
from .models import HAIKU, OPUS, TokenUsage, compute_cost, spec_for
from .prompts import CachedPrefix, build_prefix
from .retries import DegradedCall, with_retry
from .traces import TraceRecord

__all__ = [
    "HAIKU",
    "OPUS",
    "AnthropicClient",
    "CachedPrefix",
    "CallSpec",
    "DegradedCall",
    "TokenUsage",
    "TraceRecord",
    "build_prefix",
    "compute_cost",
    "route",
    "spec_for",
    "with_retry",
]

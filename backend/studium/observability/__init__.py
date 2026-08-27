"""Observability wiring (infrastructure §7).

§3: "Observability is designed in, not retrofitted. ... the reviewer can
navigate from a specific learner turn to the LLM trace that produced it, to the
HTTP request that carried it, to the error (if any) that fired."

Four systems, three of which are wired here (the fourth, Fly's infrastructure
metrics, is automatic and needs no code):

* **Langfuse** -- already built. ``studium.llm.traces`` emits a generation per
  LLM call, per agent runtime §22. Nothing here duplicates it; what this
  package adds is the correlation id that ties a Langfuse trace to the HTTP
  request that produced it.
* **Sentry** (§7.2) -- unhandled exceptions, with PII scrubbing. See
  :mod:`studium.observability.sentry`.
* **OpenTelemetry** (§7.3) -- a span per FastAPI request, exported to Langfuse
  so LLM and HTTP traces share a trace id.

**Every hook here is optional and every one of them is silent when absent.**
Three SDKs, none of them in the base install, all of them no-ops when
unconfigured. §7's value is real and it is not worth a learner's turn: an
observability backend that can fail a request is a second outage source
attached to the thing that was supposed to explain the first.

That is not a slogan -- it is asserted by a Tier 1 test that imports this
package with ``sentry_sdk`` and ``opentelemetry`` blocked and drives
:func:`configure` to completion.

**The correlation id is the load-bearing part**, and it is the piece that needs
no SDK at all. One id per request, on the log record, on the Sentry event, on
the OTel span, and on the Langfuse trace. Without it §3's "navigate from a turn
to the trace to the request to the error" is four searches by timestamp.
"""

from __future__ import annotations

import logging
from typing import Any

from .correlation import (
    CORRELATION_HEADER,
    NO_CORRELATION_ID,
    CorrelationIdMiddleware,
    correlation_id,
    install_log_filter,
    new_correlation_id,
    set_correlation_id,
    uninstall_log_filter,
)

log = logging.getLogger(__name__)

__all__ = [
    "CORRELATION_HEADER",
    "NO_CORRELATION_ID",
    "CorrelationIdMiddleware",
    "configure",
    "correlation_id",
    "install_log_filter",
    "new_correlation_id",
    "set_correlation_id",
    "status",
    "uninstall_log_filter",
]

_state: dict[str, Any] = {"sentry": False, "otel": False, "correlation": False}


def configure(app: Any = None, *, service: str = "studium-backend") -> dict[str, Any]:
    """Wire everything available. Never raises.

    Called once from the FastAPI lifespan. Returns what actually came up, which
    ``GET /health`` reports -- "is tracing on" should be answerable without
    reading startup logs, because the answer changes with an environment
    variable and the question is asked during an incident.
    """
    install_log_filter()
    _state["correlation"] = True

    from . import sentry as sentry_module

    _state["sentry"] = sentry_module.configure(service=service)

    from . import otel as otel_module

    _state["otel"] = otel_module.configure(app, service=service)

    log.info(
        "observability: correlation=on sentry=%s otel=%s langfuse=%s",
        _state["sentry"],
        _state["otel"],
        _langfuse_enabled(),
    )
    return status()


def _langfuse_enabled() -> bool:
    """Whether ``studium.llm.traces`` resolved a client.

    Read rather than re-resolved: constructing a second Langfuse client to find
    out would double the connections and could succeed where the first failed,
    reporting a state the tracing path is not in.
    """
    from studium.llm import traces

    return traces._langfuse() is not None


def status() -> dict[str, Any]:
    return {
        "correlation_ids": _state["correlation"],
        "sentry": _state["sentry"],
        "opentelemetry": _state["otel"],
        "langfuse": _langfuse_enabled(),
    }

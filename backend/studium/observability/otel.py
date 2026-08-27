"""OpenTelemetry HTTP tracing (infrastructure §7.3).

§7.3: "Every FastAPI request produces a span. Every Next.js server-side render
produces a span. Trace IDs propagate between frontend and backend so a slow
session can be traced end-to-end." And: "Traces export to Langfuse (which
accepts OTel format) so LLM traces and HTTP traces share the same trace ID."

**No separate tracing backend** (§7.3): no Jaeger, no Honeycomb. Langfuse's
OTLP endpoint is the exporter target, which is what makes "share the same trace
id" true rather than aspirational -- the alternative is two systems with two id
spaces and a timestamp join between them.

**Instrumentation is opt-in and additive.** Without ``opentelemetry-sdk`` and
``opentelemetry-instrumentation-fastapi`` installed, :func:`configure` logs at
debug and returns False. With them but with no exporter endpoint configured,
spans are created and dropped -- which is deliberately still useful, because
the span context is what carries the correlation id into anything that reads
it, and because a deployment turning tracing on later should not discover then
that the instrumentation never worked.

**The health endpoint is excluded.** Uptime Robot polls it every five minutes
and Fly's load balancer far more often than that; at MVP volume those would be
the overwhelming majority of spans, and a trace store whose contents are
mostly health checks is one nobody scrolls through twice.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"

#: Paths that produce no span. See the module docstring.
EXCLUDED_URLS = "health,metrics,favicon.ico"


def configure(app: Any = None, *, service: str = "studium-backend") -> bool:
    """Instrument the FastAPI app. Returns whether it came up. Never raises."""
    try:
        from opentelemetry import trace  # type: ignore[import-not-found]
        from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
        from opentelemetry.sdk.trace import (
            TracerProvider,  # type: ignore[import-not-found]
        )
        from opentelemetry.sdk.trace.export import (  # type: ignore[import-not-found]
            BatchSpanProcessor,
        )
    except ImportError:
        log.debug("OpenTelemetry disabled (opentelemetry-sdk not installed)")
        return False

    try:
        resource = Resource.create(
            {
                "service.name": service,
                "deployment.environment": os.environ.get("STUDIUM_ENV", "local"),
                "service.version": os.environ.get("FLY_MACHINE_VERSION", "dev"),
            }
        )
        provider = TracerProvider(resource=resource)

        endpoint = (os.environ.get(ENDPOINT_ENV) or "").strip()
        if endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
                OTLPSpanExporter,
            )

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            log.info("OpenTelemetry exporting to %s", endpoint)
        else:
            log.info(
                "OpenTelemetry instrumented with no exporter (%s not set); "
                "spans are created and dropped",
                ENDPOINT_ENV,
            )

        trace.set_tracer_provider(provider)

        if app is not None:
            _instrument_fastapi(app)
    except Exception:  # noqa: BLE001 -- must not fail startup
        log.exception("OpenTelemetry init failed; continuing without it")
        return False

    return True


def _instrument_fastapi(app: Any) -> None:
    from opentelemetry.instrumentation.fastapi import (  # type: ignore[import-not-found]
        FastAPIInstrumentor,
    )

    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls=EXCLUDED_URLS,
        server_request_hook=_tag_correlation_id,
    )


def _tag_correlation_id(span: Any, scope: dict) -> None:
    """Put the correlation id on the server span.

    The join §3 asks for: an OTel trace id identifies the request inside the
    tracing system, and the correlation id identifies it in the logs, in Sentry
    and in Langfuse. Recording one on the other means either can be searched
    for from the other, which is the difference between "navigate" and "search
    four systems by timestamp".
    """
    try:
        from .correlation import correlation_id

        current = correlation_id()
        if current and span is not None and span.is_recording():
            span.set_attribute("studium.correlation_id", current)
    except Exception:  # noqa: BLE001
        log.debug("could not tag span with correlation id", exc_info=True)


def current_trace_id() -> str:
    """The active OTel trace id as hex, or empty string.

    ``studium.llm.traces`` uses it as the Langfuse trace id where one is
    available, so an LLM generation lands under the HTTP request that caused
    it rather than under a trace of its own.
    """
    try:
        from opentelemetry import trace  # type: ignore[import-not-found]
    except ImportError:
        return ""
    span = trace.get_current_span()
    context = span.get_span_context() if span else None
    if context is None or not context.is_valid:
        return ""
    return format(context.trace_id, "032x")

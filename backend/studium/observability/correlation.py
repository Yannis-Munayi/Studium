"""One id per request, everywhere (infrastructure §3, §7).

§3: "Metrics are structured; log messages include correlation IDs; the reviewer
can navigate from a specific learner turn to the LLM trace that produced it, to
the HTTP request that carried it, to the error (if any) that fired."

The id is generated at the edge (or taken from the client, see below), stored
in a :class:`contextvars.ContextVar`, and read by everything downstream: the
log filter puts it on every record, Sentry tags every event with it, the OTel
span carries it, and ``studium.llm.traces`` passes it to Langfuse.

**A ContextVar rather than a request-scoped object passed down.** The alternative
is threading a request id through every agent method, every effect handler and
every job -- which is the change that gets half-made and leaves the interesting
half of the call graph unlabelled. ContextVars propagate into ``asyncio``
tasks automatically, which is what the streaming path needs: the Orchestrator's
turn runs as a task and its LLM calls have to carry the same id as the request
that started them.

**``asyncio.to_thread`` propagates context too**, which matters for the
retention scheduler and anything else that offloads blocking SQLAlchemy.

**An inbound header is honoured, and this is a deliberate trust decision.**
Frontend §4 proxies every API call through a Next route, so the two halves of a
learner action are two HTTP requests and the interesting question -- "what did
this click do" -- spans both. Letting the proxy set the id is what joins them.
The id is not a credential, is never used for authorization, and is truncated
and character-restricted here so a header cannot inject newlines into logs.
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import Any

#: The header the frontend proxy sets and this middleware honours. ``X-Request-
#: ID`` rather than a Studium-specific name because Fly, most proxies and every
#: log aggregator already know it.
CORRELATION_HEADER = "X-Request-ID"

#: Longer than a UUID is a client trying to write an essay into every log line.
MAX_LENGTH = 64

_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")

#: Substituted for anything a record's ``correlation_id`` would otherwise be
#: missing entirely. A formatter that references a missing attribute raises,
#: and a logging setup that raises is one you find out about while reading
#: logs to understand something else.
NO_CORRELATION_ID = "-"

_correlation_id: ContextVar[str] = ContextVar("studium_correlation_id", default="")


def correlation_id() -> str:
    """The current id, or empty string outside a request."""
    return _correlation_id.get()


def set_correlation_id(value: str) -> str:
    cleaned = sanitize(value) or new_correlation_id()
    _correlation_id.set(cleaned)
    return cleaned


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def sanitize(value: str | None) -> str:
    """Return the value if it is a usable id, else the empty string.

    **Rejects rather than repairs**, in both directions:

    * A character outside ``_ALLOWED`` means the value is not an opaque token
      -- a correlation id containing a newline is not one somebody meant to
      send, and this value ends up in log lines on both sides of the proxy.
    * **Over-length is rejected rather than truncated**, which is the less
      obvious half. Truncating a 65-character id to 64 makes two different long
      ids collide, so a caller that sent distinct ids gets one -- silently, and
      only in the logs, which is where you would go to notice.

      It also has to match the frontend. ``resolveCorrelationId`` in
      ``studium-web/app/api/backend/[...path]/route.ts`` rejects at the same
      length and generates a fresh id. If this side truncated instead, the
      proxy and the backend would disagree about what id a request travelled
      under, which defeats the one thing the header exists to do.
    """
    if not value:
        return ""
    trimmed = value.strip()
    if not trimmed or len(trimmed) > MAX_LENGTH:
        return ""
    return trimmed if all(c in _ALLOWED for c in trimmed) else ""


_installed = False
_previous_factory: Any = None


def install_log_filter() -> None:
    """Put ``correlation_id`` on every record, from wherever it was logged.

    **A log-record factory rather than a ``logging.Filter`` on the root
    logger**, and the difference is not stylistic -- the obvious version does
    not work.

    A ``Filter`` attached to a logger runs only for records created *by that
    logger*. Records from ``logging.getLogger("studium.agents.lecturer")``
    propagate to the root logger's *handlers*, and propagation does not re-run
    the ancestor logger's filters. So ``logging.getLogger().addFilter(...)``
    decorates records logged directly to root -- of which this codebase
    produces almost none -- and silently misses every line anyone would want to
    correlate.

    Attaching to each handler instead would work and would depend on the
    handlers existing when this runs, which in a uvicorn process they do not
    yet. The factory runs at ``LogRecord`` construction, before any of that.

    Idempotent, and it chains rather than replaces: another library's factory
    stays in the chain.
    """
    global _installed, _previous_factory
    if _installed:
        return

    _previous_factory = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = _previous_factory(*args, **kwargs)
        record.correlation_id = correlation_id() or NO_CORRELATION_ID
        return record

    logging.setLogRecordFactory(factory)
    _installed = True


def uninstall_log_filter() -> None:
    """Restore the previous factory. For tests; never called in a deployment."""
    global _installed, _previous_factory
    if not _installed:
        return
    logging.setLogRecordFactory(_previous_factory)
    _installed = False
    _previous_factory = None


class CorrelationIdMiddleware:
    """Pure ASGI middleware. Sets the id and echoes it on the response.

    ASGI rather than ``BaseHTTPMiddleware``: Starlette's base class wraps the
    response body in an anyio memory stream, which buffers server-sent events
    and turns a token-by-token lecture into one block that arrives at the end.
    Agent runtime §20's whole streaming contract dies quietly under it, and the
    symptom -- "the server seems to hang, then everything appears" -- is
    indistinguishable from a slow model. Not a hypothetical: ``take_turn``
    already sets ``X-Accel-Buffering: no`` against the same failure one layer
    further out.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = ""
        for name, value in scope.get("headers", []):
            if name.decode("latin-1").lower() == CORRELATION_HEADER.lower():
                inbound = value.decode("latin-1")
                break

        current = set_correlation_id(inbound)
        encoded = current.encode("latin-1")

        async def send_with_header(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((CORRELATION_HEADER.lower().encode("latin-1"), encoded))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_header)

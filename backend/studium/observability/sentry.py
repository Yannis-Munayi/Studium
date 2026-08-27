"""Sentry, with the PII stripped (infrastructure §7.2).

§7.2: "PII scrubbing enabled -- learner utterances, session content, and cost
figures are stripped from error contexts before send."

**Why cost figures are on that list, which reads oddly next to the other two.**
An exception raised inside a budget check carries the learner's spend in its
local variables, and Sentry's default ``include_local_variables`` puts locals
on every frame. Spend is not private the way an utterance is; it is private the
way a bank balance is, and Sentry is a third-party SaaS whose access list is
not the one §6.4 audits.

**The scrubber is deny-by-shape, not deny-by-key-name.** A list of forbidden
key names ("prompt", "completion", "utterance") is a list that a new field
walks straight past -- and the field that walks past it is by definition one
nobody thought about. So :func:`before_send` strips *all* local variables and
*all* request bodies unconditionally, and re-adds the small set of things worth
having: the correlation id, the agent, the session id, the concept id. That
inverts the failure mode. A new field is missing from Sentry until someone adds
it deliberately, rather than present until someone notices.

The cost of the inversion is real and worth naming: debugging from a Sentry
event alone gets harder, because the local that would have explained it is
gone. The correlation id is the answer -- it leads to the Langfuse trace, which
has the prompt, behind an access list that is audited.

**Absent or unconfigured, everything here is a no-op.** No DSN, no ``sentry_sdk``
installed, an SDK that raises on init: all three log at debug and return False.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

DSN_ENV = "SENTRY_DSN"

#: §7.2: "Performance traces for a sampled subset of requests (10% at MVP;
#: sampling rate adjustable)."
DEFAULT_TRACES_SAMPLE_RATE = 0.10

#: Event context that survives scrubbing, because none of it is learner content
#: and all of it is what makes an event actionable.
SAFE_TAGS = ("correlation_id", "agent", "kind", "model", "session_id", "concept_id")

#: Exception types §7.2 names as their own alert rules. Tagged so the Sentry
#: rule can match on a tag rather than on a message substring, which is the
#: matcher that breaks the first time someone rewords an error.
ALERTING_EXCEPTIONS = ("BudgetExceededError", "MissingProvenance")


def before_send(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any]:
    """Strip everything, then re-add what is safe. See the module docstring.

    Pure and importable without the SDK, so the Tier 1 test can feed it a
    realistic event and assert on what comes out. A scrubber that can only be
    tested by sending something to Sentry is a scrubber nobody tests.
    """
    for entry in event.get("exception", {}).get("values", []):
        for frame in entry.get("stacktrace", {}).get("frames", []):
            frame.pop("vars", None)

    for entry in event.get("threads", {}).get("values", []):
        for frame in entry.get("stacktrace", {}).get("frames", []):
            frame.pop("vars", None)

    request = event.get("request")
    if isinstance(request, dict):
        # The body of POST /api/session/{id}/turn is the learner's utterance.
        request.pop("data", None)
        request.pop("cookies", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {
                name: value
                for name, value in headers.items()
                if name.lower() in {"user-agent", "content-type", CORRELATION_HEADER_LOWER}
            }

    # Breadcrumbs are log records, and log records carry whatever a developer
    # interpolated into a message. Kept, with the message truncated: the
    # sequence of events is most of a breadcrumb's value and the tail of a long
    # interpolated string is most of its risk.
    breadcrumbs = event.get("breadcrumbs")
    values = breadcrumbs.get("values") if isinstance(breadcrumbs, dict) else None
    for crumb in values or []:
        crumb.pop("data", None)
        message = crumb.get("message")
        if isinstance(message, str) and len(message) > 200:
            crumb["message"] = message[:200] + " [truncated]"

    event.pop("user", None)
    extra = event.get("extra")
    if isinstance(extra, dict):
        event["extra"] = {k: v for k, v in extra.items() if k in SAFE_TAGS}

    tags = event.setdefault("tags", {})
    from .correlation import correlation_id

    current = correlation_id()
    if current:
        tags["correlation_id"] = current

    for entry in event.get("exception", {}).get("values", []):
        if entry.get("type") in ALERTING_EXCEPTIONS:
            tags["studium_alert"] = entry["type"]

    return event


CORRELATION_HEADER_LOWER = "x-request-id"


def configure(*, service: str = "studium-backend") -> bool:
    """Initialise Sentry if a DSN and the SDK are both present.

    Returns whether it came up. Never raises: an observability backend that can
    fail startup is an outage source attached to the thing meant to explain
    outages.
    """
    dsn = (os.environ.get(DSN_ENV) or "").strip()
    if not dsn:
        log.debug("Sentry disabled (%s not set)", DSN_ENV)
        return False

    try:
        import sentry_sdk  # type: ignore[import-not-found]
    except ImportError:
        log.warning(
            "%s is set but sentry-sdk is not installed; errors will reach the "
            "Fly log and nothing else. pip install -e '.[observability]'",
            DSN_ENV,
        )
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get("STUDIUM_ENV", "local"),
            release=_release(),
            traces_sample_rate=float(
                os.environ.get("SENTRY_TRACES_SAMPLE_RATE", DEFAULT_TRACES_SAMPLE_RATE)
            ),
            # Belt and braces with before_send. send_default_pii=False is
            # Sentry's own switch and covers things the scrubber does not see;
            # include_local_variables=False stops the frames being collected at
            # all, which is cheaper than collecting and stripping them.
            send_default_pii=False,
            include_local_variables=False,
            max_breadcrumbs=30,
            before_send=before_send,
        )
        sentry_sdk.set_tag("service", service)
    except Exception:  # noqa: BLE001 -- must not fail startup
        log.exception("Sentry init failed; continuing without it")
        return False

    log.info("Sentry enabled for %s", service)
    return True


def _release() -> str:
    """The release Sentry groups regressions by.

    Fly sets ``FLY_MACHINE_VERSION`` per release; falling back to the package
    version means a local run does not report as a deployed one.
    """
    fly = os.environ.get("FLY_MACHINE_VERSION") or os.environ.get("FLY_IMAGE_REF")
    if fly:
        return fly
    try:
        from importlib.metadata import version

        return f"studium-backend@{version('studium-backend')}"
    except Exception:  # noqa: BLE001
        return "studium-backend@unknown"


def capture(exc: BaseException, **tags: str) -> None:
    """Report an exception the code has decided to handle.

    For failures that are caught and degraded -- a Voyage timeout, a Langfuse
    emit that failed -- where the process continues and nothing would otherwise
    reach Sentry. A no-op without the SDK.
    """
    try:
        import sentry_sdk  # type: ignore[import-not-found]
    except ImportError:
        return
    try:
        with sentry_sdk.push_scope() as scope:
            for key, value in tags.items():
                scope.set_tag(key, value)
            sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001
        log.debug("Sentry capture failed", exc_info=True)

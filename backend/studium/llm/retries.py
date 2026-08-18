"""Retry policy and backoff (agent runtime §21).

§21 specifies one retry with 500ms then 2s backoff for timeouts and 5xx, a
``Retry-After``-honouring retry for 429, and a reworded single retry for content
filter trips and structured-output parse failures. Anything past that degrades
into learner-visible copy rather than an exception.

**The SDK's own retries are turned off** (``max_retries=0`` in
:mod:`studium.llm.client`) so this module is the only place attempts happen.
Leaving both on would multiply: the SDK's default 2 retries inside our 1 gives
6 attempts on a hard outage, each holding a request slot, with a tail latency
the learner reads as the app having hung. One retry policy, in one place, that
the traces can account for.

Every classification here is on the SDK's typed exception classes rather than
on message text, so a reworded API error does not silently change the policy.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anthropic

log = logging.getLogger(__name__)

#: §21: "Retry once with exponential backoff (500ms, then 2s)."
BACKOFF_SECONDS: tuple[float, ...] = (0.5, 2.0)

#: Ceiling on a server-supplied Retry-After. A minute-long hold inside a turn
#: is worse for the learner than a degradation message they can act on.
MAX_RETRY_AFTER_SECONDS = 30.0

#: Jitter fraction. Without it, every session that hit one outage retries in
#: lockstep and reproduces the thundering herd that caused it.
JITTER = 0.25


class DegradedCall(RuntimeError):
    """A model call exhausted its retries.

    Carries the failure class so the Orchestrator can pick the right
    degradation copy without re-inspecting the underlying exception.
    """

    def __init__(self, kind: str, attempts: int, cause: BaseException | None = None):
        self.kind = kind
        self.attempts = attempts
        self.__cause__ = cause
        super().__init__(f"{kind} after {attempts} attempt(s)")


@dataclass(frozen=True, slots=True)
class Attempt:
    """One attempt's outcome, recorded for the trace."""

    index: int
    failed_with: str | None = None


def classify(exc: BaseException) -> str:
    """Map an exception to a §21 failure class.

    ``unknown`` is deliberately not retried: an unrecognised error is as likely
    a bug in our request as a transient fault, and retrying a malformed request
    just bills for it twice.
    """
    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limit"
    if isinstance(exc, anthropic.APITimeoutError):
        return "timeout"
    if isinstance(exc, anthropic.APIConnectionError):
        return "connection"
    if isinstance(exc, anthropic.APIStatusError):
        return "server_error" if exc.status_code >= 500 else "bad_request"
    return "unknown"


#: Failure classes worth a second attempt.
RETRYABLE = frozenset({"rate_limit", "timeout", "connection", "server_error"})


def retry_after_seconds(exc: BaseException) -> float | None:
    """Read ``Retry-After`` off a 429, clamped and defended against garbage."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return min(float(raw), MAX_RETRY_AFTER_SECONDS)
    except (TypeError, ValueError):
        # Retry-After may be an HTTP-date. Falling back to the standard backoff
        # is better than parsing dates to save one request.
        return None


def _delay(attempt: int, exc: BaseException) -> float:
    base = retry_after_seconds(exc) if classify(exc) == "rate_limit" else None
    if base is None:
        base = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
    return base * (1 + random.uniform(-JITTER, JITTER))  # noqa: S311 -- not crypto


async def with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    what: str,
    max_attempts: int = 2,
    on_attempt: Callable[[Attempt], None] | None = None,
) -> T:
    """Run ``operation``, retrying transient failures per §21.

    ``max_attempts=2`` is "try, then retry once". Raises :class:`DegradedCall`
    when attempts run out, so callers handle one exception type rather than the
    SDK's whole hierarchy.
    """
    last: BaseException | None = None
    kind = "unknown"

    for attempt in range(max_attempts):
        try:
            result = await operation()
        except Exception as exc:  # noqa: BLE001 -- classified and re-raised below
            kind = classify(exc)
            last = exc
            if on_attempt:
                on_attempt(Attempt(index=attempt, failed_with=kind))

            if kind not in RETRYABLE or attempt == max_attempts - 1:
                log.warning(
                    "%s failed (%s) on attempt %d/%d; giving up",
                    what, kind, attempt + 1, max_attempts,
                )
                raise DegradedCall(kind, attempt + 1, exc) from exc

            delay = _delay(attempt, exc)
            log.info(
                "%s failed (%s) on attempt %d/%d; retrying in %.2fs",
                what, kind, attempt + 1, max_attempts, delay,
            )
            await asyncio.sleep(delay)
        else:
            if on_attempt:
                on_attempt(Attempt(index=attempt))
            return result

    raise DegradedCall(kind, max_attempts, last)  # pragma: no cover -- loop always returns

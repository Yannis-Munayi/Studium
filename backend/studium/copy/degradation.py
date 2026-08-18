"""Learner-visible copy for every failure mode (agent runtime §21).

Centralised so tone is consistent and localisable later. §3: "When a model call
fails after retries, when a rate limit is hit, when a budget cap trips, the
learner sees a coherent response ... never a raw error."

Two rules the wording follows throughout:

* **Say what happened and what to do.** "Try again in a moment" is actionable;
  "an error occurred" is not.
* **Never blame the learner and never expose the mechanism.** A content filter
  trip reads as "let me try that differently", not as a policy citation. The
  learner did nothing wrong and cannot act on the internals.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

#: Keyed by the failure classes studium.llm.retries.classify produces, plus the
#: runtime-level failures that never reach the SDK.
MESSAGES: dict[str, str] = {
    "timeout": (
        "The tutor is thinking longer than usual. Give it a moment and try "
        "again, or repeat what you said."
    ),
    "server_error": (
        "The tutor is thinking longer than usual. Give it a moment and try "
        "again, or repeat what you said."
    ),
    "connection": (
        "The connection dropped mid-thought. Your progress is saved -- repeat "
        "your last message and we'll pick up from there."
    ),
    "rate_limit": (
        "There's high demand right now, so responses are slower than usual. "
        "Try again in a moment."
    ),
    "content_filter": (
        "Something in that exchange tripped a safety filter. Try rephrasing and "
        "we'll continue."
    ),
    "parse_failure": (
        "The tutor's reply came back malformed. Ask that again and it should "
        "come through cleanly."
    ),
    "effect_write_failure": (
        "Your last answer was received but couldn't be recorded. Tap confirm to "
        "retry -- nothing is lost."
    ),
    "client_disconnect": (
        "The connection closed mid-answer. What was delivered is saved; ask to "
        "resume when you're ready."
    ),
    "bad_request": (
        "Something went wrong on our side handling that. It's been logged. Try "
        "again, or move on and come back to it."
    ),
    "unknown": (
        "Something went wrong on our side. It's been logged. Try again in a "
        "moment."
    ),
    "no_focus_concept": (
        "There's no concept in focus for this session yet. Pick a topic to start "
        "on and we'll go from there."
    ),
}

DEFAULT = MESSAGES["unknown"]


def for_failure(kind: str) -> str:
    """Copy for a failure class."""
    return MESSAGES.get(kind, DEFAULT)


def budget_exceeded(
    *,
    scope: str,
    reset_at: dt.datetime,
    timezone: str = "America/Toronto",
) -> str:
    """§21's budget copy, with the reset time in the learner's own timezone.

    A UTC timestamp here would be technically accurate and useless -- the
    learner needs to know whether to come back tonight or tomorrow.
    """
    try:
        local = reset_at.astimezone(ZoneInfo(timezone))
    except Exception:  # noqa: BLE001 -- an unknown tz must not fail the message
        local = reset_at

    # Formatted by hand rather than with strftime's %-I / %-d. Those are glibc
    # extensions: they raise ValueError on Windows, which would turn a budget
    # notice into a 500 on any non-Linux host.
    if scope == "daily":
        hour = local.hour % 12 or 12
        meridiem = "am" if local.hour < 12 else "pm"
        when = f"{hour}:{local.minute:02d} {meridiem}"
    else:
        when = f"{local.day} {local.strftime('%B')}"

    horizon = "tomorrow" if scope == "daily" else "next month"

    return (
        f"You've reached your {scope} usage limit. Your progress is saved -- "
        f"resume {horizon} (resets {when})."
    )


def budget_warning(*, scope: str, spent_usd: float, limit_usd: float) -> str:
    """Soft-cap notice. Not a block; the session continues (§19)."""
    return (
        f"Heads up: you've used ${spent_usd:.2f} of your ${limit_usd:.2f} "
        f"{scope} budget. Nothing stops yet."
    )


def start_of_tomorrow(*, timezone: str = "America/Toronto") -> dt.datetime:
    """Midnight tonight, in the learner's timezone."""
    tz = ZoneInfo(timezone)
    now = dt.datetime.now(tz)
    return (now + dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def start_of_next_month(*, timezone: str = "America/Toronto") -> dt.datetime:
    tz = ZoneInfo(timezone)
    now = dt.datetime.now(tz)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return now.replace(
        year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0
    )

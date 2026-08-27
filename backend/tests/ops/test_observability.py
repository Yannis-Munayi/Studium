"""Observability wiring (infrastructure §7).

Two things are worth testing here and they are not the SDKs.

**The scrubber.** §7.2 strips "learner utterances, session content, and cost
figures" before send. :func:`sentry.before_send` is pure and importable without
the SDK precisely so it can be fed a realistic event and asserted on — a
scrubber that can only be tested by sending something to Sentry is a scrubber
nobody tests.

**That everything degrades to nothing.** §7's value is real and it is not worth
a learner's turn. The claim is asserted by blocking the imports and driving
``configure`` to completion, rather than by believing the try/except blocks.
"""

from __future__ import annotations

import importlib.abc
import logging
import sys
from collections.abc import Callable, Iterator

import pytest

from studium.observability import correlation
from studium.observability import sentry as sentry_module

# --- correlation ids -------------------------------------------------------


def test_an_id_is_set_and_read_back() -> None:
    correlation.set_correlation_id("abc-123")
    assert correlation.correlation_id() == "abc-123"


@pytest.mark.parametrize(
    "value",
    [
        "with space",
        "line\nbreak",
        "semi;colon",
        "a" * 65,
        "",
        None,
        "../../etc",
        "<script>",
    ],
)
def test_hostile_inbound_ids_are_replaced_not_escaped(value: str | None) -> None:
    """The header comes from a client and lands in every log line under the
    request.

    Replaced rather than escaped: a correlation id containing a newline is not
    one anybody meant to send, and accepting a sanitised version of it would
    make two different requests share an id.
    """
    assert correlation.sanitize(value) == ""
    generated = correlation.set_correlation_id(value or "")
    assert generated and correlation.sanitize(generated) == generated


def test_a_well_formed_inbound_id_survives() -> None:
    """Frontend §4 proxies every call, so a learner action is two requests.

    Honouring the proxy's id is what joins them; generating a fresh one here
    would make "what did this click do" two searches by timestamp.
    """
    assert correlation.sanitize("0198fb2c4d1e7a9b") == "0198fb2c4d1e7a9b"
    assert correlation.sanitize("req_ABC-123.4") == "req_ABC-123.4"


@pytest.fixture
def captured() -> Iterator[Callable[[str, str], logging.LogRecord]]:
    """Log one line through a named logger and hand back the record.

    A handler attached to the logger under test rather than pytest's
    ``caplog``. ``caplog`` works through the *root* logger's level and a
    handler installed per phase, so whether it sees a record depends on global
    logging state that 2,000 other tests in this suite are also free to touch.

    Writing it this way is what turned an unreproducible flake into a real
    finding. The first version used ``caplog`` and failed only in runs where
    the migration-sequence tests had already driven Alembic in the same
    process -- Alembic's ``fileConfig`` call defaulted to
    ``disable_existing_loggers=True``, which sets ``.disabled = True`` on every
    ``studium.*`` logger that already existed. The observability code was
    correct; the logger it wrote to had been switched off. See
    ``migrations/env.py`` and ``test_alembic_does_not_disable_existing_loggers``
    below.

    The assertion message carries the state it would need to diagnose that
    again, because the next instance will look identical from the outside.
    """
    handlers: list[tuple[logging.Logger, logging.Handler, int, bool]] = []

    def log_one(logger_name: str, message: str) -> logging.LogRecord:
        logger = logging.getLogger(logger_name)
        handler = logging.Handler()
        handler.setLevel(logging.DEBUG)
        records: list[logging.LogRecord] = []
        handler.emit = records.append  # type: ignore[method-assign]

        handlers.append((logger, handler, logger.level, logger.propagate))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        # Isolated from the root logger entirely, so nothing another test did
        # to root's level or handlers can reach this.
        logger.propagate = False

        logger.info(message)
        assert records, (
            f"{logger_name} produced no record with a handler attached "
            f"directly to it. Something switched logging off globally: "
            f"disabled={logger.disabled} "
            f"manager.disable={logging.root.manager.disable} "
            f"level={logger.level} effective={logger.getEffectiveLevel()} "
            f"factory={logging.getLogRecordFactory()}"
        )
        return records[-1]

    try:
        yield log_one
    finally:
        for logger, handler, level, propagate in handlers:
            logger.removeHandler(handler)
            logger.setLevel(level)
            logger.propagate = propagate


def test_the_id_reaches_records_from_a_child_logger(captured) -> None:
    """The failure the obvious implementation has.

    A ``logging.Filter`` on the root logger runs only for records created *by*
    the root logger. Every line in this codebase comes from a module logger and
    propagates to root's *handlers*, and propagation does not re-run the
    ancestor logger's filters -- so the obvious version decorates nothing
    anyone would want to correlate, and reports success while doing it. The
    record-factory implementation runs at construction and has no such gap.
    """
    correlation.install_log_filter()
    correlation.set_correlation_id("deadbeef")
    record = captured("studium.agents.lecturer", "hello")
    assert record.correlation_id == "deadbeef"


def test_records_outside_a_request_carry_a_dash(captured) -> None:
    """A missing attribute crashes a formatter that references it.

    Which is the failure mode where adding correlation ids takes down logging,
    and logging is what you read when something has gone wrong.
    """
    correlation.install_log_filter()
    correlation._correlation_id.set("")
    record = captured("studium.jobs.retention", "nightly")
    assert record.correlation_id == correlation.NO_CORRELATION_ID


def test_install_log_filter_is_idempotent_and_chains() -> None:
    """The lifespan runs more than once in a test process.

    Chaining rather than replacing matters too: another library's factory has
    to survive, or installing this one silently drops whatever it was adding.
    """
    correlation.uninstall_log_filter()
    marker = object()
    base = logging.getLogRecordFactory()

    def other(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        record = base(*args, **kwargs)
        record.other_marker = marker
        return record

    logging.setLogRecordFactory(other)
    try:
        correlation.install_log_filter()
        correlation.install_log_filter()
        record = logging.getLogRecordFactory()(
            "t", logging.INFO, __file__, 1, "m", None, None
        )
        assert record.other_marker is marker, "the previous factory was replaced"
        assert hasattr(record, "correlation_id")
    finally:
        correlation.uninstall_log_filter()
        logging.setLogRecordFactory(base)
        correlation.install_log_filter()


def test_alembic_does_not_disable_existing_loggers() -> None:
    """``fileConfig`` defaults to switching off every logger that already exists.

    Alembic's generated ``env.py`` calls ``fileConfig(config.config_file_name)``
    with no keyword, and ``logging.config.fileConfig`` defaults
    ``disable_existing_loggers`` to True — which sets ``.disabled = True`` on
    every non-root logger created before it runs. Permanently, with no error
    and no log line saying so.

    In its own process (the deploy's release command) nothing else exists yet
    and it is harmless. In-process it silences the entire application's
    logging, and the symptom is that logs stop rather than that anything fails.

    Asserted against the file because there is no way to observe the flag
    afterwards: the damage is indistinguishable from a quiet system.
    """
    from pathlib import Path

    env = (Path(__file__).resolve().parents[2] / "migrations" / "env.py").read_text(
        encoding="utf-8"
    )
    assert "disable_existing_loggers=False" in env, (
        "migrations/env.py calls fileConfig without disable_existing_loggers="
        "False. Any in-process Alembic run will switch off every studium.* "
        "logger created before it."
    )


def test_the_middleware_is_pure_asgi() -> None:
    """Not BaseHTTPMiddleware, and this is load-bearing.

    Starlette's base class buffers the response body in an anyio memory
    stream, which turns agent runtime §20's token-by-token lecture into one
    block arriving at the end -- indistinguishable from a slow model. The class
    below has to stay a plain ASGI callable.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    assert not issubclass(correlation.CorrelationIdMiddleware, BaseHTTPMiddleware)
    assert callable(correlation.CorrelationIdMiddleware.__call__)


# --- the Sentry scrubber ---------------------------------------------------


def _event_with_everything() -> dict:
    """A realistic event: locals on a frame, a request body, breadcrumbs, a user."""
    return {
        "exception": {
            "values": [
                {
                    "type": "ValueError",
                    "stacktrace": {
                        "frames": [
                            {
                                "function": "handle_turn",
                                "vars": {
                                    "learner_text": "I don't understand beta reduction",
                                    "cost_usd": "0.4231",
                                    "session_id": "abc",
                                },
                            }
                        ]
                    },
                }
            ]
        },
        "request": {
            "url": "https://studium.app/api/session/x/turn",
            "data": {"text": "why is this wrong"},
            "cookies": {"studium-learner": "uuid"},
            "headers": {
                "User-Agent": "Mozilla",
                "Authorization": "Bearer secret",
                "X-Studium-User": "a-real-uuid",
                "Content-Type": "application/json",
            },
        },
        "breadcrumbs": {
            "values": [
                {"message": "x" * 500, "data": {"prompt": "the whole system prompt"}}
            ]
        },
        "user": {"id": "a-real-uuid", "email": "learner@example.com"},
        "extra": {"completion": "the model's whole answer", "agent": "lecturer"},
    }


def test_local_variables_are_stripped() -> None:
    """§7.2's three categories all live here, and the deny is by shape.

    A list of forbidden key names is a list a new field walks straight past --
    and the field that walks past it is by definition one nobody thought about.
    """
    event = sentry_module.before_send(_event_with_everything())
    frame = event["exception"]["values"][0]["stacktrace"]["frames"][0]
    assert "vars" not in frame


def test_the_request_body_and_cookies_are_stripped() -> None:
    """The body of POST /turn *is* the learner's utterance."""
    event = sentry_module.before_send(_event_with_everything())
    assert "data" not in event["request"]
    assert "cookies" not in event["request"]


def test_only_innocuous_headers_survive() -> None:
    """An allow-list, so a header added later is absent until someone adds it."""
    event = sentry_module.before_send(_event_with_everything())
    headers = event["request"]["headers"]
    assert set(headers) <= {"User-Agent", "Content-Type", "x-request-id"}
    assert "Authorization" not in headers
    assert "X-Studium-User" not in headers


def test_the_user_block_is_removed() -> None:
    event = sentry_module.before_send(_event_with_everything())
    assert "user" not in event


def test_extra_is_reduced_to_the_safe_tags() -> None:
    event = sentry_module.before_send(_event_with_everything())
    assert event["extra"] == {"agent": "lecturer"}
    assert "completion" not in event["extra"]


def test_breadcrumb_data_goes_and_long_messages_are_truncated() -> None:
    """Breadcrumbs are log records, and log records carry interpolated strings.

    Kept, because the sequence of events is most of a breadcrumb's value;
    truncated, because the tail of a long interpolated string is most of its
    risk.
    """
    event = sentry_module.before_send(_event_with_everything())
    crumb = event["breadcrumbs"]["values"][0]
    assert "data" not in crumb
    assert crumb["message"].endswith("[truncated]")
    assert len(crumb["message"]) < 300


def test_the_correlation_id_is_tagged() -> None:
    correlation.set_correlation_id("trace-me")
    event = sentry_module.before_send(_event_with_everything())
    assert event["tags"]["correlation_id"] == "trace-me"


def test_the_alerting_exceptions_get_a_tag() -> None:
    """§7.2 configures Sentry rules on two specific exceptions.

    A tag rather than a message substring: a substring matcher breaks the first
    time someone rewords an error, and it breaks silently -- the rule stops
    matching and no alert fires, which looks exactly like nothing going wrong.
    """
    event = {"exception": {"values": [{"type": "BudgetExceededError"}]}}
    assert sentry_module.before_send(event)["tags"]["studium_alert"] == (
        "BudgetExceededError"
    )
    event = {"exception": {"values": [{"type": "MissingProvenance"}]}}
    assert sentry_module.before_send(event)["tags"]["studium_alert"] == (
        "MissingProvenance"
    )


def test_the_scrubber_survives_a_minimal_event() -> None:
    """Sentry sends message events with none of the blocks above."""
    assert sentry_module.before_send({}) == {"tags": {}} or "tags" in sentry_module.before_send({})


def test_sentry_is_off_without_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert sentry_module.configure() is False


# --- degradation -----------------------------------------------------------


class _Blocker(importlib.abc.MetaPathFinder):
    def __init__(self, blocked: set[str]) -> None:
        self.blocked = blocked

    def find_spec(self, name, path, target=None):  # noqa: ANN001, ANN201
        if name.split(".")[0] in self.blocked:
            raise ImportError(f"{name} is not installed (simulated)")
        return None


@pytest.fixture
def without_sdks() -> Iterator[None]:
    blocked = {"sentry_sdk", "opentelemetry", "langfuse"}
    finder = _Blocker(blocked)
    saved = {name: mod for name, mod in sys.modules.items() if name.split(".")[0] in blocked}
    for name in saved:
        del sys.modules[name]
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        sys.modules.update(saved)


def test_configure_completes_with_no_sdks_installed(
    without_sdks: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim §7 rests on, asserted rather than assumed.

    Configured but uninstallable is the case that would raise if anything
    raised: an operator set the DSN and the extra was not installed.
    """
    monkeypatch.setenv("SENTRY_DSN", "https://key@example.ingest.sentry.io/1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.invalid/otel")

    import studium.llm.traces as traces

    monkeypatch.setattr(traces, "_langfuse_checked", True)
    monkeypatch.setattr(traces, "_langfuse_client", None)

    from studium import observability

    state = observability.configure(None)
    assert state == {
        "correlation_ids": True,
        "sentry": False,
        "opentelemetry": False,
        "langfuse": False,
    }


def test_correlation_ids_work_without_any_sdk(without_sdks: None) -> None:
    """The load-bearing piece needs no SDK at all.

    Without it, §3's "navigate from a turn to the trace to the request to the
    error" is four searches by timestamp — which is the state a deployment with
    no observability budget would be in, and it should still have this.
    """
    correlation.set_correlation_id("still-works")
    assert correlation.correlation_id() == "still-works"


def test_otel_trace_id_is_empty_without_the_sdk(without_sdks: None) -> None:
    from studium.observability import otel

    assert otel.current_trace_id() == ""


def test_health_is_excluded_from_tracing() -> None:
    """Uptime Robot polls it every 5 minutes and Fly far more often.

    A trace store whose contents are mostly health checks is one nobody scrolls
    through twice.
    """
    from studium.observability import otel

    assert "health" in otel.EXCLUDED_URLS

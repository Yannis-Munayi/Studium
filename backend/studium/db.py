"""Engine, session factory, and transaction helpers (spec §9).

Isolation policy:

* ``READ COMMITTED`` by default -- sufficient for essentially every query.
* ``REPEATABLE READ`` with bounded retry for the read-modify-write cycle on
  ``concept_mastery``. If two agent turns produce evidence for the same
  concept concurrently, both increments must land. Retrying is preferable to
  pessimistic row locking because contention is rare and retries are cheap.

Every request handler opens exactly one transaction. Long-running operations
(LLM calls) commit state-mutating writes *before* the call and start a new
transaction after, so a dropped connection mid-call cannot lose written state.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

#: Postgres error code for "could not serialize access due to concurrent update".
SERIALIZATION_FAILURE = "40001"
DEADLOCK_DETECTED = "40P01"

MAX_RETRY_ATTEMPTS = 3


def _make_engine(url: str) -> Engine:
    return create_engine(
        url,
        echo=settings.echo_sql,
        pool_pre_ping=True,
        # PgBouncer in transaction mode forbids session-scoped state. asyncpg
        # and psycopg both keep a client-side prepared-statement cache by
        # default, which is exactly that -- disable it so the same code works
        # with and without the pooler in front.
        connect_args={"prepare_threshold": None},
    )


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def owner_engine() -> Engine:
    """Engine for the retention, erasure and cost-roll-up jobs.

    These must DELETE from append-only tables, which migration 0003 forbids to
    the application role.
    """
    return _make_engine(settings.jobs_database_url)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one transaction per request."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def repeatable_read(session: Session) -> Iterator[Session]:
    """Run a block at REPEATABLE READ.

    Caller is responsible for commit; ``serializable_retry`` wraps this with
    the retry loop.
    """
    session.rollback()  # cannot change isolation mid-transaction
    session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    try:
        yield session
    finally:
        session.rollback()
        session.connection(execution_options={"isolation_level": "READ COMMITTED"})


def is_serialization_error(exc: BaseException) -> bool:
    if not isinstance(exc, (DBAPIError, OperationalError)):
        return False
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return sqlstate in (SERIALIZATION_FAILURE, DEADLOCK_DETECTED)


def with_serialization_retry(fn, *args, attempts: int = MAX_RETRY_ATTEMPTS, **kwargs):
    """Call ``fn`` under REPEATABLE READ, retrying serialization failures.

    Exponential backoff, capped attempts. Beyond that the caller sees the
    original error rather than a silently dropped write.
    """
    delay = 0.02
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- re-raised below
            if not is_serialization_error(exc):
                raise
            last = exc
            if attempt < attempts - 1:
                time.sleep(delay)
                delay *= 2
    assert last is not None
    raise last


def assert_server_version(session: Session) -> None:
    """Startup check: §4 pins Postgres 16.x, and several things here need it.

    NULLS NOT DISTINCT on the cost-ledger unique constraint is 15+, and the
    partial-index and expression-index forms used throughout assume modern
    planner behaviour.
    """
    raw = session.execute(text("SHOW server_version_num")).scalar_one()
    num = int(raw)
    major, minor = divmod(num, 10000)[0], (num % 10000) // 100
    if (major, minor) < settings.minimum_server_version:
        raise RuntimeError(
            f"Postgres {settings.minimum_server_version[0]}+ required, found {major}.{minor}"
        )


def assert_tls(session: Session) -> None:
    """§11: all Postgres connections require TLS, asserted at startup."""
    row = session.execute(
        text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
    ).scalar()
    if row is False:
        raise RuntimeError("database connection is not using TLS")

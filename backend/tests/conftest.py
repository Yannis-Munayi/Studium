"""Test configuration (spec §14).

Two tiers:

* **Offline** -- schema conventions, enum stability, migration hygiene, and the
  pure logic in mastery/assessment/fsrs/acl. These read the SQLAlchemy metadata
  and the migration files, so they run in CI with no database. They are the
  tests that catch a missing FK index or a silently renamed enum value.
* **Postgres-backed** -- integrity and query-shape tests, marked
  ``@pytest.mark.postgres``. Skipped with a clear reason when no database is
  reachable, rather than failing and training everyone to ignore red.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

TEST_DB_ENV = "STUDIUM_TEST_DATABASE_URL"
DEFAULT_TEST_URL = "postgresql+psycopg://studium:studium@localhost:5432/studium_test"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "postgres: requires a live Postgres 16 with the schema applied"
    )


def _test_url() -> str:
    return os.environ.get(TEST_DB_ENV, DEFAULT_TEST_URL)


#: Fail fast when nothing is listening. Without this the driver waits on the
#: OS TCP timeout, which on Windows is long enough to look like a hang.
CONNECT_TIMEOUT_SECONDS = 3


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = _test_url()
    eng = create_engine(
        url,
        future=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 -- reported as a skip, not a failure
        eng.dispose()
        pytest.skip(
            f"no Postgres at {url!r} ({type(exc).__name__}). "
            f"Start one with `make db-up`, or point {TEST_DB_ENV} at a database."
        )
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    """A session in a transaction that is always rolled back.

    Every test sees the seeded fixture data and no test can leak state into
    the next one.
    """
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()

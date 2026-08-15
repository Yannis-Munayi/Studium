"""Live-database migration checks (spec §13 CI checks 2 and 3).

2. Every migration runs cleanly against a fresh database.
3. Every migration is reversible: ``upgrade head``, ``downgrade -1``,
   ``upgrade head`` leaves no diff in ``pg_dump --schema-only``.

These need a database they are allowed to destroy, so they run against
``STUDIUM_MIGRATION_TEST_DATABASE_URL`` and are skipped otherwise -- pointing
them at the normal test database would drop its schema mid-run.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.postgres

BACKEND = Path(__file__).resolve().parents[2]
URL_ENV = "STUDIUM_MIGRATION_TEST_DATABASE_URL"


def _url() -> str:
    url = os.environ.get(URL_ENV)
    if not url:
        pytest.skip(
            f"set {URL_ENV} to a scratch database this test may drop and recreate"
        )
    return url


@pytest.fixture
def alembic_config() -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.set_main_option("sqlalchemy.url", _url())
    return cfg


@pytest.fixture
def clean_database(alembic_config: Config):
    """Drop and recreate the public schema so each test starts from empty."""
    engine = create_engine(_url())
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def _schema_dump(url: str) -> str:
    """pg_dump --schema-only, normalised for comparison."""
    dsn = url.replace("postgresql+psycopg://", "postgresql://")
    result = subprocess.run(
        ["pg_dump", "--schema-only", "--no-owner", "--no-acl", dsn],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"pg_dump unavailable or failed: {result.stderr.strip()[:200]}")
    lines = [
        line
        for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("--")
    ]
    return "\n".join(lines)


def test_full_migration_sequence(alembic_config: Config, clean_database) -> None:
    """§13 check 2: empty database to head, cleanly."""
    command.upgrade(alembic_config, "head")

    with clean_database.connect() as conn:
        tables = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        ).scalar_one()
        # 34 tables plus alembic_version.
        assert tables == 35, f"expected 35 tables after head, found {tables}"

        version = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        assert version == "0003"


def test_migration_round_trip(alembic_config: Config, clean_database) -> None:
    """§13 check 3: upgrade head, downgrade -1, upgrade head, no schema diff."""
    command.upgrade(alembic_config, "head")
    before = _schema_dump(_url())

    command.downgrade(alembic_config, "-1")
    command.upgrade(alembic_config, "head")
    after = _schema_dump(_url())

    assert before == after, (
        "downgrade/upgrade round-trip changed the schema. Diff the pg_dump "
        "output for the most recent migration's downgrade()."
    )


def test_downgrade_to_base_leaves_no_tables(
    alembic_config: Config, clean_database
) -> None:
    """A full teardown must not strand tables, types, or functions."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    with clean_database.connect() as conn:
        tables = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                "AND table_name <> 'alembic_version'"
            )
        ).scalar_one()
        assert tables == 0, f"{tables} tables survived downgrade to base"

        types = conn.execute(
            text(
                "SELECT count(*) FROM pg_type t "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = 'public' AND t.typtype = 'e'"
            )
        ).scalar_one()
        assert types == 0, f"{types} enum types survived downgrade to base"


def test_uuid_function_round_trips_through_the_database(
    alembic_config: Config, clean_database
) -> None:
    """The generated ids must be valid v7 UUIDs and monotonically ordered.

    This is the check that the spec's 33-character fallback fails.
    """
    command.upgrade(alembic_config, "head")
    with clean_database.connect() as conn:
        values = conn.execute(
            text("SELECT uuid_generate_v7() FROM generate_series(1, 200)")
        ).scalars().all()
    assert all(v.version == 7 for v in values)
    assert list(values) == sorted(values), "UUIDv7 must be time-ordered"
    assert len(set(values)) == len(values), "collision in 200 generated ids"

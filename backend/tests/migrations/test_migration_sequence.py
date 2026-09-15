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
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from studium.models import Base

from ..dbprobe import CONNECT_TIMEOUT_SECONDS, require_database

pytestmark = pytest.mark.postgres

BACKEND = Path(__file__).resolve().parents[2]
URL_ENV = "STUDIUM_MIGRATION_TEST_DATABASE_URL"


def _url() -> str:
    url = os.environ.get(URL_ENV)
    if not url:
        pytest.skip(
            f"set {URL_ENV} to a scratch database this test may drop and recreate"
        )
    require_database(url, URL_ENV)
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
    engine = create_engine(
        _url(), connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS}
    )
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def _schema_dump(url: str) -> str:
    """pg_dump --schema-only, normalised for comparison."""
    dsn = url.replace("postgresql+psycopg://", "postgresql://")
    try:
        result = subprocess.run(
            ["pg_dump", "--schema-only", "--no-owner", "--no-acl", dsn],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        # Not on PATH at all -- common on Windows, where the Postgres client
        # tools are not added to it. A missing binary is an absent tool, not a
        # failing migration, so it skips like every other unmet dependency.
        pytest.skip("pg_dump is not on PATH; install the Postgres client tools")
    if result.returncode != 0:
        pytest.skip(f"pg_dump unavailable or failed: {result.stderr.strip()[:200]}")
    lines = [
        line
        for line in result.stdout.splitlines()
        if line.strip()
        and not line.startswith("--")
        # Recent pg_dump wraps output in \restrict/\unrestrict guarded by a
        # token regenerated on every invocation. Comparing two dumps without
        # dropping these means comparing two random strings, so the round-trip
        # would fail on a schema that never changed.
        and not line.startswith(("\\restrict", "\\unrestrict"))
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
        # Derived, not hard-coded: this assertion sat at "0003" for five
        # migrations because nothing made it move, and it only surfaced once
        # the suite could reach a database at all.
        expected = len(Base.metadata.tables) + 1  # + alembic_version
        assert tables == expected, f"expected {expected} tables, found {tables}"

        version = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        head = ScriptDirectory.from_config(alembic_config).get_current_head()
        assert version == head


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


def _seed_a_chain_at_0012(conn) -> dict[str, object]:
    """A learner with one credential and one work item, on the 0012 schema.

    Written as raw SQL against the columns 0012 actually has, rather than
    through the ORM: the models describe *head*, and a model-driven insert here
    would try to write `is_credential` into a table that does not have it yet.
    """
    user_id = conn.execute(
        text(
            "INSERT INTO users (email, display_name) "
            "VALUES ('backfill@example.com', 'Backfill') RETURNING id"
        )
    ).scalar_one()
    subject_id = conn.execute(
        text(
            "INSERT INTO subjects (slug, title) "
            "VALUES ('backfill-subject', 'Backfill') RETURNING id"
        )
    ).scalar_one()
    enrollment_id = conn.execute(
        text(
            "INSERT INTO learner_subjects (user_id, subject_id, subject_version) "
            "VALUES (:uid, :sid, 1) RETURNING id"
        ),
        {"uid": user_id, "sid": subject_id},
    ).scalar_one()

    ids = {}
    for index, kind in enumerate(("proof", "assessment_pass", "subject_completion")):
        ids[kind] = conn.execute(
            text(
                """
                INSERT INTO portfolio_items
                    (user_id, learner_subject_id, chain_index, kind, title, body,
                     content_sha256, signature)
                VALUES (:uid, :lsid, :idx, CAST(:kind AS portfolio_item_kind),
                        :kind, '{}', repeat('b', 64), '{}'::jsonb)
                RETURNING id
                """
            ),
            {"uid": user_id, "lsid": enrollment_id, "idx": index, "kind": kind},
        ).scalar_one()
    return ids


def test_0013_backfills_is_credential_from_existing_rows(
    alembic_config: Config, clean_database
) -> None:
    """The backfill, run over a table that is not empty.

    Every other test migrates to head before any row exists, so the
    ``UPDATE ... SET is_credential = TRUE`` never touches anything and the CHECK
    constraint is validated against nothing. On a real deployment it runs over
    whatever is already in ``portfolio_items``, and a backfill that missed would
    leave existing credentials flagged FALSE -- which is to say, deleted by the
    first erasure that reached them.
    """
    command.upgrade(alembic_config, "0012")
    with clean_database.begin() as conn:
        ids = _seed_a_chain_at_0012(conn)

    command.upgrade(alembic_config, "0013")

    with clean_database.connect() as conn:
        flags = dict(
            conn.execute(
                text("SELECT kind::text, is_credential FROM portfolio_items")
            ).all()
        )
    assert flags == {
        "proof": False,
        "assessment_pass": True,
        "subject_completion": True,
    }, ids


def test_0013_downgrade_refuses_to_destroy_retained_credentials(
    alembic_config: Config, clean_database
) -> None:
    """The one downgrade in the project that stops rather than proceeding.

    Every other test here runs a downgrade against an empty database, where
    0013's guard is trivially satisfied and therefore never exercised. A
    retained credential has no enrollment to be re-attached to -- the
    ``learner_subjects`` row went with the erasure that detached it -- so
    restoring ``learner_subject_id NOT NULL`` means deleting it, which is the
    loss the migration exists to prevent. Data layer §12.4's downgrade sign-off
    is what resolves that; the migration must not resolve it silently.
    """
    command.upgrade(alembic_config, "head")

    with clean_database.begin() as conn:
        user_id = conn.execute(
            text(
                """
                INSERT INTO users (email, display_name)
                VALUES ('detached@example.com', 'Anonymized former learner')
                RETURNING id
                """
            )
        ).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO portfolio_items
                    (user_id, learner_subject_id, chain_index, kind, title, body,
                     content_sha256, signature, is_credential)
                VALUES (:uid, NULL, 0, 'assessment_pass', 'Assessment passed',
                        '{}', repeat('a', 64), '{}'::jsonb, TRUE)
                """
            ),
            {"uid": user_id},
        )

    with pytest.raises(RuntimeError, match="right-to-erasure"):
        command.downgrade(alembic_config, "0012")

    # And the refusal left the schema alone rather than half-reversing it.
    with clean_database.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM portfolio_items")
        ).scalar_one() == 1
        assert conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'portfolio_items' "
                "  AND column_name = 'is_credential'"
            )
        ).scalar_one() == 1


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
    assert len(set(values)) == len(values), "collision in 200 generated ids"
    # Ordered to millisecond resolution; the sub-millisecond counter of
    # RFC 9562 §6.2 is optional and this implementation randomises those bits.
    stamps = [int.from_bytes(v.bytes[:6], "big") for v in values]
    assert stamps == sorted(stamps), "UUIDv7 timestamp prefix must be ordered"

"""Schema-level conventions (spec §14, "Schema-level tests").

These read ``Base.metadata`` rather than ``information_schema``, so they run
without a database and fail fast in CI. The spec describes the FK-index test as
"auto-generated ... that reads information_schema"; the metadata is the same
truth one step earlier, and catching it before the migration runs is strictly
better.
"""

from __future__ import annotations

import pytest
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID

from studium.models import SOFT_DELETE_TABLES, TRIGGERED_TABLES, Base

METADATA = Base.metadata
TABLES = sorted(METADATA.tables.values(), key=lambda t: t.name)


def _covered_by_leading_index(table, column_name: str) -> bool:
    """True when some index or unique constraint has the column *first*.

    Leading position matters: a UNIQUE (learner_subject_id, concept_id) does
    not help a cascade delete that filters on concept_id alone.
    """
    for index in table.indexes:
        cols = list(index.expressions)
        if cols and getattr(cols[0], "name", None) == column_name:
            return True
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            cols = list(constraint.columns)
            if cols and cols[0].name == column_name:
                return True
    if table.primary_key is not None:
        pk = list(table.primary_key.columns)
        if pk and pk[0].name == column_name:
            return True
    return False


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_every_foreign_key_column_is_indexed(table) -> None:
    """§7: "Every FK in §6 has an accompanying index."

    Postgres does not index the referencing side of a foreign key, and an
    unindexed FK turns every cascade delete into a sequential scan.
    """
    missing = [
        fk.parent.name
        for fk in table.foreign_keys
        if not _covered_by_leading_index(table, fk.parent.name)
    ]
    assert not missing, (
        f"{table.name}: foreign key column(s) {missing} have no leading index. "
        f"A cascade delete through them will sequential-scan."
    )


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_primary_keys_are_uuid(table) -> None:
    """§5: primary key is always ``id``, type UUID, default uuid_generate_v7()."""
    pk_columns = list(table.primary_key.columns)
    assert pk_columns, f"{table.name} has no primary key"
    for col in pk_columns:
        assert isinstance(col.type, PgUUID), (
            f"{table.name}.{col.name} is {col.type!r}, expected UUID"
        )
    if len(pk_columns) == 1 and pk_columns[0].name == "id":
        default = pk_columns[0].server_default
        assert default is not None and "uuid_generate_v7" in str(default.arg), (
            f"{table.name}.id must default to uuid_generate_v7()"
        )


#: Tables whose creation timestamp is named for what it records rather than
#: generically. The convention is "every mutable table knows when it was made";
#: insisting on the literal name `created_at` would be pedantry.
CREATION_TIMESTAMP_ALIASES = {
    "session_summaries": "generated_at",
}


@pytest.mark.parametrize("table_name", TRIGGERED_TABLES)
def test_mutable_tables_have_updated_at(table_name: str) -> None:
    """§5: a creation and a modification timestamp on every mutable table.

    The matching trigger is asserted against the migrations in
    tests/migrations/test_migration_files.py.
    """
    table = METADATA.tables[table_name]
    created = CREATION_TIMESTAMP_ALIASES.get(table_name, "created_at")

    assert "updated_at" in table.c, f"{table_name} is missing updated_at"
    assert created in table.c, f"{table_name} is missing {created}"
    for col_name in (created, "updated_at"):
        col = table.c[col_name]
        assert not col.nullable, f"{table_name}.{col_name} must be NOT NULL"
        assert col.server_default is not None, (
            f"{table_name}.{col_name} needs DEFAULT NOW()"
        )


@pytest.mark.parametrize("table_name", SOFT_DELETE_TABLES)
def test_soft_delete_tables_have_partial_index(table_name: str) -> None:
    """§14: "Every soft-delete table has a partial index excluding deleted rows."""
    table = METADATA.tables[table_name]
    assert "deleted_at" in table.c, f"{table_name} has no deleted_at"
    partials = [
        ix
        for ix in table.indexes
        if "deleted_at IS NULL" in str(ix.dialect_options["postgresql"].get("where", ""))
    ]
    assert partials, (
        f"{table_name} carries deleted_at but has no partial index excluding "
        f"soft-deleted rows"
    )


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_no_varchar(table) -> None:
    """§5: TEXT for everything, no VARCHAR(n)."""
    offenders = [
        c.name
        for c in table.columns
        if c.type.__class__.__name__ in ("VARCHAR", "String", "CHAR")
        and getattr(c.type, "length", None)
    ]
    assert not offenders, f"{table.name}: use TEXT, not VARCHAR/CHAR for {offenders}"


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_booleans_are_not_nullable(table) -> None:
    """§5: "A nullable boolean is nearly always a modeling mistake."

    ``assessment_attempts.passed`` is the deliberate exception: NULL means not
    yet graded, and a CHECK ties it to score being NULL too.
    """
    allowed = {("assessment_attempts", "passed")}
    offenders = [
        c.name
        for c in table.columns
        if c.type.__class__.__name__ == "Boolean"
        and c.nullable
        and (table.name, c.name) not in allowed
    ]
    assert not offenders, f"{table.name}: nullable boolean(s) {offenders}"


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_foreign_keys_declare_ondelete(table) -> None:
    """§5: "Every foreign key is declared with an explicit ON DELETE policy."""
    missing = [fk.parent.name for fk in table.foreign_keys if not fk.ondelete]
    assert not missing, f"{table.name}: FK column(s) {missing} have no ON DELETE"


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_money_columns_are_numeric(table) -> None:
    """§5: money is NUMERIC and the column name ends in _usd."""
    for col in table.columns:
        if col.name.endswith("_usd"):
            assert col.type.__class__.__name__ == "Numeric", (
                f"{table.name}.{col.name} must be NUMERIC, got {col.type!r}"
            )
        if col.type.__class__.__name__ == "Numeric":
            assert col.name.endswith("_usd"), (
                f"{table.name}.{col.name} is NUMERIC but does not end in _usd"
            )


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_timestamps_are_timezone_aware(table) -> None:
    """§4: all timestamps are TIMESTAMPTZ."""
    naive = [
        c.name
        for c in table.columns
        if c.type.__class__.__name__ == "DateTime" and not c.type.timezone
    ]
    assert not naive, f"{table.name}: {naive} must be TIMESTAMPTZ"


def test_table_count_matches_spec() -> None:
    """Guard against a table being added without a spec update.

    42 = the data layer's 34, plus:

    * ``ingestion_review_queue`` -- ingestion §5 addition 1 (migration 0010).
    * five from evaluation (0011): ``golden_datasets``,
      ``golden_dataset_entries``, ``evaluation_runs``, ``evaluation_results``
      from evaluation §5, and ``signing_keys``, which evaluation §12 builds on
      and no spec ever defined (DIVERGENCES-EVALUATION E1).
    * two from infrastructure (0012): ``retention_actions`` (§12.2) and
      ``retention_holds`` (§12.4).

    None of the eight are in the data layer's §6; all eight join the v1.2
    batch, which infrastructure §18 counts at twenty-two items across all seven
    specs.
    """
    assert len(TABLES) == 42, (
        f"{len(TABLES)} tables defined; 34 from data layer §6, plus "
        f"ingestion_review_queue from ingestion §5, five from evaluation §5 "
        f"and §12, and two from infrastructure §12. "
        f"Update the spec and DIVERGENCES.md together."
    )

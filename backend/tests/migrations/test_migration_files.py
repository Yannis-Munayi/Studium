"""Migration hygiene (spec §13, §14).

The spec's four CI checks are:

1. Every migration has a corresponding downgrade.
2. Every migration runs cleanly against a fresh database.
3. Every migration is reversible (pg_dump --schema-only round-trip).
4. No migration is committed without a CHANGELOG.md entry.

(1) and (4) are static and enforced here. (2) and (3) need a live database and
live in ``test_migration_sequence.py``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from studium.models import TRIGGERED_TABLES

BACKEND = Path(__file__).resolve().parents[2]
VERSIONS = BACKEND / "migrations" / "versions"
CHANGELOG = BACKEND / "CHANGELOG.md"

MIGRATIONS = sorted(VERSIONS.glob("[0-9]*.py"))


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _assignment(tree: ast.Module, name: str) -> object:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not assigned")


def test_migrations_exist() -> None:
    assert MIGRATIONS, "no migrations found"


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_migration_has_a_real_downgrade(path: Path) -> None:
    """A downgrade that is just ``pass`` is not a downgrade."""
    tree = _module(path)
    downgrade = _function(tree, "downgrade")
    assert downgrade is not None, f"{path.name} has no downgrade()"

    def is_noise(node: ast.stmt) -> bool:
        """Docstrings and bare ``pass`` do not count as a downgrade."""
        if isinstance(node, ast.Pass):
            return True
        return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)

    body = [n for n in downgrade.body if not is_noise(n)]
    assert body, (
        f"{path.name}: downgrade() is empty. Spec §13 requires a working "
        f"downgrade for every up-migration."
    )


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_migration_has_an_upgrade(path: Path) -> None:
    assert _function(_module(path), "upgrade") is not None


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_filename_matches_naming_convention(path: Path) -> None:
    """§13: NNNN_verb_object.py"""
    assert re.fullmatch(r"\d{4}_[a-z0-9_]+\.py", path.name), (
        f"{path.name} does not match NNNN_verb_object.py"
    )


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_revision_id_matches_filename(path: Path) -> None:
    revision = _assignment(_module(path), "revision")
    assert path.name.startswith(f"{revision}_"), (
        f"{path.name} declares revision {revision!r}"
    )


def test_revisions_form_a_single_chain() -> None:
    """A branch point in the migration graph is almost always an accident."""
    revisions = {}
    for path in MIGRATIONS:
        tree = _module(path)
        revisions[_assignment(tree, "revision")] = _assignment(tree, "down_revision")

    roots = [rev for rev, down in revisions.items() if down is None]
    assert len(roots) == 1, f"expected exactly one root migration, found {roots}"

    parents = [down for down in revisions.values() if down is not None]
    assert len(parents) == len(set(parents)), (
        f"branch point: two migrations share a parent ({parents})"
    )

    for rev, down in revisions.items():
        if down is not None:
            assert down in revisions, f"{rev} points at missing parent {down!r}"


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_changelog_mentions_the_migration(path: Path) -> None:
    """§13 CI check 4: no migration without a CHANGELOG entry."""
    assert CHANGELOG.exists(), "CHANGELOG.md is missing"
    text = CHANGELOG.read_text(encoding="utf-8")
    assert path.stem in text, (
        f"{path.name} has no CHANGELOG.md entry describing the semantic change"
    )


def test_every_mutable_table_gets_an_updated_at_trigger() -> None:
    """§14: "Every mutable table has an updated_at trigger."

    Checked against the migration text, since the trigger is DDL the ORM
    metadata cannot see. Scans the whole history rather than 0001 alone: the
    session_summaries and retrieval_checks triggers arrive in 0005, and a table
    added in some future revision will bring its own.
    """
    history = "\n".join(p.read_text(encoding="utf-8") for p in MIGRATIONS)
    missing = [
        table for table in TRIGGERED_TABLES if f"trg_{table}_updated_at" not in history
    ]
    assert not missing, f"no updated_at trigger created anywhere for {missing}"


def test_initial_migration_creates_the_uuid_function_correctly() -> None:
    """The spec's uuid_generate_v7() fallback emits 33 hex characters, so every
    insert fails. Guard the corrected assembly against a careless revert."""
    source = (VERSIONS / "0001_initial_schema.py").read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION uuid_generate_v7()" in source
    # 12 (timestamp) + 1 version + 3 rand_a + 2 variant + 14 rand_b = 32.
    assert "substr(v_hex, 1, 3)" in source, "rand_a slice changed"
    assert "substr(v_hex, 7, 14)" in source, "rand_b slice changed -- length will be wrong"
    assert "substr(encode(v_rand, 'hex'), 6)" not in source, (
        "the spec's 15-character tail is back; uuids will be 33 chars and every "
        "INSERT will fail"
    )


def test_grants_migration_revokes_writes_on_append_only_tables() -> None:
    """Every append-only table has its write privileges revoked somewhere.

    Scans the whole history rather than 0003 alone, and the reason is a real
    hazard rather than tidiness. 0003 ends with an ``ALTER DEFAULT PRIVILEGES
    ... GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO studium_app``, so a
    table created by a *later* migration is writable by the application role
    the moment it exists. ``retention_actions`` (0012) is the first such table
    that belongs here, and it carries its own REVOKE.

    Asserting against 0003 alone would therefore have two failure modes and
    both are wrong: it fails for a correctly-revoked later table, and it would
    pass for one that was added to the tuple and revoked nowhere at all.
    """
    from studium.models import APPEND_ONLY_TABLES

    history = {p.name: p.read_text(encoding="utf-8") for p in MIGRATIONS}
    assert "REVOKE UPDATE, DELETE" in history["0003_grants.py"]

    for table in APPEND_ONLY_TABLES:
        revoked = [
            name
            for name, source in history.items()
            if f"REVOKE UPDATE, DELETE ON {table}" in source
            or (name == "0003_grants.py" and table in source)
        ]
        assert revoked, (
            f"append-only table {table!r} has no REVOKE UPDATE, DELETE in any "
            f"migration. 0003's ALTER DEFAULT PRIVILEGES grants both on every "
            f"table created after it, so a later table must revoke for itself."
        )

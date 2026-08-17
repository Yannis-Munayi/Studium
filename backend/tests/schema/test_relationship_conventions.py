"""ORM relationships must defer deletion to the database.

Every foreign key in this schema carries an explicit ``ON DELETE`` policy --
that is where the spec's §6 lifecycle rules actually live. SQLAlchemy does not
read those policies. By default, deleting a parent through the ORM loads its
children and issues ``UPDATE child SET fk = NULL`` *before* the parent's
``DELETE``, which:

* fails outright when the child's FK is ``NOT NULL`` (twelve of twelve here), or
* silently orphans rows the database was told to cascade, when it is nullable.

``passive_deletes=True`` tells the ORM to leave the children alone and let the
``ON DELETE`` clause do the work it was written to do.

This is an offline test: it reads mapper metadata, not a database.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import RelationshipDirection

from studium.models import Base

#: Policies the database enforces on its own. ``NO ACTION`` and ``RESTRICT``
#: are excluded deliberately -- there the ORM nulling a child FK would mask an
#: error the database is supposed to raise, but that is a different bug with a
#: different fix, and no relationship here is backed by one.
DATABASE_OWNED = {"CASCADE", "SET NULL", "SET DEFAULT"}


def _ondelete(column) -> str | None:
    for fk in column.foreign_keys:
        return (fk.constraint.ondelete or "NO ACTION").upper()
    return None


def _parent_side_relationships():
    seen = set()
    for mapper in Base.registry.mappers:
        for rel in mapper.relationships:
            if rel.direction is not RelationshipDirection.ONETOMANY:
                continue
            key = (mapper.class_.__name__, rel.key)
            if key in seen:
                continue
            seen.add(key)
            policies = {
                policy
                for _, remote in rel.local_remote_pairs
                if (policy := _ondelete(remote)) is not None
            }
            yield key, rel, policies


def test_every_parent_side_relationship_is_covered() -> None:
    """Guards the test below: if the walk finds nothing, it proves nothing."""
    assert sum(1 for _ in _parent_side_relationships()) == 12


@pytest.mark.parametrize(
    ("name", "rel", "policies"),
    [pytest.param(k, r, p, id=f"{k[0]}.{k[1]}") for k, r, p in _parent_side_relationships()],
)
def test_database_owned_deletes_are_left_to_the_database(name, rel, policies) -> None:
    if not policies & DATABASE_OWNED:
        pytest.skip(f"{name} is not backed by a database-owned delete policy")
    assert rel.passive_deletes, (
        f"{name[0]}.{name[1]} is backed by ON DELETE {'/'.join(sorted(policies))} "
        "but does not set passive_deletes=True, so the ORM will try to null the "
        "child foreign key before deleting the parent."
    )

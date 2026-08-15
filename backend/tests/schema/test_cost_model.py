"""The cost model's shape (spec v1.1 §6.12), asserted offline.

The widening is the change in v1.1 most likely to be quietly undone -- five
columns where one would "do" reads like redundancy unless you know why. These
tests hold the reason in place: the categories exist so budget enforcement sees
total spend rather than agent-trace spend, and the TTL split exists because the
two cache-write prices differ.
"""

from __future__ import annotations

import pytest

from studium.models import Base

LEDGER = Base.metadata.tables["cost_ledger"]
TRACES = Base.metadata.tables["agent_traces"]

CATEGORIES = (
    "cost_agent_usd",
    "cost_content_usd",
    "cost_ingestion_usd",
    "cost_summary_usd",
    "cost_grading_usd",
)


@pytest.mark.parametrize("column", CATEGORIES)
def test_every_cost_source_has_its_own_column(column: str) -> None:
    assert column in LEDGER.c, (
        f"cost_ledger.{column} is missing. v1.0 summed only agent traces, "
        f"which left content, ingestion, summarisation and grading out of the "
        f"number budget enforcement reads."
    )


def test_total_is_generated_from_the_categories() -> None:
    total = LEDGER.c["cost_usd"]
    assert total.computed is not None, (
        "cost_usd must be generated, not written -- otherwise the total can "
        "drift from the categories it is supposed to summarise"
    )
    expression = str(total.computed.sqltext)
    for column in CATEGORIES:
        assert column in expression, f"{column} is not part of the generated total"


def test_cache_writes_are_split_by_ttl_on_both_sides() -> None:
    """A 5-minute cache write bills at 1.25x base input, a 1-hour write at 2x.
    A single column cannot re-derive cost once both are in use -- and the split
    has to exist on the source as well as the ledger, or the roll-up has
    nothing to read."""
    for table in (LEDGER, TRACES):
        assert "cache_write_5m_tokens" in table.c, f"{table.name} lost the 5m column"
        assert "cache_write_1h_tokens" in table.c, f"{table.name} lost the 1h column"
        assert "cache_write_tokens" not in table.c, (
            f"{table.name} has an ambiguous combined cache-write column"
        )


def test_ledger_owner_is_nullable_for_erasure() -> None:
    """§10 retains the aggregate after a learner is erased, which is only
    possible if the owner column can be nulled."""
    assert LEDGER.c["user_id"].nullable
    fk = next(iter(LEDGER.c["user_id"].foreign_keys))
    assert fk.ondelete == "SET NULL"


def test_owner_uniqueness_treats_nulls_as_equal() -> None:
    """Without NULLS NOT DISTINCT every erased learner's aggregate becomes a
    separate row on the same (day, model) key instead of merging."""
    from sqlalchemy import UniqueConstraint

    constraint = next(
        c
        for c in LEDGER.constraints
        if isinstance(c, UniqueConstraint) and "user_id" in c.columns
    )
    assert constraint.dialect_options["postgresql"]["nulls_not_distinct"] is True


def test_content_artifacts_can_be_attributed_to_a_learner() -> None:
    """§6.12 attributes content cost to "the learner in whose session the
    generation was triggered" -- which needs a join path the v1.1 table does
    not otherwise have."""
    artifacts = Base.metadata.tables["content_artifacts"]
    column = artifacts.c["generated_for_session_id"]
    assert column.nullable, "pre-generation genuinely has no requesting session"
    fk = next(iter(column.foreign_keys))
    assert fk.column.table.name == "learning_sessions"
    assert fk.ondelete == "SET NULL", (
        "an artifact outlives the session that prompted it"
    )

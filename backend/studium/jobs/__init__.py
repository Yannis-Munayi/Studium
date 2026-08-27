"""Scheduled jobs (spec v1.1 §10, §6.12).

All of these connect as ``studium_owner``: they DELETE from append-only tables,
which migration 0003 forbids to the application role.

The erasure procedure is re-exported here for convenience but lives in
``studium.privacy``, which is the path v1.1 §10 names.
"""

from __future__ import annotations

from ..privacy import erase_user, purge_expired_soft_deletes
from .cost_rollup import reconcile, refresh_decay, roll_up_day
from .retention import (
    POLICIES,
    active_policies,
    apply_retention,
    check_dangling_chunk_refs,
    deletion_predicate,
)

__all__ = [
    "POLICIES",
    "active_policies",
    "apply_retention",
    "check_dangling_chunk_refs",
    "deletion_predicate",
    "erase_user",
    "purge_expired_soft_deletes",
    "reconcile",
    "refresh_decay",
    "roll_up_day",
]

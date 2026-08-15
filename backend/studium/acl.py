"""Access control and learner-facing projections (spec §11).

Enforcement lives in application code, not row-level security: RLS needs
session variables set at connection time, which is awkward under PgBouncer
transaction pooling, and the checks are cheap where the current user is
naturally in scope.

Two rules in §11 are *column*-level but written as table-level grants, so they
are defeated by the very rows that implement versioned grading and confusion
tracking. The projections below are the fix (DIVERGENCES.md C5):

* ``rubric_criteria.key_points`` never reaches a learner -- including via
  ``assessment_responses.criterion_snapshot``, which is a copy of it.
* ``journal_entries.hypothesis`` is a Tutor-facing prompt aid. It can be wrong,
  and reading it can be dispiriting, so it is never serialised to the learner.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    LEARNER = "learner"
    REVIEWER = "reviewer"
    ADMIN = "admin"


class AccessDenied(PermissionError):
    """Raised instead of returning a filtered-empty result, so a bug in a
    caller surfaces loudly rather than as silently missing data."""


#: Tables a learner may read *and* write for their own rows.
LEARNER_WRITABLE = frozenset(
    {
        "users",
        "user_profiles",
        "learner_subjects",
        "journal_entries",
        "portfolio_items",
    }
)

#: Tables a learner may read for their own rows but never write. These hold
#: derived or adjudicated state: mastery is BKT-derived, grades are the
#: Evaluator's, review scheduling is FSRS's. §11 lumps these in with the
#: writable set, which would let a learner set their own mastery. (C6)
LEARNER_READABLE = LEARNER_WRITABLE | frozenset(
    {
        "concept_mastery",
        "learning_sessions",
        "session_turns",
        "assessment_attempts",
        "assessment_responses",
        "review_cards",
        "retrieval_checks",
        "session_summaries",
    }
)

#: Never readable by a learner, even for their own rows.
SYSTEM_INTERNAL = frozenset({"agent_traces", "audit_log", "content_review_queue"})


def assert_can_read(role: Role, table: str, *, owns_row: bool) -> None:
    if role is Role.ADMIN:
        return
    if role is Role.REVIEWER and table != "audit_log":
        return
    if table in SYSTEM_INTERNAL:
        raise AccessDenied(f"{role} may not read {table}")
    if table in LEARNER_READABLE and owns_row:
        return
    raise AccessDenied(f"{role} may not read {table}")


def assert_can_write(role: Role, table: str, *, owns_row: bool) -> None:
    if role is Role.ADMIN:
        return
    if table in SYSTEM_INTERNAL:
        raise AccessDenied(f"{role} may not write {table}")
    if role is Role.LEARNER:
        if table in LEARNER_WRITABLE and owns_row:
            return
        raise AccessDenied(f"learner may not write {table}")
    if role is Role.REVIEWER:
        return
    raise AccessDenied(f"{role} may not write {table}")


# --- column-level projections ---------------------------------------------


def project_rubric_criterion(row: Mapping[str, Any], role: Role) -> dict[str, Any]:
    """Strip key_points unless the caller is a reviewer or admin."""
    out = dict(row)
    if role is Role.LEARNER:
        out.pop("key_points", None)
    return out


def project_assessment_response(row: Mapping[str, Any], role: Role) -> dict[str, Any]:
    """Strip the key points out of the criterion snapshot.

    The snapshot is what makes a three-week-old attempt meaningful after the
    rubric is edited, so it is kept -- but for a learner it is reduced to the
    prompt and the criterion identity, never the expected answers.
    """
    out = dict(row)
    if role is not Role.LEARNER:
        return out
    snapshot = out.get("criterion_snapshot")
    if isinstance(snapshot, Mapping):
        out["criterion_snapshot"] = {
            k: v
            for k, v in snapshot.items()
            if k not in ("key_points", "expected_key_points")
        }
    out.pop("missing_points", None)
    return out


def project_journal_entry(row: Mapping[str, Any], role: Role) -> dict[str, Any]:
    """Strip the Tracker's hypothesis from learner-facing output."""
    out = dict(row)
    if role is Role.LEARNER:
        out.pop("hypothesis", None)
    return out


def project_source(row: Mapping[str, Any], role: Role) -> dict[str, Any]:
    """Learners see source metadata (title, authors), not storage details."""
    out = dict(row)
    if role is Role.LEARNER:
        for field in ("storage_path", "content_sha256", "license_notes"):
            out.pop(field, None)
    return out


def can_read_source(
    *,
    role: Role,
    license: str,
    uploaded_by: uuid.UUID | None,
    user_id: uuid.UUID,
) -> bool:
    """§6.3: a 'user_uploaded' source is readable only by its uploader or an
    admin. Enforced here rather than in RLS because the check depends on
    session context the database does not hold."""
    if role is Role.ADMIN:
        return True
    if license != "user_uploaded":
        return True
    return uploaded_by == user_id


# --- prompt-injection boundary (§11) --------------------------------------

USER_CONTENT_OPEN = "<user_content>"
USER_CONTENT_CLOSE = "</user_content>"


def wrap_user_content(text: str) -> str:
    """Demarcate learner- or upload-authored text before it enters a prompt.

    The data layer is what stores this content, so the boundary is applied at
    every read into an agent context. Any literal delimiter in the source text
    is neutralised so it cannot close the block early.
    """
    safe = text.replace(USER_CONTENT_OPEN, "&lt;user_content&gt;").replace(
        USER_CONTENT_CLOSE, "&lt;/user_content&gt;"
    )
    return f"{USER_CONTENT_OPEN}\n{safe}\n{USER_CONTENT_CLOSE}"

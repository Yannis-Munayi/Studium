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


#: Tables a reviewer may read but never write (evaluation §14.1's "The reviewer
#: role does not").
#:
#: * ``signing_keys`` -- "Change signing keys". A reviewer who can rotate the
#:   issuer key can issue credentials under a key of their choosing, which
#:   makes the signature attest to nothing. Rotation is subsystem 7's, run by
#:   an operator with secret-store access.
#: * ``portfolio_items`` -- "issue portfolio items directly (portfolio items
#:   only issue via the summative assessment flow)". A hand-written credential
#:   is a credential for work nobody did, and it would verify.
#: * ``concept_mastery`` / ``mastery_events`` -- "Modify learner mastery
#:   estimates directly". Mastery is BKT-derived from evidence; setting it
#:   by hand unlinks the number from the thing it claims to measure, and
#:   §12.1 lets a subject-level credential turn on mastery thresholds.
REVIEWER_READ_ONLY = frozenset(
    {
        "signing_keys",
        "portfolio_items",
        "concept_mastery",
        "mastery_events",
    }
)


def assert_can_read(role: Role, table: str, *, owns_row: bool) -> None:
    if role is Role.ADMIN:
        return
    if role is Role.REVIEWER:
        # Evaluation §14.1: a reviewer may "read every table in the schema",
        # audit_log included. The data layer excluded it, which was defensible
        # when the role was undefined and is not now: §14.2 hands the reviewer
        # direct database access on purpose ("locking them out of raw access
        # would prevent them from responding to unforeseen situations"), so
        # denying one table through the ACL while handing over psql is theatre.
        # Subsystem 6 owns the role definition; this follows it.
        # See DIVERGENCES-EVALUATION (E11).
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
        if table in REVIEWER_READ_ONLY:
            raise AccessDenied(
                f"reviewer may not write {table} (evaluation §14.1). "
                f"Portfolio items issue only via the summative assessment "
                f"flow, mastery is derived from evidence, and signing keys "
                f"are the infrastructure spec's."
            )
        return
    raise AccessDenied(f"{role} may not write {table}")


def assert_can_delete(role: Role, table: str, *, owns_row: bool) -> None:
    """Evaluation §14.1: the reviewer role does not "delete rows from any table".

    Deletion is a third verb, not a special case of writing, and the ACL had no
    notion of it -- so ``assert_can_write`` returning for a reviewer was
    implicitly granting deletes on every table in the schema. §14.1 is explicit
    that data retention is "per data layer §10, not per-reviewer discretion":
    the retention and erasure jobs run as ``studium_owner``, on a schedule,
    against a written policy. A reviewer who can delete can quietly undo that
    policy for one row and leave no trace but a gap.

    Admins may delete. Learners may not -- their erasure path is §10's
    right-to-erasure, which de-identifies rather than deleting and is a
    different operation with different guarantees.
    """
    if role is Role.ADMIN:
        return
    raise AccessDenied(
        f"{role} may not delete from {table}. Data retention is data layer "
        f"§10's, not per-reviewer discretion (evaluation §14.1); a learner's "
        f"erasure runs through the right-to-erasure job, which de-identifies "
        f"rather than deletes."
    )


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

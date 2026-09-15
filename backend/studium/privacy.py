"""Right to erasure, PIPEDA-aligned (spec v1.1 §10).

The v1.0 procedure was cascade-unsafe: it deleted ``learner_subjects`` early,
which cascades to the very rows later steps said to retain. v1.1 inverts the
order -- anonymise first, then delete -- and that ordering is the whole content
of this module. Each step below is load-bearing in sequence; reordering any two
of them reintroduces the original defect.

Runs as ``studium_owner``: it writes to ``audit_log``, which migration 0003
makes append-only for the application role.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from studium.models.identity import ANONYMIZED_LEARNER_ID, RESERVED_USER_IDS

#: Days the soft-deleted account is kept before the row itself is removed.
#: The window exists so an accidental deletion can be undone, which is also
#: why the email is not scrambled: recovery needs the identity intact.
DISPUTE_WINDOW_DAYS = 30


class ErasureRefused(ValueError):
    """The request names an account that must not be erased."""


def erase_user(session: Session, user_id: uuid.UUID, *, reason: str = "") -> None:
    """Execute a right-to-erasure request, in the order v1.1 specifies.

    1. Mark the account closed and record the request.
    2. Anonymise the aggregates §10 retains -- *before* their parents go.
    3. Detach the credentials that outlive the learner (amendment v1.2.1 §3.1).
    4. Purge learner-owned data.
    5. (Later, by ``purge_expired_soft_deletes``) hard-delete the account.

    Commits on success. The caller gets an exception and an untouched database
    on failure, because every step runs in one transaction: a half-erased user
    is a worse outcome than a failed request.

    Refuses the two reserved accounts. Neither is a person, so neither has a
    right to erasure to exercise -- and erasing the anonymised-learner account
    would cascade away every credential every erased learner ever earned, which
    is the exact loss step 3 exists to prevent, achieved in one command.
    """
    if user_id in RESERVED_USER_IDS:
        raise ErasureRefused(
            f"{user_id} is a reserved system account, not a learner. Erasing "
            f"it would cascade to rows that belong to the deployment rather "
            f"than to any person -- for the anonymised-learner account, to "
            f"every credential retained under amendment v1.2.1 §3.1."
        )

    params = {"user_id": user_id}

    # --- 1. Mark closed ---------------------------------------------------
    #
    # The email is deliberately left intact: the dispute window exists so an
    # accidental deletion can be reversed, and that needs the identity. The
    # address is still free for immediate re-registration because the unique
    # index on users.email is partial -- once deleted_at is set, the row drops
    # out of it (§6.1, and DIVERGENCES.md V6).
    session.execute(
        text("UPDATE users SET deleted_at = NOW() WHERE id = :user_id"), params
    )
    session.execute(
        text(
            """
            INSERT INTO audit_log (actor_user_id, action, target_type, target_id, reason)
            VALUES (:user_id, 'erase_user', 'users', :user_id, :reason)
            """
        ),
        {"user_id": user_id, "reason": reason or "right to erasure request"},
    )

    # --- 2. Anonymise retained aggregates ---------------------------------
    #
    # Order within this step matters and the spec says so explicitly: the
    # response redaction finds its rows *through* assessment_attempts.user_id,
    # so it has to run before that column is nulled. The alternative the spec
    # offers -- keying off "attempts anonymised in the last hour" -- would
    # sweep in another learner's already-erased rows on a busy day.
    session.execute(
        text(
            """
            UPDATE assessment_responses ar
               SET learner_response = '[redacted]',
                   feedback = NULL,
                   missing_points = '[]'::jsonb
              FROM assessment_attempts aa
             WHERE aa.id = ar.attempt_id
               AND aa.user_id = :user_id
            """
        ),
        params,
    )
    session.execute(
        text(
            """
            UPDATE assessment_attempts
               SET user_id = NULL,
                   learner_subject_id = NULL,
                   overall_feedback = NULL
             WHERE user_id = :user_id
            """
        ),
        params,
    )
    # §6.12 reserves user_id IS NULL for post-erasure aggregates, and
    # uq_cost_ledger_day is NULLS NOT DISTINCT -- so (NULL, day, model) is one
    # shared row per day and model, not one per erased learner. A plain
    # UPDATE ... SET user_id = NULL therefore works for the first learner
    # erased on a given day and raises a unique violation for the second,
    # aborting the erasure. Fold the totals into the shared row instead.
    # cost_usd is generated and must not be written.
    session.execute(
        text(
            """
            INSERT INTO cost_ledger AS cl (
                user_id, day, model,
                tokens_in, tokens_out, cache_read_tokens,
                cache_write_5m_tokens, cache_write_1h_tokens,
                cost_agent_usd, cost_content_usd, cost_ingestion_usd,
                cost_summary_usd, cost_grading_usd, session_count
            )
            SELECT NULL::uuid, day, model,
                   tokens_in, tokens_out, cache_read_tokens,
                   cache_write_5m_tokens, cache_write_1h_tokens,
                   cost_agent_usd, cost_content_usd, cost_ingestion_usd,
                   cost_summary_usd, cost_grading_usd, session_count
              FROM cost_ledger
             WHERE user_id = :user_id
            ON CONFLICT (user_id, day, model) DO UPDATE
               SET tokens_in             = cl.tokens_in + EXCLUDED.tokens_in,
                   tokens_out            = cl.tokens_out + EXCLUDED.tokens_out,
                   cache_read_tokens     = cl.cache_read_tokens
                                         + EXCLUDED.cache_read_tokens,
                   cache_write_5m_tokens = cl.cache_write_5m_tokens
                                         + EXCLUDED.cache_write_5m_tokens,
                   cache_write_1h_tokens = cl.cache_write_1h_tokens
                                         + EXCLUDED.cache_write_1h_tokens,
                   cost_agent_usd        = cl.cost_agent_usd
                                         + EXCLUDED.cost_agent_usd,
                   cost_content_usd      = cl.cost_content_usd
                                         + EXCLUDED.cost_content_usd,
                   cost_ingestion_usd    = cl.cost_ingestion_usd
                                         + EXCLUDED.cost_ingestion_usd,
                   cost_summary_usd      = cl.cost_summary_usd
                                         + EXCLUDED.cost_summary_usd,
                   cost_grading_usd      = cl.cost_grading_usd
                                         + EXCLUDED.cost_grading_usd,
                   session_count         = cl.session_count
                                         + EXCLUDED.session_count
            """
        ),
        params,
    )
    session.execute(
        text("DELETE FROM cost_ledger WHERE user_id = :user_id"), params
    )
    # The erasure request itself keeps its actor for accountability; it is
    # anonymised later, when the audit log's own seven-year window expires.
    session.execute(
        text(
            """
            UPDATE audit_log
               SET actor_user_id = NULL
             WHERE actor_user_id = :user_id
               AND NOT (action = 'erase_user' AND target_id = :user_id)
            """
        ),
        params,
    )

    # --- 3. Detach the credentials (amendment v1.2.1 §3.1) ----------------
    #
    # Before step 4, and that ordering is as load-bearing as step 2's. Deleting
    # the enrollment cascades to portfolio_items through the composite
    # enrollment key; a credential still attached to it at that moment is gone,
    # and gone permanently, with the public verify endpoint no longer resolving
    # an id an external verifier may be holding.
    #
    # Nulling learner_subject_id is what breaks that cascade: the composite key
    # is MATCH SIMPLE, so a row with a NULL in it needs no referent. Pointing
    # user_id at the reserved account is what survives step 5, where the users
    # row itself goes and its ON DELETE CASCADE would otherwise take the
    # credential with it.
    #
    # What is deliberately *not* preserved: the surrounding hash chain, which
    # is the learner's own work and is exactly what §10 removes. The retained
    # credential's manifest therefore names a prev_sha256 that no longer
    # resolves. §12.4 does not read it -- the credential payload carries its
    # own signature -- so verification is unaffected.
    detached = session.execute(
        text(
            """
            UPDATE portfolio_items
               SET user_id = :anonymized_id,
                   learner_subject_id = NULL
             WHERE user_id = :user_id
               AND is_credential
            """
        ),
        {"user_id": user_id, "anonymized_id": ANONYMIZED_LEARNER_ID},
    ).rowcount
    if detached:
        # Recorded where the erasure itself is recorded. "The learner asked to
        # be forgotten and three signed credentials still exist" is a question
        # someone will eventually ask, and the answer needs to have been
        # written down at the time rather than reconstructed from row counts.
        session.execute(
            text(
                """
                INSERT INTO audit_log
                    (actor_user_id, action, target_type, target_id, reason)
                VALUES (NULL, 'retain_credentials', 'portfolio_items', :user_id, :reason)
                """
            ),
            {
                "user_id": user_id,
                "reason": (
                    f"{detached} credential(s) reassigned to the anonymised "
                    f"learner account and detached from the enrollment "
                    f"(amendment v1.2.1 §3.1: credentials must stay externally "
                    f"verifiable after erasure)"
                ),
            },
        )

    # --- 4. Purge learner-owned data --------------------------------------
    #
    # Deleting the enrollment cascades to concept_mastery, learning_sessions
    # (and through them session_turns, agent_traces, session_summaries,
    # retrieval_checks), journal_entries and their events, review_cards and
    # their events, and every portfolio_item step 3 did not detach.
    session.execute(
        text("DELETE FROM learner_subjects WHERE user_id = :user_id"), params
    )
    session.execute(text("DELETE FROM user_profiles WHERE user_id = :user_id"), params)
    # Immediately, not on the grace window: an open session token outliving an
    # erasure request is a live credential for a closed account.
    session.execute(text("DELETE FROM auth_sessions WHERE user_id = :user_id"), params)

    session.commit()


#: ``retention_actions.metadata->>'policy_name'`` for the rows this module
#: writes. The table's own columns carry no policy name -- infrastructure
#: §12.2's DDL does not have one and 0012 reproduces it verbatim -- so the
#: nightly worker and this both key off metadata. ``ops retention log`` and the
#: §12.2 queries read it.
ERASURE_PURGE_POLICY = "erasure_purge"


def purge_expired_soft_deletes(session: Session) -> int:
    """Hard-delete accounts closed longer than the dispute window (§10 step 4).

    The anonymised rows in ``assessment_attempts``, ``cost_ledger`` and
    ``audit_log`` survive this: their owner columns are already NULL, so the
    cascade has nothing to follow.

    **Writes a ``retention_actions`` row per account purged** (amendment v1.2.1
    §3.2). Every ordinary retention policy already writes one; the one deletion
    carrying a statutory deadline left no trace at all, so "did the erasure
    request from user X on date Y complete by Y+30" was the single retention
    question the schema could not answer. Every other disposal was provably
    logged and this one was provably *requested* and unaccountably
    completed-or-not.

    One row per account rather than one per pass, because the question is asked
    about a person and a date. A pass that purges three accounts writes three
    rows; a pass that purges none writes none -- unlike the nightly worker's
    per-policy rows, where a zero is the evidence that the worker ran. Here the
    worker's own ``erasure_purge`` stage result is that evidence, and a zero row
    would assert a disposal that did not happen.

    **In the same transaction as the delete.** An audit row committed
    separately can be lost while the delete stands, which produces exactly the
    unaccounted disposal §3.2 exists to rule out. The nightly worker's
    ``_record`` deliberately swallows its own failures for the opposite reason
    -- there, losing one line of the trail beats losing ten committed deletes
    and the nine policies still to run. The trade goes the other way when the
    deletion is the one with a regulator attached.
    """
    started = time.perf_counter()

    # Selected before the delete, because after it there is nothing left to
    # describe. FOR UPDATE so a concurrent pass -- two schedulers, or an
    # operator running `ops nightly` beside the timer -- cannot both claim the
    # same account and write two disposal rows for one disposal.
    due = session.execute(
        text(
            f"""
            SELECT id, deleted_at
              FROM users
             WHERE deleted_at IS NOT NULL
               AND deleted_at < NOW() - INTERVAL '{DISPUTE_WINDOW_DAYS} days'
             ORDER BY deleted_at
               FOR UPDATE
            """
        )
    ).all()
    if not due:
        return 0

    # Resolved before the users row goes: the FK is ON DELETE SET NULL, so
    # after the delete the erase_user row is still there but no longer says
    # whose it was, and the link an auditor would follow is gone.
    audit_refs = {
        row.target_id: row.id
        for row in session.execute(
            text(
                """
                SELECT id, target_id
                  FROM audit_log
                 WHERE action = 'erase_user'
                   AND target_type = 'users'
                   AND target_id = ANY(:ids)
                 ORDER BY created_at
                """
            ),
            {"ids": [r.id for r in due]},
        ).all()
    }

    result = session.execute(
        text("DELETE FROM users WHERE id = ANY(:ids)"),
        {"ids": [r.id for r in due]},
    )

    # One measurement for the pass, recorded on each row. A single set-based
    # DELETE has no per-account share to apportion, and inventing one would
    # make the column read as something it is not.
    duration_ms = int((time.perf_counter() - started) * 1000)

    for row in due:
        session.execute(
            text(
                """
                INSERT INTO retention_actions
                    (table_name, rows_deleted, duration_ms, metadata)
                VALUES ('users', 1, :duration, CAST(:metadata AS jsonb))
                """
            ),
            {
                "duration": duration_ms,
                "metadata": json.dumps(
                    {
                        "policy_name": ERASURE_PURGE_POLICY,
                        "accounts_in_pass": len(due),
                        # The identifier is the thing being disposed of, so it
                        # cannot be stored. The hash lets an auditor holding a
                        # candidate id confirm *this* row is the one, without
                        # the table becoming a second list of erased learners.
                        # The plain id does survive in audit_log's erase_user
                        # row, which §10 keeps deliberately as the
                        # accountability trail; `audit_log_ref` is the link.
                        "user_id_sha256": hashlib.sha256(
                            str(row.id).encode("utf-8")
                        ).hexdigest(),
                        "audit_log_ref": str(audit_refs[row.id])
                        if row.id in audit_refs
                        else None,
                        "dispute_window_days": DISPUTE_WINDOW_DAYS,
                        "deleted_at_original": row.deleted_at.isoformat(),
                    }
                )
            },
        )

    session.commit()
    return int(result.rowcount or 0)

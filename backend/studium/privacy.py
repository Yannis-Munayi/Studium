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

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

#: Days the soft-deleted account is kept before the row itself is removed.
#: The window exists so an accidental deletion can be undone, which is also
#: why the email is not scrambled: recovery needs the identity intact.
DISPUTE_WINDOW_DAYS = 30


def erase_user(session: Session, user_id: uuid.UUID, *, reason: str = "") -> None:
    """Execute a right-to-erasure request, in the order v1.1 specifies.

    1. Mark the account closed and record the request.
    2. Anonymise the aggregates §10 retains -- *before* their parents go.
    3. Purge learner-owned data.
    4. (Later, by ``purge_expired_soft_deletes``) hard-delete the account.

    Commits on success. The caller gets an exception and an untouched database
    on failure, because every step runs in one transaction: a half-erased user
    is a worse outcome than a failed request.
    """
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

    # --- 3. Purge learner-owned data --------------------------------------
    #
    # Deleting the enrollment cascades to concept_mastery, learning_sessions
    # (and through them session_turns, agent_traces, session_summaries,
    # retrieval_checks), journal_entries and their events, review_cards and
    # their events, and portfolio_items.
    session.execute(
        text("DELETE FROM learner_subjects WHERE user_id = :user_id"), params
    )
    session.execute(text("DELETE FROM user_profiles WHERE user_id = :user_id"), params)
    # Immediately, not on the grace window: an open session token outliving an
    # erasure request is a live credential for a closed account.
    session.execute(text("DELETE FROM auth_sessions WHERE user_id = :user_id"), params)

    session.commit()


def purge_expired_soft_deletes(session: Session) -> int:
    """Hard-delete accounts closed longer than the dispute window (§10 step 4).

    The anonymised rows in ``assessment_attempts``, ``cost_ledger`` and
    ``audit_log`` survive this: their owner columns are already NULL, so the
    cascade has nothing to follow.
    """
    result = session.execute(
        text(
            f"""
            DELETE FROM users
             WHERE deleted_at IS NOT NULL
               AND deleted_at < NOW() - INTERVAL '{DISPUTE_WINDOW_DAYS} days'
            """
        )
    )
    session.commit()
    return int(result.rowcount or 0)

"""Signing key lifecycle (infrastructure §11).

Evaluation §12.3 built the cryptography and the table; this is the operational
half §11 asks for -- generation, rotation, publication and the compromise
response. The signing and verification primitives stay in
``studium.eval.credentials`` and are called from here rather than reimplemented.

**The private half never touches this process's disk or the database.**
``mint`` prints a seed for the operator to place in Fly secrets and a password
manager (§9.1's two independent copies) and forgets it. ``publish`` reads the
seed from the environment, derives the public half, and writes only that.
There is deliberately no ``studium ops export-signing-key``: a command that
prints a live private key is one that ends up in a terminal scrollback, a
screen share, or a support ticket.

**Rotation is a swap inside one transaction** (§11.2 step 4: "Flip the new row
to ``active = TRUE``; flip the old row to ``active = FALSE`` in the same
transaction"). This schema spells "active" as ``retired_at IS NULL`` and
enforces one current key per issuer with a partial unique index, so the swap is
not merely tidy -- the intermediate state where both are current cannot be
committed, and a rotation that failed halfway leaves the incumbent signing.

**The ordering §11.2 gives is the one thing worth reading twice.** Step 3
updates the Fly secret *before* step 4 flips the rows. Inverted, there is a
window where the database says the new key is current and the process is still
signing with the old one, and every credential issued in it names a key that
did not sign it. :func:`rotation_plan` prints the steps in order for exactly
that reason -- the procedure is the deliverable, not the function.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.eval.credentials import (
    DEFAULT_ISSUER,
    SIGNING_KEY_ENV,
    SIGNING_KEY_ID_ENV,
    SigningIdentity,
    generate_seed,
    load_identity,
    register_public_key,
)

__all__ = [
    "KeyRecord",
    "mark_compromised",
    "mint",
    "publish",
    "published",
    "rotation_plan",
]

#: §6.3 and §11.2. Printed by `keys list` so "is this key overdue" is
#: answerable without opening a calendar.
ROTATION_INTERVAL = dt.timedelta(days=365)

#: §11.2 step 6: "Old private key: retained in encrypted backup for 90 days (in
#: case of dispute), then destroyed."
PRIVATE_KEY_GRACE = dt.timedelta(days=90)


@dataclass(frozen=True, slots=True)
class KeyRecord:
    key_id: str
    algorithm: str
    public_key: str
    issuer: str
    activated_at: dt.datetime
    retired_at: dt.datetime | None
    compromised_at: dt.datetime | None

    @property
    def current(self) -> bool:
        return self.retired_at is None

    @property
    def age(self) -> dt.timedelta:
        return dt.datetime.now(dt.UTC) - self.activated_at

    @property
    def overdue(self) -> bool:
        return self.current and self.age > ROTATION_INTERVAL

    def render(self) -> str:
        marks = []
        if self.current:
            marks.append("CURRENT")
        if self.overdue:
            marks.append(f"OVERDUE by {(self.age - ROTATION_INTERVAL).days}d")
        if self.compromised_at:
            marks.append(f"COMPROMISED {self.compromised_at.date()}")
        if self.retired_at:
            marks.append(f"retired {self.retired_at.date()}")
        return (
            f"  {self.key_id:24} {self.algorithm:9} "
            f"activated {self.activated_at.date()}  {' '.join(marks)}"
        )


def mint() -> str:
    """§11.1: generate a keypair and print the private half once.

    Returns the base64 seed. The caller prints it; nothing writes it.
    """
    return generate_seed()


def publish(session: Session, identity: SigningIdentity | None = None) -> uuid.UUID:
    """Publish the configured key's public half, retiring the incumbent.

    §11.2 steps 2 and 4 collapsed into one statement pair, which
    ``credentials.register_public_key`` already does correctly: the incumbent
    is retired and the successor inserted in the same transaction, because the
    partial unique index will not hold both.
    """
    return register_public_key(session, identity or load_identity())


def published(session: Session, *, issuer: str = DEFAULT_ISSUER) -> list[KeyRecord]:
    rows = session.execute(
        sql(
            """
            SELECT key_id, algorithm, public_key, issuer,
                   activated_at, retired_at, compromised_at
              FROM signing_keys
             WHERE issuer = :issuer
             ORDER BY activated_at DESC
            """
        ),
        {"issuer": issuer},
    ).all()
    return [KeyRecord(**dict(r._mapping)) for r in rows]


def mark_compromised(
    session: Session,
    key_id: str,
    *,
    earliest_possible: dt.datetime,
    reason: str,
    actor: uuid.UUID | None = None,
) -> bool:
    """§11.4 step 2: stamp ``compromised_at`` and write the audit row.

    ``earliest_possible`` rather than "now": §11.4 step 3 publishes the window
    "between the earliest possible compromise and rotation", and stamping the
    moment of discovery makes that window too narrow by exactly the time it
    took to notice — which is the interval an attacker was using it.

    Does **not** rotate. §11.4 step 1 rotates first, and coupling the two here
    would make the marking depend on a new keypair being ready. The CLI runs
    them in the spec's order and says so; this function does one thing so the
    marking can happen the moment suspicion arises.

    Idempotent in the direction that matters: re-running with an *earlier*
    estimate widens the window; a later one is refused, because narrowing a
    published scrutiny window retroactively tells verifiers that credentials
    they were warned about are fine.
    """
    row = session.execute(
        sql(
            "SELECT id, compromised_at, activated_at FROM signing_keys "
            " WHERE key_id = :key_id"
        ),
        {"key_id": key_id},
    ).one_or_none()
    if row is None:
        raise LookupError(f"no signing key {key_id!r}")
    if earliest_possible < row.activated_at:
        raise ValueError(
            f"{earliest_possible.isoformat()} is before the key was activated "
            f"({row.activated_at.isoformat()}); the CHECK would reject it"
        )
    if row.compromised_at is not None and earliest_possible >= row.compromised_at:
        return False

    session.execute(
        sql(
            "UPDATE signing_keys SET compromised_at = :at WHERE key_id = :key_id"
        ),
        {"at": earliest_possible, "key_id": key_id},
    )
    session.execute(
        sql(
            """
            INSERT INTO audit_log
                (actor_user_id, action, target_type, target_id, reason, after)
            VALUES
                (:actor, 'signing_key_compromised', 'signing_keys', :target,
                 :reason, CAST(:after AS jsonb))
            """
        ),
        {
            "actor": actor,
            "target": row.id,
            "reason": reason,
            "after": f'{{"key_id": "{key_id}", '
            f'"earliest_possible_compromise": "{earliest_possible.isoformat()}"}}',
        },
    )
    return True


def credentials_at_risk(
    session: Session, key_id: str
) -> list[dict[str, object]]:
    """§11.4 step 3's scope: what the compromised key signed inside the window.

    "Portfolio items signed with the compromised key between the earliest
    possible compromise and rotation should be treated with additional
    scrutiny." This is that list — the thing the operator has to publish a
    notice about, so it had better be derivable rather than estimated.

    Bounded by ``compromised_at`` at the start and by ``retired_at`` at the end
    (or now, if the key has not been rotated yet, which means the window is
    still open and the first thing to do is close it).
    """
    rows = session.execute(
        sql(
            """
            SELECT p.id, p.kind::text AS kind, p.created_at, p.user_id
              FROM portfolio_items p
              JOIN signing_keys k ON k.key_id = :key_id
             WHERE p.signature ->> 'key_id' = :key_id
               AND k.compromised_at IS NOT NULL
               AND p.created_at >= k.compromised_at
               AND p.created_at <= COALESCE(k.retired_at, NOW())
             ORDER BY p.created_at
            """
        ),
        {"key_id": key_id},
    ).all()
    return [dict(r._mapping) for r in rows]


def rotation_plan(*, compromised: bool = False) -> str:
    """§11.2's procedure, printed. Ordering is the content.

    A function returning text rather than a doc page because the operator runs
    it at the moment they are about to do the thing, and a runbook in another
    tab is a runbook that gets skimmed. ``docs/ops/signing-keys.md`` carries the
    same steps for the case where there is no shell.
    """
    urgency = (
        "IMMEDIATELY (§11.4 step 1: 'bumped to hours, not scheduled')"
        if compromised
        else "on the annual calendar (§11.2)"
    )
    steps = [
        f"Signing key rotation -- {urgency}",
        "",
        "  1. Generate a new keypair:",
        "         studium ops mint-signing-key",
        "     Store the seed in Fly secrets AND in the password manager.",
        "     §9.1: two independent copies. A lost signing key makes every",
        "     credential it signed unverifiable, and nothing in the running",
        "     system can recover it.",
        "",
        "  2. Set the secret, which redeploys with the new key:",
        f"         flyctl secrets set {SIGNING_KEY_ENV}=<seed> \\",
        f"                            {SIGNING_KEY_ID_ENV}=<new key id>",
        "",
        "     THIS STEP COMES BEFORE STEP 3, and the order is load-bearing.",
        "     Publishing first opens a window where the database names the new",
        "     key as current while the process is still signing with the old",
        "     one; every credential issued in it cites a key that did not sign",
        "     it, and only a verifier finds out.",
        "",
        "  3. Publish the public half and retire the incumbent (one transaction):",
        "         studium ops publish-signing-key",
        "",
        "  4. Verify:",
        "         studium ops smoke-test        # signs and verifies a probe",
        "         studium ops keys list",
        "",
        "  5. Old private key: keep the encrypted backup for 90 days (§11.2",
        "     step 6, in case of dispute), then destroy it. Diary it -- nothing",
        "     in this system will remind you.",
        "",
        "  6. Old public key: leave it published forever (§11.2 step 7). Every",
        "     credential it signed still verifies against it, and a verifier",
        "     that cannot find it cannot tell rotated from forged.",
    ]
    if compromised:
        steps += [
            "",
            "  7. Mark the compromise, with the EARLIEST time it could have",
            "     started -- not when you noticed (§11.4 steps 2 and 3):",
            "         studium ops compromise-signing-key <old key id> \\",
            "             --earliest 2026-08-01T00:00:00Z --reason '...'",
            "",
            "  8. Publish the notice. `compromise-signing-key` prints the",
            "     credentials issued inside the window; those are what the",
            "     notice has to name.",
            "",
            "  9. Investigate scope: what accessed the key, when, from where.",
            "     Sentry logs plus `flyctl logs` are the primary sources (§11.4",
            "     step 4). Rotate every other secret the same person or process",
            "     could reach (§6.4's 48-hour rule).",
        ]
    return "\n".join(steps)

"""Portfolio signing, issuance and verification (evaluation §12).

Ed25519 over a canonical JSON serialisation. A verifier with the published
public key can check a credential without contacting Studium (§12.4), which is
the whole value of the scheme: a claim nobody has to trust the issuer's uptime
to check.

**Canonicalisation is the load-bearing part, not the cryptography.** A
signature is over bytes. If the same payload can serialise two ways, the
signature verifies against one of them and not the other, and a credential that
was valid on Tuesday fails on Wednesday because a dict iterated differently.
:func:`canonicalize` is deliberately strict -- sorted keys, no whitespace, no
``default=`` fallback, no NaN -- and §17 asks for a Tier 1 test that the same
payload serialises byte-identically across invocations.

**No private key material reaches the database.** ``signing_keys`` holds the
public half; the private half is read from the environment
(``STUDIUM_SIGNING_KEY``) and never written anywhere. Subsystem 7 owns where
that environment variable comes from.

**Two signing paths, on purpose.**

*Credentials* (``assessment_pass``, ``subject_completion``) must be signed.
Signing is what they are for, so no key means no credential:
:class:`SigningKeyUnavailable` propagates and §16's row applies -- the learner
is told it will be issued once the problem is fixed, and a background job
retries.

*Work items* (the six ``portfolio_item_kind`` values that predate this
subsystem) are hash-chained first and signed second. The chain is their
tamper-evidence; the signature is additional. Failing a learner's whole turn
because a development box has no key configured would be the wrong trade, and
it is the trade the runtime is making today -- see :func:`build_manifest` and
DIVERGENCES-EVALUATION (E9). An unsigned work item records *that it is
unsigned* rather than pretending otherwise.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from studium.models.portfolio import CREDENTIAL_KINDS as _MODEL_CREDENTIAL_KINDS

#: §12.2. The issuer name that goes into every credential and is checked on
#: verification.
DEFAULT_ISSUER = "studium.app"

#: Where the private seed is read from. 32 bytes, base64. Subsystem 7 owns how
#: it gets there; nothing in this package writes it.
SIGNING_KEY_ENV = "STUDIUM_SIGNING_KEY"
SIGNING_KEY_ID_ENV = "STUDIUM_SIGNING_KEY_ID"

#: §12.1's two credential kinds. Separated from work-item kinds throughout: a
#: credential is issued only by the summative flow (§14.1) and is the only
#: thing the public verify endpoint will serve.
#: Re-exported from the model rather than restated. Migration 0013 puts a CHECK
#: constraint on ``portfolio_items.is_credential`` written against that same
#: tuple, so a second definition here would be a second answer to a question
#: the schema now enforces one answer to.
CREDENTIAL_KINDS = frozenset(_MODEL_CREDENTIAL_KINDS)


class SigningKeyUnavailable(RuntimeError):
    """No usable signing key. §16's "Portfolio item: signing key unavailable".

    Learner-facing copy lives at :data:`UNAVAILABLE_MESSAGE`; this carries the
    operator-facing detail.
    """


class CredentialError(ValueError):
    """A credential could not be built from the inputs given."""


#: §16: the learner sees this, not the exception.
UNAVAILABLE_MESSAGE = (
    "We couldn't issue your credential right now; it will be issued "
    "automatically once the issue is resolved."
)


# --- canonicalisation ------------------------------------------------------


def canonicalize(payload: Mapping[str, Any]) -> bytes:
    """The exact bytes that get signed (§12.3).

    Rules, all of which matter for byte-stability across processes and
    Python versions:

    * keys sorted, so dict insertion order cannot change the bytes;
    * no whitespace, so a pretty-printer cannot;
    * ``ensure_ascii=False`` and explicit UTF-8, so a subject title with an
      accent signs the same way everywhere -- the alternative escapes it to
      ``\\uXXXX`` and makes the bytes depend on the encoder's mood;
    * ``allow_nan=False``, because ``NaN`` and ``Infinity`` are not JSON and
      round-trip through other languages' parsers as errors or as null;
    * **no ``default=`` fallback.** ``studium.llm.prompts.stable_json`` passes
      ``default=str`` and is right to for a cache key, where a near-miss costs
      a cache miss. Here it would let a ``Decimal`` score sign as
      ``"0.87"`` and a ``float`` score as ``0.87`` -- two different signatures
      for the same credential, discovered by a verifier months later. Callers
      convert before signing; :func:`_coerce` does it explicitly.
    """
    return json.dumps(
        _coerce(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _coerce(value: Any) -> Any:
    """Convert to JSON-native types explicitly, refusing what it cannot.

    Refusing is the point. A silent fallback here is a signature mismatch
    later, and the caller is always in a position to say what it meant.
    """
    from decimal import Decimal

    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Decimal):
        # Through float, not str: §12.2 shows "score": 0.87 as a JSON number,
        # and a verifier re-serialising the item from its own parser will
        # produce a number. Signing it as a string would make every
        # round-tripped credential fail.
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dt.datetime):
        # Always UTC, always 'Z', always second precision -- §12.2's example
        # is "2026-08-23T14:30:00Z". Microseconds would make two credentials
        # issued in the same second serialise differently for no reason a
        # verifier can see.
        return value.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _coerce(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_coerce(v) for v in value]
    raise CredentialError(
        f"cannot canonicalise {type(value).__name__} for signing; convert it at "
        f"the call site so the intended JSON form is explicit"
    )


def content_digest(payload: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical bytes, hex. Feeds ``content_sha256``."""
    return hashlib.sha256(canonicalize(payload)).hexdigest()


# --- keys ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SigningIdentity:
    """A loaded private key and the public identity that goes with it."""

    key_id: str
    #: 32-byte Ed25519 seed.
    seed: bytes
    issuer: str = DEFAULT_ISSUER

    def public_key_b64(self) -> str:
        return base64.b64encode(bytes(self._signing_key().verify_key)).decode("ascii")

    def _signing_key(self) -> Any:
        return _nacl().signing.SigningKey(self.seed)


def _nacl() -> Any:
    try:
        import nacl
        import nacl.signing  # noqa: F401
    except ImportError as exc:  # pragma: no cover -- optional extra
        raise SigningKeyUnavailable(
            "pynacl is not installed; install the 'evaluation' extra "
            "(pip install -e '.[evaluation]'). §12.3 pins Ed25519 via pynacl."
        ) from exc
    return nacl


def load_identity(*, issuer: str = DEFAULT_ISSUER) -> SigningIdentity:
    """Read the current signing identity from the environment.

    Raises :class:`SigningKeyUnavailable` rather than generating a key. A
    generated key would sign credentials nobody can verify, because its public
    half was never published -- which is worse than not issuing, since the
    learner would hold a credential that fails verification and have no way to
    know why.
    """
    raw = os.environ.get(SIGNING_KEY_ENV, "").strip()
    if not raw:
        raise SigningKeyUnavailable(
            f"{SIGNING_KEY_ENV} is not set. Credentials cannot be issued "
            f"without a key whose public half is published (§12.4)."
        )
    try:
        seed = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise SigningKeyUnavailable(f"{SIGNING_KEY_ENV} is not valid base64") from exc
    if len(seed) != 32:
        raise SigningKeyUnavailable(
            f"{SIGNING_KEY_ENV} decodes to {len(seed)} bytes; Ed25519 needs 32"
        )

    key_id = os.environ.get(SIGNING_KEY_ID_ENV, "").strip()
    if not key_id:
        raise SigningKeyUnavailable(
            f"{SIGNING_KEY_ID_ENV} is not set. §12.2's issuer_public_key_id must "
            f"name a published key, and deriving one from the key material "
            f"would change if the key were ever re-encoded."
        )
    return SigningIdentity(key_id=key_id, seed=seed, issuer=issuer)


def generate_seed() -> str:
    """A fresh base64 seed, for ``studium eval keys generate``.

    Printed for an operator to place in secret storage. Never persisted here.
    """
    return base64.b64encode(bytes(_nacl().signing.SigningKey.generate())).decode("ascii")


def register_public_key(
    session: Session, identity: SigningIdentity, *, retire_current: bool = True
) -> uuid.UUID:
    """Publish a key's public half, retiring the incumbent (§12.3 rotation).

    Retirement is a separate statement rather than an upsert because the
    partial unique index permits exactly one un-retired key per issuer: the
    incumbent has to step down before the successor can be inserted, and doing
    it in one transaction is what keeps the gap from being observable.
    """
    if retire_current:
        session.execute(
            sql(
                """
                UPDATE signing_keys SET retired_at = NOW()
                 WHERE issuer = :issuer AND retired_at IS NULL
                   AND key_id <> :key_id
                """
            ),
            {"issuer": identity.issuer, "key_id": identity.key_id},
        )

    return session.execute(
        sql(
            """
            INSERT INTO signing_keys (key_id, algorithm, public_key, issuer)
            VALUES (:key_id, 'ed25519', :public_key, :issuer)
            ON CONFLICT (key_id) DO UPDATE SET retired_at = NULL
            RETURNING id
            """
        ),
        {
            "key_id": identity.key_id,
            "public_key": identity.public_key_b64(),
            "issuer": identity.issuer,
        },
    ).scalar_one()


def published_keys(session: Session, *, issuer: str = DEFAULT_ISSUER) -> list[dict[str, Any]]:
    """Every key, current and retired (§12.3, §12.4).

    Retired keys stay published: historical credentials must keep verifying,
    and a verifier that cannot find the key a two-year-old credential names
    cannot tell "retired" from "forged".
    """
    rows = session.execute(
        sql(
            """
            SELECT key_id, algorithm, public_key, issuer, activated_at, retired_at
              FROM signing_keys WHERE issuer = :issuer
             ORDER BY activated_at
            """
        ),
        {"issuer": issuer},
    ).all()
    return [
        {
            "key_id": r.key_id,
            "algorithm": r.algorithm,
            "public_key": r.public_key,
            "issuer": r.issuer,
            "activated_at": r.activated_at,
            "retired_at": r.retired_at,
            "current": r.retired_at is None,
        }
        for r in rows
    ]


# --- signing and verification ----------------------------------------------


def sign(payload: Mapping[str, Any], identity: SigningIdentity) -> str:
    """Ed25519 over :func:`canonicalize`'s bytes. Base64 signature."""
    signed = identity._signing_key().sign(canonicalize(payload))
    return base64.b64encode(signed.signature).decode("ascii")


def verify(payload: Mapping[str, Any], signature: str, public_key_b64: str) -> bool:
    """Check a signature against a published public key (§12.4).

    Returns False rather than raising for any failure a verifier can hit with
    well-formed input -- a bad signature, a truncated key, a payload that was
    edited. Verification is a question, and "no" is an answer.
    """
    try:
        verify_key = _nacl().signing.VerifyKey(
            base64.b64decode(public_key_b64, validate=True)
        )
        verify_key.verify(canonicalize(payload), base64.b64decode(signature, validate=True))
    except SigningKeyUnavailable:
        raise
    except Exception:
        return False
    return True


def build_manifest(
    *,
    previous_sha256: str,
    content_sha256: str,
    user_id: uuid.UUID,
    session_id: uuid.UUID | None,
    issued_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """The §6.10 manifest: "previous item's SHA-256, this item's SHA-256,
    session id, user id, timestamp".

    Data layer §6.10 specifies this and ``portfolio_items.signature`` is
    ``NOT NULL``, but ``orchestration.effects._record_portfolio_item`` never
    built one -- so every ``record_portfolio_item`` effect raised
    ``NotNullViolation`` at insert, and because ``_apply_all`` rolls the whole
    batch back on a handler exception, the turn lost its mastery evidence and
    its journal update along with the portfolio item. No test covered the path.
    See DIVERGENCES-EVALUATION (E9); this function is the fix, and the effect
    handler now calls it.
    """
    return {
        "prev_sha256": previous_sha256,
        "content_sha256": content_sha256,
        "user_id": str(user_id),
        "session_id": str(session_id) if session_id else None,
        "issued_at": issued_at or dt.datetime.now(dt.UTC),
    }


def envelope(
    manifest: Mapping[str, Any], identity: SigningIdentity | None
) -> dict[str, Any]:
    """Wrap a manifest with its signature, or record that it has none.

    ``identity`` of ``None`` produces an *unsigned* envelope. That is legal for
    work items and never for credentials -- :func:`issue_credential` requires
    an identity and raises without one. An unsigned envelope says so in a field
    a query can find, rather than omitting the signature and looking like a
    signed one whose value happens to be absent.
    """
    signed = dict(_coerce(manifest))
    if identity is None:
        return {
            "alg": None,
            "signed": False,
            "unsigned_reason": f"{SIGNING_KEY_ENV} not configured",
            "manifest": signed,
            "signature": None,
        }
    return {
        "alg": "ed25519",
        "signed": True,
        "key_id": identity.key_id,
        "issuer": identity.issuer,
        "manifest": signed,
        "signature": sign(signed, identity),
    }


# --- credentials (§12.1, §12.2) --------------------------------------------


def build_credential(
    *,
    learner_id: uuid.UUID,
    subject: str,
    kind: str,
    score: float,
    passing_threshold: float,
    criteria_results: list[dict[str, Any]],
    assessment_id: uuid.UUID | None = None,
    issued_at: dt.datetime | None = None,
    key_id: str = "",
    issuer: str = DEFAULT_ISSUER,
) -> dict[str, Any]:
    """§12.2's payload, minus the signature (which is over exactly this).

    Field set and names follow §12.2 literally so a verifier written against
    the spec works against what we emit. ``assessment_id`` is omitted entirely
    rather than set to null for a ``subject_completion``, matching §12.2's
    "// for assessment_pass" annotation -- a null would sign differently from
    an absent key and force every verifier to normalise.
    """
    if kind not in CREDENTIAL_KINDS:
        raise CredentialError(
            f"{kind!r} is not a credential kind; expected one of "
            f"{sorted(CREDENTIAL_KINDS)}"
        )
    if kind == "assessment_pass" and assessment_id is None:
        raise CredentialError("assessment_pass requires an assessment_id (§12.2)")
    if not 0.0 <= score <= 1.0:
        raise CredentialError(f"score {score} is outside 0.0-1.0")
    if score < passing_threshold:
        # §12.1: a credential attests a *pass*. Issuing one below threshold
        # would make the signature attest something false, and the signature is
        # the only thing making the claim tamper-evident.
        raise CredentialError(
            f"score {score} is below the passing threshold {passing_threshold}; "
            f"§11.5 issues no portfolio item for a failed attempt"
        )

    payload: dict[str, Any] = {
        "learner_id": str(learner_id),
        "subject": subject,
        "kind": kind,
        "score": float(score),
        "passing_threshold": float(passing_threshold),
        "criteria_results": criteria_results,
        "issued_at": issued_at or dt.datetime.now(dt.UTC),
        "issuer": issuer,
        "issuer_public_key_id": key_id,
    }
    if assessment_id is not None:
        payload["assessment_id"] = str(assessment_id)
    return payload


def issue_credential(
    session: Session,
    *,
    user_id: uuid.UUID,
    learner_subject_id: uuid.UUID,
    subject_slug: str,
    kind: str,
    score: float,
    passing_threshold: float,
    criteria_results: list[dict[str, Any]],
    assessment_id: uuid.UUID | None = None,
    concept_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    title: str | None = None,
    identity: SigningIdentity | None = None,
) -> uuid.UUID:
    """Write a signed credential into ``portfolio_items`` (§11.5, §12).

    Joins the learner's existing hash chain rather than living in a table of
    its own. That is not thrift: the chain already answers "was this row
    inserted after that one and has either been edited since", and a credential
    benefits from that as much as a proof does. What makes it a credential
    rather than a work item is its ``kind``, which is also what the public
    verify endpoint filters on.

    Raises :class:`SigningKeyUnavailable` when no key is configured. §16 turns
    that into the learner-facing message and a retry job; it is deliberately
    not degraded into an unsigned row.
    """
    identity = identity or load_identity()

    # ``is_credential`` is written as a literal TRUE below rather than derived,
    # and this is what makes that safe: build_credential refuses any kind
    # outside CREDENTIAL_KINDS, so every row this function reaches the INSERT
    # for is one. Migration 0013's CHECK constraint is the backstop if that
    # ever stops being true.
    payload = build_credential(
        learner_id=user_id,
        subject=subject_slug,
        kind=kind,
        score=score,
        passing_threshold=passing_threshold,
        criteria_results=criteria_results,
        assessment_id=assessment_id,
        key_id=identity.key_id,
        issuer=identity.issuer,
    )

    body = canonicalize(payload).decode("utf-8")

    tail = session.execute(
        sql(
            """
            SELECT id, chain_index, content_sha256
              FROM portfolio_items
             WHERE learner_subject_id = :lsid
             ORDER BY chain_index DESC
             LIMIT 1
             FOR UPDATE
            """
        ),
        {"lsid": learner_subject_id},
    ).first()

    next_index = (tail.chain_index + 1) if tail else 0
    prev_hash = tail.content_sha256 if tail else ""
    # The chain digest covers the previous hash as well as this body, which is
    # what makes it a chain rather than a list of independent hashes.
    chain_digest = hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()

    manifest = build_manifest(
        previous_sha256=prev_hash,
        content_sha256=chain_digest,
        user_id=user_id,
        session_id=session_id,
        issued_at=payload["issued_at"],
    )
    signature = envelope(manifest, identity)
    # The credential payload is signed in its own right, so a verifier holding
    # only the item (§12.4 serves exactly that) can check the claim without
    # needing the rest of the learner's chain.
    signature["credential_signature"] = sign(payload, identity)

    return session.execute(
        sql(
            """
            INSERT INTO portfolio_items
                (user_id, learner_subject_id, chain_index, concept_id, session_id,
                 kind, title, body, content_sha256, signature, is_credential)
            VALUES
                (:user_id, :lsid, :chain_index, :concept_id, :session_id,
                 CAST(:kind AS portfolio_item_kind), :title, :body, :digest,
                 CAST(:signature AS jsonb), TRUE)
            RETURNING id
            """
        ),
        {
            "user_id": user_id,
            "lsid": learner_subject_id,
            "chain_index": next_index,
            "concept_id": concept_id,
            "session_id": session_id,
            "kind": kind,
            "title": title or _default_title(kind, subject_slug),
            "body": body,
            "digest": chain_digest,
            # parent_item_id is left NULL: it is the revision link, not the
            # chain link, and a credential revises nothing.
            "signature": json.dumps(_coerce(signature), ensure_ascii=False),
        },
    ).scalar_one()


def _default_title(kind: str, subject_slug: str) -> str:
    if kind == "subject_completion":
        return f"Subject completion: {subject_slug}"
    return f"Assessment passed: {subject_slug}"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """What ``GET /api/portfolio/verify/{item_id}`` reports (§12.4)."""

    found: bool
    valid: bool = False
    #: The §12.2 payload, when the item is a credential. Never populated for a
    #: work item: the public endpoint must not serve a learner's essay.
    item: dict[str, Any] | None = None
    signature: str | None = None
    key_id: str | None = None
    key_retired: bool = False
    #: Infrastructure §11.4. Set when the signing key has been marked
    #: compromised and this item was signed inside the scrutiny window.
    #: Advisory: ``valid`` is unchanged, because the signature still verifies
    #: and the mathematics did not stop working. What changed is whether the
    #: verifier should believe only Studium could have produced it.
    key_compromised_at: dt.datetime | None = None
    reason: str = ""

    @property
    def within_compromise_window(self) -> bool:
        return self.key_compromised_at is not None


def verify_item(session: Session, item_id: uuid.UUID) -> VerificationResult:
    """Verify one credential by id, for the public endpoint (§12.4).

    **Only credentials.** A ``portfolio_items`` row may be a proof, an essay or
    a notebook the learner wrote, and §12.4's endpoint is unauthenticated
    ("Verification does not require an account or authentication"). Serving a
    work item through it would publish a learner's coursework to anyone who
    could guess a UUID. §12 does not say this, because §12 assumes
    ``portfolio_items`` holds only credentials; in this schema it does not. See
    DIVERGENCES-EVALUATION (E1). Unknown and non-credential ids return the
    identical "not found" response, so the endpoint cannot be used to test
    whether an id exists.
    """
    row = session.execute(
        sql(
            """
            SELECT p.id, p.kind::text AS kind, p.body, p.signature, p.created_at
              FROM portfolio_items p
             WHERE p.id = :id
               AND p.kind::text IN ('assessment_pass', 'subject_completion')
            """
        ),
        {"id": item_id},
    ).one_or_none()

    if row is None:
        return VerificationResult(found=False, reason="no such credential")

    envelope_ = row.signature or {}
    signature = envelope_.get("credential_signature")
    key_id = envelope_.get("key_id")
    if not signature or not key_id:
        return VerificationResult(
            found=True, valid=False, reason="credential carries no signature"
        )

    key = session.execute(
        sql(
            "SELECT public_key, retired_at, compromised_at FROM signing_keys "
            " WHERE key_id = :key_id"
        ),
        {"key_id": key_id},
    ).one_or_none()
    if key is None:
        return VerificationResult(
            found=True,
            valid=False,
            key_id=key_id,
            reason=f"issuer key {key_id!r} is not published",
        )

    try:
        payload = json.loads(row.body)
    except ValueError:
        return VerificationResult(
            found=True, valid=False, key_id=key_id, reason="credential body is not JSON"
        )

    valid = verify(payload, signature, key.public_key)

    # Infrastructure §11.4 step 3: items "signed with the compromised key
    # between the earliest possible compromise and rotation should be treated
    # with additional scrutiny". Bounded at both ends -- a credential issued
    # before the compromise began is not in the window, and one issued after
    # rotation was signed by a different key entirely.
    in_window = (
        key.compromised_at is not None
        and row.created_at >= key.compromised_at
        and (key.retired_at is None or row.created_at <= key.retired_at)
    )

    return VerificationResult(
        found=True,
        valid=valid,
        item=payload,
        signature=signature,
        key_id=key_id,
        # A retired key is still a valid signer for what it already signed
        # (§12.3). Reported so a verifier can see the key has rotated, not so
        # they can reject the credential.
        key_retired=key.retired_at is not None,
        key_compromised_at=key.compromised_at if in_window else None,
        reason=(
            "signature does not match the published key"
            if not valid
            else (
                "signature verifies, but the issuer key was compromised and "
                "this credential falls inside the disclosed window; treat it "
                "with additional scrutiny (infrastructure §11.4)"
                if in_window
                else ""
            )
        ),
    )

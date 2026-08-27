"""Portfolio signing and canonicalisation (evaluation §17 Tier 1).

§17 names two:

* "Portfolio item signing: ``sign(payload, key)`` produces a valid Ed25519
  signature that verifies against the corresponding public key."
* "Portfolio item canonicalisation: given the same payload, the canonical JSON
  serialization is byte-identical across invocations."

The second is the one that actually breaks in production. A signature is over
bytes; if a payload can serialise two ways, a credential valid on Tuesday fails
on Wednesday because a dict iterated differently or a Decimal reached the
encoder instead of a float. Every test below that looks pedantic is a
same-payload-different-bytes case.
"""

from __future__ import annotations

import base64
import datetime as dt
from decimal import Decimal

import pytest

from studium.eval import credentials
from studium.eval.credentials import (
    CredentialError,
    SigningIdentity,
    SigningKeyUnavailable,
    build_credential,
    canonicalize,
    envelope,
    sign,
    verify,
)

pytest.importorskip("nacl", reason="pynacl is the 'evaluation' extra (§12.3)")


@pytest.fixture
def identity() -> SigningIdentity:
    return SigningIdentity(
        key_id="studium-test-2026",
        seed=base64.b64decode(credentials.generate_seed()),
    )


PAYLOAD = {
    "learner_id": "8b1f0f4e-0000-7000-8000-000000000001",
    "subject": "lambda-calculus",
    "kind": "assessment_pass",
    "score": 0.87,
    "passing_threshold": 0.70,
    "criteria_results": [{"criterion": "beta", "weight": 2, "grade": 2}],
    "issued_at": dt.datetime(2026, 8, 23, 14, 30, tzinfo=dt.UTC),
    "issuer": "studium.app",
    "issuer_public_key_id": "studium-test-2026",
}


class TestCanonicalisation:
    def test_byte_identical_across_invocations(self) -> None:
        assert canonicalize(PAYLOAD) == canonicalize(PAYLOAD)

    def test_key_order_does_not_change_the_bytes(self) -> None:
        """The failure this prevents: a payload built by a different code path
        signs differently and the credential stops verifying."""
        reordered = dict(reversed(list(PAYLOAD.items())))
        assert canonicalize(PAYLOAD) == canonicalize(reordered)

    def test_no_incidental_whitespace(self) -> None:
        assert b", " not in canonicalize(PAYLOAD)
        assert b": " not in canonicalize(PAYLOAD)

    def test_datetimes_serialise_as_the_spec_shows(self) -> None:
        """§12.2's example is "2026-08-23T14:30:00Z"."""
        assert b'"issued_at":"2026-08-23T14:30:00Z"' in canonicalize(PAYLOAD)

    def test_microseconds_are_dropped(self) -> None:
        """Two credentials issued in the same second must not serialise
        differently for a reason no verifier can see."""
        precise = {**PAYLOAD, "issued_at": PAYLOAD["issued_at"].replace(microsecond=123456)}
        assert canonicalize(precise) == canonicalize(PAYLOAD)

    def test_non_utc_datetimes_are_converted_not_rejected(self) -> None:
        elsewhere = PAYLOAD["issued_at"].astimezone(dt.timezone(dt.timedelta(hours=5)))
        assert canonicalize({**PAYLOAD, "issued_at": elsewhere}) == canonicalize(PAYLOAD)

    def test_decimal_and_float_produce_the_same_bytes(self) -> None:
        """A Decimal score signing as "0.87" and a float as 0.87 is two
        signatures for one credential, discovered months later by a verifier."""
        assert canonicalize({**PAYLOAD, "score": Decimal("0.87")}) == canonicalize(PAYLOAD)

    def test_non_ascii_is_not_escaped(self) -> None:
        """\\uXXXX escaping makes the bytes depend on the encoder rather than
        on the content."""
        blob = canonicalize({**PAYLOAD, "subject": "λ-calculus"})
        assert "λ".encode() in blob

    def test_unserialisable_types_are_refused_not_coerced(self) -> None:
        """stable_json's `default=str` is right for a cache key and wrong here:
        a silent coercion is a signature mismatch later."""
        with pytest.raises(CredentialError, match="cannot canonicalise"):
            canonicalize({**PAYLOAD, "extra": object()})

    def test_nan_is_refused(self) -> None:
        with pytest.raises(ValueError):
            canonicalize({**PAYLOAD, "score": float("nan")})


class TestSigning:
    def test_signature_verifies(self, identity: SigningIdentity) -> None:
        signature = sign(PAYLOAD, identity)
        assert verify(PAYLOAD, signature, identity.public_key_b64())

    def test_a_tampered_payload_does_not_verify(self, identity: SigningIdentity) -> None:
        signature = sign(PAYLOAD, identity)
        assert not verify({**PAYLOAD, "score": 0.99}, signature, identity.public_key_b64())

    def test_a_different_key_does_not_verify(self, identity: SigningIdentity) -> None:
        other = SigningIdentity(
            key_id="other", seed=base64.b64decode(credentials.generate_seed())
        )
        signature = sign(PAYLOAD, identity)
        assert not verify(PAYLOAD, signature, other.public_key_b64())

    def test_verification_answers_no_rather_than_raising(
        self, identity: SigningIdentity
    ) -> None:
        """"Is this genuine" is a question and "no" is an answer. A verifier
        should not have to wrap the call to handle malformed input."""
        assert not verify(PAYLOAD, "not base64 !!", identity.public_key_b64())
        assert not verify(PAYLOAD, sign(PAYLOAD, identity), "not a key")

    def test_signing_is_deterministic(self, identity: SigningIdentity) -> None:
        """Ed25519 is deterministic by construction; asserting it means a
        re-signed credential is byte-identical, so a re-issue is detectable as
        a no-op rather than looking like a new credential."""
        assert sign(PAYLOAD, identity) == sign(PAYLOAD, identity)

    def test_public_key_is_32_bytes(self, identity: SigningIdentity) -> None:
        assert len(base64.b64decode(identity.public_key_b64())) == 32


class TestKeyLoading:
    def test_missing_key_raises_rather_than_generating_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A generated key signs credentials nobody can verify, because its
        public half was never published -- worse than not issuing, since the
        learner holds something that fails verification for no visible reason."""
        monkeypatch.delenv(credentials.SIGNING_KEY_ENV, raising=False)
        with pytest.raises(SigningKeyUnavailable, match="not set"):
            credentials.load_identity()

    def test_a_key_of_the_wrong_length_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(credentials.SIGNING_KEY_ENV, base64.b64encode(b"short").decode())
        monkeypatch.setenv(credentials.SIGNING_KEY_ID_ENV, "k")
        with pytest.raises(SigningKeyUnavailable, match="32"):
            credentials.load_identity()

    def test_a_key_without_an_id_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§12.2's issuer_public_key_id must name a *published* key. Deriving
        one from the material would change if the key were re-encoded."""
        monkeypatch.setenv(credentials.SIGNING_KEY_ENV, credentials.generate_seed())
        monkeypatch.delenv(credentials.SIGNING_KEY_ID_ENV, raising=False)
        with pytest.raises(SigningKeyUnavailable, match="KEY_ID"):
            credentials.load_identity()

    def test_a_round_trip_through_the_environment_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(credentials.SIGNING_KEY_ENV, credentials.generate_seed())
        monkeypatch.setenv(credentials.SIGNING_KEY_ID_ENV, "studium-2026")
        loaded = credentials.load_identity()
        assert verify(PAYLOAD, sign(PAYLOAD, loaded), loaded.public_key_b64())


class TestEnvelope:
    def test_an_unsigned_envelope_says_so(self) -> None:
        """A work item written without a key records *that it is unsigned*
        rather than omitting the signature and looking like a signed one whose
        value happens to be absent."""
        result = envelope({"prev_sha256": "", "content_sha256": "x"}, None)
        assert result["signed"] is False
        assert result["signature"] is None
        assert result["unsigned_reason"]

    def test_a_signed_envelope_carries_the_key_id(
        self, identity: SigningIdentity
    ) -> None:
        result = envelope({"prev_sha256": "", "content_sha256": "x"}, identity)
        assert result["signed"] is True
        assert result["key_id"] == identity.key_id
        assert verify(result["manifest"], result["signature"], identity.public_key_b64())

    def test_the_manifest_is_json_safe(self, identity: SigningIdentity) -> None:
        """It goes into a JSONB column; a raw datetime would fail at insert."""
        import json
        import uuid

        manifest = credentials.build_manifest(
            previous_sha256="",
            content_sha256="abc",
            user_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
        )
        json.dumps(envelope(manifest, identity))


class TestBuildCredential:
    def test_valid_assessment_pass(self) -> None:
        import uuid

        payload = build_credential(
            learner_id=uuid.uuid4(),
            subject="lambda-calculus",
            kind="assessment_pass",
            score=0.87,
            passing_threshold=0.70,
            criteria_results=[],
            assessment_id=uuid.uuid4(),
            key_id="k",
        )
        assert payload["kind"] == "assessment_pass"
        assert "assessment_id" in payload

    def test_subject_completion_omits_assessment_id_rather_than_nulling_it(self) -> None:
        """§12.2 annotates the field "// for assessment_pass". A null signs
        differently from an absent key and forces every verifier to normalise."""
        import uuid

        payload = build_credential(
            learner_id=uuid.uuid4(),
            subject="lambda-calculus",
            kind="subject_completion",
            score=0.9,
            passing_threshold=0.70,
            criteria_results=[],
            key_id="k",
        )
        assert "assessment_id" not in payload

    def test_assessment_pass_requires_an_assessment_id(self) -> None:
        import uuid

        with pytest.raises(CredentialError, match="assessment_id"):
            build_credential(
                learner_id=uuid.uuid4(),
                subject="s",
                kind="assessment_pass",
                score=0.9,
                passing_threshold=0.7,
                criteria_results=[],
                key_id="k",
            )

    def test_a_score_below_the_threshold_cannot_be_credentialed(self) -> None:
        """The signature would make a false claim tamper-evident, not untrue.
        §11.5 issues no portfolio item for a failed attempt."""
        import uuid

        with pytest.raises(CredentialError, match="below the passing threshold"):
            build_credential(
                learner_id=uuid.uuid4(),
                subject="s",
                kind="subject_completion",
                score=0.5,
                passing_threshold=0.7,
                criteria_results=[],
                key_id="k",
            )

    def test_a_work_item_kind_is_not_a_credential(self) -> None:
        import uuid

        with pytest.raises(CredentialError, match="not a credential kind"):
            build_credential(
                learner_id=uuid.uuid4(),
                subject="s",
                kind="prose",
                score=0.9,
                passing_threshold=0.7,
                criteria_results=[],
                key_id="k",
            )

    def test_the_built_payload_signs_and_verifies(self, identity: SigningIdentity) -> None:
        import uuid

        payload = build_credential(
            learner_id=uuid.uuid4(),
            subject="lambda-calculus",
            kind="assessment_pass",
            score=0.87,
            passing_threshold=0.70,
            criteria_results=[{"criterion": "beta", "weight": 2, "grade": 2}],
            assessment_id=uuid.uuid4(),
            key_id=identity.key_id,
        )
        assert verify(payload, sign(payload, identity), identity.public_key_b64())

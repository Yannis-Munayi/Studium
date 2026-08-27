"""License and rights classification (ingestion §11).

There is no classifier in this module, and that is the point.

An earlier build had one. It read the filename, found the substring "turing",
and set ``license = 'public_domain'`` on the strength of it. The file was a
1936 Church paper, so the answer happened to be right, and the reasoning was
worthless -- the same rule would have marked a Turing biography published in
2019 as public domain and put it in front of learners as freely usable. A false
positive on a rights claim is categorically worse than a false negative:
declining to publish something we could have published costs a source, while
publishing something we had no right to is a legal exposure that the system
asserted on its own authority.

So §11.4 refuses three things by name, to stop each being reinvented as an
improvement on the last:

* **Filename.** Refused as a category. No version of that pattern is
  acceptable, however many substrings it checks.
* **PDF metadata.** Many PDFs carry no rights field; those that do are often
  wrong, because the field is set by whatever tool produced the file. Metadata
  is a hint *for the reviewer*, which is why the extractor still records it
  (:func:`studium.ingestion.extract._document_metadata`) and why nothing reads
  it back as a decision.
* **An LLM.** The most tempting, because a model reading a title page usually
  can identify the work. It has the identical failure mode: confident output
  from insufficient input, now with fluent justification attached. If it ever
  becomes a v1.1 feature it produces *suggestions a reviewer confirms*, never a
  classification.

What replaces the classifier is one honest default and a human.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from .provenance import ingestion_writer
from .queue import flag

#: §11.1. Every upload starts here, and this value is a statement about the
#: *uploader*, not about the work: "the person who uploaded this claims they
#: have rights to make it available; the system has not verified that."
#:
#: Not ``public_domain``, which is a rights claim the system is in no position
#: to make. Not ``user_uploaded`` either, which sounds close but says something
#: about how the file arrived rather than what may be done with it -- and it
#: carries an access restriction in the query layer (§6.3) that would hide a
#: perfectly publishable source from everyone but its uploader.
HONEST_DEFAULT = "permission_granted"

#: The ``license_kind`` values a reviewer may choose (data layer §6.0).
LICENSE_KINDS = (
    "public_domain",
    "cc_by",
    "cc_by_sa",
    "cc_by_nc",
    "user_uploaded",
    "permission_granted",
    "fair_use",
)

#: Classifications that let a source be published. ``permission_granted`` is
#: absent deliberately: it is the *unclassified* state, so a source still
#: sitting on the default has not been reviewed, whatever else is true of it.
#: §11.2 -- a source cannot be published until a real classification lands.
PUBLISHABLE_LICENSES = frozenset(
    {"public_domain", "cc_by", "cc_by_sa", "cc_by_nc", "fair_use"}
)


@dataclass(frozen=True, slots=True)
class LicenseState:
    source_id: uuid.UUID
    title: str
    license: str
    license_notes: str | None
    status: str

    @property
    def is_classified(self) -> bool:
        """Whether a reviewer has made a call on this source.

        ``permission_granted`` with a note is a real classification -- someone
        looked and recorded the basis. Without a note it is still the default
        the upload set, and nobody has looked at all. The note is the evidence
        that a human was involved, which is the entire content of §11.
        """
        if self.license in PUBLISHABLE_LICENSES:
            return True
        return self.license == HONEST_DEFAULT and bool((self.license_notes or "").strip())


@ingestion_writer
def classify(
    session: Session,
    *,
    source_id: uuid.UUID,
    license: str,
    note: str,
) -> LicenseState:
    """Record a reviewer's license determination (§11.2 steps 3-4).

    ``note`` is required, not optional. It carries the *basis* -- "Michaelson
    hosts this on his university page; Dover permits author redistribution of
    backlist" -- and without it the row records a conclusion with no reasoning,
    which is indistinguishable from the guess this workflow exists to replace.
    The next reviewer needs to be able to check the work.
    """
    if license not in LICENSE_KINDS:
        raise ValueError(
            f"unknown license {license!r}; expected one of {', '.join(LICENSE_KINDS)}"
        )
    if not note.strip():
        raise ValueError(
            "a classification note is required: it records the basis for the "
            "call, and a conclusion without one is a guess (§11.2)"
        )

    row = session.execute(
        sql(
            """
            UPDATE sources
               SET license = CAST(:license AS license_kind),
                   license_notes = :note
             WHERE id = :source_id
               AND deleted_at IS NULL
            RETURNING id, title, license, license_notes, status
            """
        ),
        {"source_id": source_id, "license": license, "note": note.strip()},
    ).one_or_none()

    if row is None:
        raise LookupError(f"no source {source_id}")

    return LicenseState(
        source_id=row.id,
        title=row.title,
        license=row.license,
        license_notes=row.license_notes,
        status=row.status,
    )


def state(session: Session, source_id: uuid.UUID) -> LicenseState | None:
    row = session.execute(
        sql(
            """
            SELECT id, title, license, license_notes, status
              FROM sources
             WHERE id = :id AND deleted_at IS NULL
            """
        ),
        {"id": source_id},
    ).one_or_none()
    if row is None:
        return None
    return LicenseState(
        source_id=row.id,
        title=row.title,
        license=row.license,
        license_notes=row.license_notes,
        status=row.status,
    )


def unclassified(session: Session, *, subject_id: uuid.UUID | None = None) -> list[LicenseState]:
    """Sources with no reviewer determination (§11.3 ``studium sources list``).

    Also what blocks a publish: §14 refuses to publish a subject while any of
    its sources is still sitting on the untouched default.
    """
    rows = session.execute(
        sql(
            """
            SELECT id, title, license, license_notes, status
              FROM sources
             WHERE deleted_at IS NULL
               AND (:no_subject OR subject_id = :subject_id)
               AND license = CAST(:default_license AS license_kind)
               AND COALESCE(BTRIM(license_notes), '') = ''
             ORDER BY created_at
            """
        ),
        {
            "no_subject": subject_id is None,
            "subject_id": subject_id,
            "default_license": HONEST_DEFAULT,
        },
    ).all()
    return [
        LicenseState(
            source_id=row.id,
            title=row.title,
            license=row.license,
            license_notes=row.license_notes,
            status=row.status,
        )
        for row in rows
    ]


@ingestion_writer
def flag_pending(session: Session, *, source_id: uuid.UUID, title: str) -> uuid.UUID:
    """Queue the ``license_pending`` row every upload creates (§6.2 step 5)."""
    return flag(
        session,
        flag_source="license_pending",
        source_id=source_id,
        reason=(
            f"{title!r} was uploaded at the default license "
            f"({HONEST_DEFAULT}) and needs a reviewer determination before it "
            f"can be published"
        ),
        payload={"title": title, "license": HONEST_DEFAULT},
    )


def flag_conflict(
    session: Session, *, source_id: uuid.UUID, declared: str, reason: str
) -> uuid.UUID:
    """§12.1 ``license_conflict``: the reviewer disputes a declared license."""
    return flag(
        session,
        flag_source="license_conflict",
        source_id=source_id,
        reason=reason,
        payload={"declared": declared},
    )

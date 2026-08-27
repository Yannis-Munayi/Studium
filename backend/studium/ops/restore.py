"""Restore verification (infrastructure §9.2, §9.3).

§3: "Backups exist to be restored, not to sit unused. ... a backup that has
never been restored is a coin flip that could have been avoided by testing."

``studium ops verify-restore`` is the command §9.2 and §9.3 both name and
neither defines. What it has to answer is narrow and specific: *is this
restored database one the backend could serve from?* Not "does it have rows" --
a restore that silently truncated the last hour has rows.

Six checks, in the order they stop being worth running:

1. **Schema version.** ``alembic_version`` matches the head the checkout knows
   about. A restore from before a migration is a database the current code will
   fail against on its first query, and it is the failure most likely to be
   mistaken for a code bug at 03:00.
2. **Extensions.** ``vector``, ``pgcrypto``, ``citext``, ``pg_trgm``. A restore
   into an instance without them fails at the first embedding write, hours
   later, with a message about a type.
3. **Table population.** Every table exists and the ones that cannot legitimately
   be empty are not. Distinguishing "empty because new" from "empty because the
   restore dropped it" is the whole job, so the list of must-not-be-empty
   tables is explicit rather than "any table with rows in production".
4. **Referential integrity across the arrays.** ``concept_sources.chunk_ids``
   carries no foreign key (data layer §6.2), so a partial restore breaks it
   silently. This is the one integrity property Postgres will not re-check for
   us on restore.
5. **The hash chain.** ``portfolio_items`` is a per-learner chain; a restore
   that lost the middle of one produces a chain that still verifies at the tail
   and lies about what came before. Checked by recomputing the links.
6. **Signing keys.** At least one un-retired key per issuer, or credentials
   cannot be issued and the restored system comes up quietly unable to do the
   one thing whose failure a learner notices weeks later.

Read-only throughout. It runs against the scratch instance in §9.3's drill and
against the real one in §9.2's restore, and a verification that could write is
one nobody will run on production.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

#: Extensions migration 0001 installs. `uuid-ossp` is deliberately absent: it
#: has no v7 generator and was dropped in the initial build.
REQUIRED_EXTENSIONS: tuple[str, ...] = ("pgcrypto", "citext", "pg_trgm", "vector")

#: Tables whose emptiness means the restore is wrong rather than the system
#: being new. Kept short on purpose: every entry is a claim that a working
#: deployment always has rows here, and a wrong claim turns the drill into a
#: false alarm nobody investigates twice.
MUST_NOT_BE_EMPTY: tuple[str, ...] = (
    "users",          # the system account is seeded by migration 0008
    "subjects",
    "concepts",
)

#: Functions and procedures the schema depends on and pg_dump can drop on the
#: floor if the restore targets a database with a different search_path.
REQUIRED_ROUTINES: tuple[str, ...] = (
    "uuid_generate_v7",
    "set_updated_at",
    "accumulate_session_cost",
    "refresh_subject_metadata",
)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str
    #: False for checks whose failure is informational rather than blocking --
    #: an empty corpus in a freshly restored scratch instance, say.
    blocking: bool = True

    def render(self) -> str:
        mark = "PASS" if self.passed else ("FAIL" if self.blocking else "warn")
        return f"  [{mark:>4}] {self.name:34} {self.detail}"


@dataclass(slots=True)
class RestoreVerification:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return all(c.passed or not c.blocking for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.blocking]

    def render(self) -> str:
        lines = ["restore verification (§9.2)", ""]
        lines += [c.render() for c in self.checks]
        lines.append("")
        if self.ok:
            lines.append(
                "  RESTORE USABLE. This says the schema, extensions, routines "
                "and the integrity Postgres cannot re-check itself are intact. "
                "It does not say the restore point is the one you wanted -- "
                "check the newest row's timestamp against when the incident "
                "started."
            )
        else:
            lines.append(f"  RESTORE NOT USABLE: {len(self.failures)} blocking failure(s).")
        return "\n".join(lines)


def verify(session: Session, *, expected_head: str | None = None) -> RestoreVerification:
    """Run every check. Never raises for a failed check; raises only if the
    database is unreachable, which the caller will have noticed anyway."""
    checks: list[Check] = [
        _schema_version(session, expected_head),
        _extensions(session),
        _routines(session),
        *_population(session),
        _chunk_id_arrays(session),
        _portfolio_chain(session),
        _signing_keys(session),
        _newest_row(session),
    ]
    return RestoreVerification(checks=checks)


def head_revision() -> str | None:
    """The migration head this checkout knows about, read from the files.

    From the files rather than from Alembic's config so the check works in a
    container with no alembic.ini on the path -- which is exactly the container
    an operator is in during a restore.
    """
    from pathlib import Path

    versions = Path(__file__).resolve().parents[2] / "migrations" / "versions"
    if not versions.is_dir():
        return None
    revisions: dict[str, str | None] = {}
    for path in versions.glob("[0-9]*.py"):
        text = path.read_text(encoding="utf-8")
        rev = down = None
        for line in text.splitlines():
            if line.startswith("revision = "):
                rev = line.split("=", 1)[1].strip().strip("\"'")
            elif line.startswith("down_revision = "):
                raw = line.split("=", 1)[1].strip()
                down = None if raw == "None" else raw.strip("\"'")
        if rev:
            revisions[rev] = down
    parents = {d for d in revisions.values() if d}
    heads = [r for r in revisions if r not in parents]
    return heads[0] if len(heads) == 1 else None


def _schema_version(session: Session, expected: str | None) -> Check:
    expected = expected or head_revision()
    actual = session.execute(
        sql("SELECT version_num FROM alembic_version")
    ).scalar_one_or_none()
    if actual is None:
        return Check("schema version", False, "alembic_version is empty or missing")
    if expected is None:
        return Check(
            "schema version", True, f"at {actual} (no head to compare against)", blocking=False
        )
    return Check(
        "schema version",
        actual == expected,
        f"restored at {actual}, this checkout expects {expected}"
        if actual != expected
        else f"at {actual}",
    )


def _extensions(session: Session) -> Check:
    present = {
        r[0]
        for r in session.execute(sql("SELECT extname FROM pg_extension")).all()
    }
    missing = [e for e in REQUIRED_EXTENSIONS if e not in present]
    return Check(
        "extensions",
        not missing,
        f"missing {missing}" if missing else ", ".join(REQUIRED_EXTENSIONS),
    )


def _routines(session: Session) -> Check:
    present = {
        r[0]
        for r in session.execute(
            sql(
                "SELECT proname FROM pg_proc p "
                "  JOIN pg_namespace n ON n.oid = p.pronamespace "
                " WHERE n.nspname = 'public'"
            )
        ).all()
    }
    missing = [r for r in REQUIRED_ROUTINES if r not in present]
    return Check(
        "functions and procedures",
        not missing,
        f"missing {missing}" if missing else f"{len(REQUIRED_ROUTINES)} present",
    )


def _population(session: Session) -> list[Check]:
    from studium.models import Base

    checks: list[Check] = []
    expected_tables = sorted(Base.metadata.tables)
    present = {
        r[0]
        for r in session.execute(
            sql(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        ).all()
    }
    missing = [t for t in expected_tables if t not in present]
    checks.append(
        Check(
            "tables present",
            not missing,
            f"missing {missing}" if missing else f"{len(expected_tables)} tables",
        )
    )

    for table in MUST_NOT_BE_EMPTY:
        if table not in present:
            continue
        count = int(session.execute(sql(f"SELECT count(*) FROM {table}")).scalar_one())
        checks.append(
            Check(
                f"{table} populated",
                count > 0,
                f"{count} row(s)"
                if count
                else "empty -- a working deployment always has rows here",
            )
        )
    return checks


def _chunk_id_arrays(session: Session) -> Check:
    """Data layer §6.2's array of chunk ids carries no foreign key.

    So a restore that lost ``source_chunks`` rows leaves ``concept_sources``
    pointing at ids that resolve to nothing, and Postgres reports a clean
    restore. Retrieval degrades gracefully rather than failing, which is
    correct behaviour and is exactly why nobody would notice.
    """
    dangling = int(
        session.execute(
            sql(
                """
                SELECT count(*) FROM concept_sources cs
                 WHERE EXISTS (
                     SELECT 1 FROM unnest(cs.chunk_ids) AS cid
                      WHERE NOT EXISTS (
                          SELECT 1 FROM source_chunks sc WHERE sc.id = cid
                      )
                 )
                """
            )
        ).scalar_one()
    )
    return Check(
        "concept_sources.chunk_ids resolve",
        dangling == 0,
        f"{dangling} concept_sources row(s) reference missing chunks"
        if dangling
        else "no dangling chunk references",
    )


def _portfolio_chain(session: Session) -> Check:
    """Recompute each learner's hash chain (data layer §6.10).

    A restore that lost the middle of a chain leaves a tail that verifies
    against its own predecessor and a whole that no longer describes what the
    learner did. The chain is short -- tens of items per learner -- so
    recomputing it in Python is cheaper than the query that would do it in SQL
    and considerably easier to read.
    """
    rows = session.execute(
        sql(
            """
            SELECT learner_subject_id, chain_index, body, content_sha256
              FROM portfolio_items
             ORDER BY learner_subject_id, chain_index
            """
        )
    ).all()

    broken: list[str] = []
    previous_hash: dict[object, str] = {}
    expected_index: dict[object, int] = {}
    for row in rows:
        key = row.learner_subject_id
        want_index = expected_index.get(key, 0)
        if row.chain_index != want_index:
            broken.append(f"{key} jumps {want_index}->{row.chain_index}")
            expected_index[key] = row.chain_index + 1
            previous_hash[key] = row.content_sha256
            continue
        digest = hashlib.sha256(
            (previous_hash.get(key, "") + (row.body or "")).encode("utf-8")
        ).hexdigest()
        if digest != row.content_sha256:
            broken.append(f"{key} index {row.chain_index} digest mismatch")
        previous_hash[key] = row.content_sha256
        expected_index[key] = row.chain_index + 1

    return Check(
        "portfolio hash chains",
        not broken,
        "; ".join(broken[:3]) + (" ..." if len(broken) > 3 else "")
        if broken
        else f"{len(rows)} item(s) across {len(expected_index)} chain(s)",
    )


def _signing_keys(session: Session) -> Check:
    rows = session.execute(
        sql(
            """
            SELECT issuer,
                   count(*) FILTER (WHERE retired_at IS NULL) AS current,
                   count(*) AS total
              FROM signing_keys GROUP BY issuer
            """
        )
    ).all()
    if not rows:
        return Check(
            "signing keys",
            True,
            "none published; credentials cannot be issued until "
            "`studium ops mint-signing-key` runs",
            blocking=False,
        )
    without = [r.issuer for r in rows if r.current == 0]
    return Check(
        "signing keys",
        not without,
        f"issuer(s) {without} have no current key"
        if without
        else f"{sum(r.total for r in rows)} key(s), {len(rows)} issuer(s)",
    )


def _newest_row(session: Session) -> Check:
    """How fresh the restore is. Informational -- only a human knows the target.

    §9.4 puts the RPO at 24 hours, or 5 minutes with point-in-time recovery.
    Whether *this* restore hit its point is a question about the incident, not
    about the database, so this reports and never fails.
    """
    newest = session.execute(
        sql("SELECT max(created_at) FROM session_turns")
    ).scalar()
    if newest is None:
        return Check("restore point", True, "no session turns to date from", blocking=False)
    import datetime as dt

    age = dt.datetime.now(dt.UTC) - newest
    return Check(
        "restore point",
        True,
        f"newest session turn {newest.isoformat()} ({age.total_seconds() / 3600:.1f}h "
        f"ago). §9.4's RPO is 24h, or 5 min with PITR -- compare against when "
        f"the incident started.",
        blocking=False,
    )

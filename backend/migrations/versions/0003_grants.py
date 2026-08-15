"""role grants: enforce append-only tables at the privilege level

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-14

Spec section 6.5: "The append-only property is enforced by revoking UPDATE and
DELETE on this table from the application role in migration 0003_grants."
Section 6.12 says the same for audit_log.

Two roles, because the spec's own lifecycle rules need them (DEVIATIONS A2/C8):

* ``studium_app`` -- what the FastAPI process connects as. Can INSERT into the
  append-only tables and SELECT from them, nothing else.
* ``studium_owner`` -- owns the tables. The retention job, the erasure job and
  the cost roll-up connect as this, because section 10 requires them to DELETE
  from exactly the tables the application may not touch.

Cascading deletes are unaffected by the revoke either way: Postgres runs
referential actions with the privileges of the referencing table's owner.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APP_ROLE = "studium_app"
OWNER_ROLE = "studium_owner"

APPEND_ONLY = (
    "mastery_events",
    "journal_events",
    "review_events",
    "agent_traces",
    "audit_log",
)


def upgrade() -> None:
    # NOLOGIN group roles: the deploy grants them to the actual login users, so
    # credentials stay in Fly secrets rather than in a migration.
    for role in (APP_ROLE, OWNER_ROLE):
        op.execute(
            f"DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"CREATE ROLE {role} NOLOGIN; END IF; END $$;"
        )

    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}, {OWNER_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
        f"TO {APP_ROLE}"
    )
    op.execute(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {OWNER_ROLE}")
    op.execute(
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public "
        f"TO {APP_ROLE}, {OWNER_ROLE}"
    )

    # History is append-only. Only the owner rewrites it, and only to correct
    # genuinely bad data -- which is itself recorded in the audit log.
    for table in APPEND_ONLY:
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM {APP_ROLE}")

    # Future tables inherit the same posture without a follow-up migration.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT ALL ON TABLES TO {OWNER_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE ALL ON TABLES FROM {OWNER_ROLE}"
    )
    for table in APPEND_ONLY:
        op.execute(f"GRANT UPDATE, DELETE ON {table} TO {APP_ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {OWNER_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}, {OWNER_ROLE}")
    # Roles themselves are left in place: they may own objects outside this
    # schema, and DROP ROLE fails if anything still depends on them.

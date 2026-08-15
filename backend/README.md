# Studium — data layer

Subsystem 1 of 7, implementing `spec/Sub System 1 - Data Layer/01-data-layer-v1.1.md`.
Persistence only:
34 tables, the migrations that build them, the deterministic rules the schema's
invariants depend on, and the lifecycle jobs.

Agent behaviour, retrieval ranking, prompt content, and the API surface belong
to later subsystems and are deliberately absent.

The spec was ratified as v1.1 on 15 August 2026, folding in the corrections
from the v1.0 build. Where the implementation still differs — and why — is in
[DIVERGENCES.md](DIVERGENCES.md).

## Layout

```
studium/
  models/          34 tables, one module per spec section
  mastery.py       BKT update, forgetting curve, MASTERY_THRESHOLD
  assessment.py    scoring and compute_pass
  graph.py         acyclicity, prerequisites, unlock_status
  acl.py           roles and the learner-facing column projections
  privacy.py       right to erasure (section 10)
  cost.py          live spend and budget enforcement (section 8)
  queries.py       the frequent access paths from spec section 8
  review/fsrs.py   spaced-repetition scheduling
  jobs/            retention, cost roll-up, decay refresh
  db.py            engine, isolation levels, serialization retry
migrations/        Alembic: 0001-0003 the v1.0 build, 0004-0008 the v1.1 changes
scripts/           seed.py, migrate_from_draft.py
tests/             schema, integrity, queries, migrations, unit, fixtures
```

## Getting started

```sh
make install     # deps
make db-up       # Postgres 16 + pgvector in Docker
make db-reset    # drop, create, migrate, seed
make test        # everything
```

Without Docker, point `STUDIUM_DATABASE_URL` at any Postgres 16 with the
`vector`, `citext`, `pg_trgm` and `pgcrypto` extensions available.

## Tests

The suite is split so most of it runs with no database at all:

```sh
make test-offline   # 377 tests, no Postgres needed
make test-db        # integrity, query shapes, migration round-trip
```

The offline tier reads the SQLAlchemy metadata and the migration files
directly. It is what catches a foreign key without an index, an enum value
renamed without review, a migration with an empty `downgrade()`, or a
migration committed without a CHANGELOG entry — the four things spec section 14
asks CI to enforce, minus the two that genuinely need a live server.

The database tier covers the integrity properties (cascades, `RESTRICT` on
citations, the composite owner keys, cycle detection), the erasure procedure,
and query shapes — including an assertion that no session-start query falls
back to a sequential scan.

## Conventions worth knowing before you edit

- **`updated_at` is a trigger, not the ORM.** A raw SQL write keeps the same
  guarantee. Adding a mutable table means adding it to `TRIGGERED_TABLES` *and*
  installing the trigger in a migration; the tests check both.
- **Append-only means append-only.** `mastery_events`, `journal_events`,
  `review_events`, `agent_traces` and `audit_log` have `UPDATE`/`DELETE`
  revoked from `studium_app`. The retention and erasure jobs connect as
  `studium_owner`.
- **Nothing asks a model whether the learner passed.** Mastery is BKT over the
  evidence log, passing is `score >= threshold`, unlocking is a graph test.
  If you find yourself wanting an LLM call in `studium/`, it belongs in
  subsystem 2.
- **Gating recomputes decay.** Do not gate on the `p_known_decayed` column; it
  is only as fresh as the last job run. Use `graph.unlock_status`.
- **Migration 0001 is a committed artifact.** It was generated from the model
  metadata once and then hand-edited. Do not regenerate it — write a new
  revision instead, or the later `ADD COLUMN`s will collide with it.
- **Cost has five sources, not one.** `cost_ledger` carries a column per
  category and generates the total. If you add a sixth thing that spends money,
  it needs a column, a roll-up statement, and an attribution rule — otherwise
  budget enforcement silently under-reports.

## Carrying the draft forward

```sh
python scripts/migrate_from_draft.py --dry-run
python scripts/migrate_from_draft.py --student demo
```

Reads the draft's own loaders rather than re-parsing its JSON, so a change to
the draft's shape fails loudly here instead of mis-migrating. The subject lands
as `status = 'draft'` with a warnings list to work through — synthesised rubric
prompts and prerequisite edges derived from linear unit order both need a human
pass before publishing.

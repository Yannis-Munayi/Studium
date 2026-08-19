# Studium — backend

Subsystems 1 through 3 of 7:

| # | Subsystem | Spec | Divergences |
|---|---|---|---|
| 1 | Data layer | `spec/Sub System 1 - Data Layer/01-data-layer-v1.1.md` | [DIVERGENCES.md](DIVERGENCES.md) |
| 2 | Agent runtime | `spec/Sub System 2 - Agent Runtime/02-agent-runtime-v1.0.md` | [DIVERGENCES-RUNTIME.md](DIVERGENCES-RUNTIME.md) |
| 3 | Retrieval | `spec/Sub System 3 - Retrieval/03-retrieval-v1.0.md` | [DIVERGENCES-RETRIEVAL.md](DIVERGENCES-RETRIEVAL.md) |

The frontend (subsystem 4), ingestion (5), the evaluation harness (6), and
infrastructure (7) are deliberately absent.

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

  agents/          the seven agents; one module each
  llm/             the Anthropic wrapper, routing, prompts, retries, traces
  orchestration/   state machine, effects, streaming, handoff
  session/         lifecycle, context assembly, budget gate
  api/app.py       SSE turn endpoint, interrupt channel, citation resolution

  retrieval/       subsystem 3
    types.py           the retrieve_passages contract and its thresholds
    chunking.py        the chunking algorithm (pure, deterministic)
    providers.py       embeddings, reranking, backoff, circuit breakers
    search.py          hybrid SQL, RRF fusion, the concept-graph constraint
    service.py         the pipeline, end to end
    citations.py       [Pn] markers in, provenance out
    cache.py           five-minute results, warm starts
    embedding_worker.py  the background embedder

migrations/        Alembic: 0001-0003 v1.0, 0004-0008 v1.1, 0009 retrieval
scripts/           seed.py, migrate_from_draft.py
tests/             schema, integrity, queries, migrations, unit, fixtures,
                   agents, llm, orchestration, session, api, retrieval, online
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
back to a sequential scan. Retrieval's share of it covers HNSW ordering, the
generated tsvector column, the concept-graph filter, and citation resolution.

A third tier spends real money and never runs in CI:

```sh
pip install -e ".[dev,retrieval]"
VOYAGE_API_KEY=... STUDIUM_RUN_PAID_TESTS=1 \
  python -m pytest tests/online/test_retrieval_paid.py -m anthropic
```

Worth re-running on any embedding or reranker change: one of those tests checks
that hand-labelled relevant passages score above the 0.5 floor thin-grounding
detection assumes. If they stop doing so, every well-grounded retrieval gets
flagged and the review queue floods.

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
- **Passage numbering is a contract, not a formatting choice.** Retrieval
  returns passages sorted by `chunk_id`, and citation resolution renumbers
  stored citations the same way. Both sides must agree or every `[Pn]` in every
  stored artifact resolves to the wrong chunk — silently, because the lists are
  the same length. It is also what keeps the Lecturer's cached prefix
  byte-stable, so breaking it costs money before it costs correctness.
- **Retrieval degrades, it does not raise.** `retrieve_passages` returns an
  empty flagged result rather than an exception, on every path. A concept whose
  curation is thin is a normal state of an evolving corpus; the caller's job is
  to say less, not to fail the learner's turn.
- **`voyageai` is optional and must stay optional.** `studium.retrieval` is
  imported by `studium.agents`, so a hard dependency would break the whole
  runtime for anyone without a Voyage key. Without it, chunking uses a
  character-ratio token estimate and the retriever runs on stub embeddings —
  fine for tests, not for a deployment, which is why `default_retriever` warns.

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

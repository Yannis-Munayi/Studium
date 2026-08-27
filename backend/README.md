# Studium — backend

Subsystems 1, 2, 3, 5, 6 and 7 of 7:

| # | Subsystem | Spec | Divergences |
|---|---|---|---|
| 1 | Data layer | `spec/Sub System 1 - Data Layer/01-data-layer-v1.1.md` | [DIVERGENCES.md](DIVERGENCES.md) |
| 2 | Agent runtime | `spec/Sub System 2 - Agent Runtime/02-agent-runtime-v1.0.md` | [DIVERGENCES-RUNTIME.md](DIVERGENCES-RUNTIME.md) |
| 3 | Retrieval | `spec/Sub System 3 - Retrieval/03-retrieval-v1.0.md` | [DIVERGENCES-RETRIEVAL.md](DIVERGENCES-RETRIEVAL.md) |
| 5 | Content authoring and ingestion | `spec/Sub System 5 - Content authoring and ingestion/05-ingestion-v1.0.md` | [DIVERGENCES-INGESTION.md](DIVERGENCES-INGESTION.md) |
| 6 | Evaluation and assessment harness | `spec/Sub System 6 - Assessment and evaluation harness/06-evaluation-v1.0.md` | [DIVERGENCES-EVALUATION.md](DIVERGENCES-EVALUATION.md) |
| 7 | Infrastructure | `Documentation/Planning/Sub System 7 - Infrastructure/07-infrastructure-v1.0.md` | [DIVERGENCES-INFRASTRUCTURE.md](DIVERGENCES-INFRASTRUCTURE.md) |

The frontend is subsystem 4 and lives in [`../studium-web`](../studium-web).

Subsystem 7's operational half — runbooks, the deploy procedure, the restore
procedure, the calendar — is in [`../docs/ops`](../docs/ops). The deployment
itself is `fly.toml` and `Dockerfile` here and in `../studium-web`.

**What later subsystems found in earlier ones.** Each of these is recorded
rather than patched from outside, except the ones the finder had to fix to
function at all.

- The frontend build: `api/app.py`'s stream is a POST and so cannot be consumed
  by `EventSource` as the frontend spec assumes, and a generated artifact's id
  never reaches the client, so no `[Pn]` marker can be resolved
  ([SPEC_DEBT.md](SPEC_DEBT.md) SD5).
- The evaluation build: `record_portfolio_item` never built the `NOT NULL`
  signature manifest, so **every** invocation raised and took its whole effect
  batch — mastery evidence included — down with it; and
  `SUMMATIVE_ASSESSMENT` was a state a session could enter and never leave,
  because no transition named it as a source. Both had passing test suites
  because nothing exercised either path. Fixed, and recorded as
  [DIVERGENCES-EVALUATION.md](DIVERGENCES-EVALUATION.md) E9 and E10.
- The infrastructure build: **three jobs were written to run on a schedule and
  nothing ran them.** `privacy.purge_expired_soft_deletes` is the serious one —
  data layer §10 step 4, the hard delete after the 30-day dispute window — so
  every right-to-erasure request was permanently half-finished, with the
  learner's email still in `users`. `cost_rollup.refresh_decay` and
  `retention.check_dangling_chunk_refs` were the other two; the third had never
  been called by anything at all. All three are stages of
  `studium.ops.nightly.run_nightly` now. See
  [DIVERGENCES-INFRASTRUCTURE.md](DIVERGENCES-INFRASTRUCTURE.md) N9.

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
    warming.py         the rolling pre-warm loop
    embedding_worker.py  the background embedder

  ingestion/       subsystem 5
    extract.py         PDF to text, per-extractor
    normalize.py       ligatures, furniture, hyphenation (pure, versioned)
    pipeline.py        upload -> extract -> normalize -> chunk -> embed
    authoring.py       concept graph, rubrics, concept-sources from YAML
    licensing.py       the determination no classifier is allowed to make
    publish.py         the publish gate
    queue.py           the ingestion review queue
    provenance.py      the §13 invariant, as an enforced registry
    cli.py             studium ingest / publish / sources / browse / queue

  eval/            subsystem 6
    datasets.py        golden dataset authoring format and validation
    checks.py          the deterministic check registry
    fixtures.py        seeded runtime contexts, derived so runs are comparable
    grading.py         deterministic and Evaluator meta-graded
    runner.py          run execution and persistence
    metrics.py         §8's per-agent metrics and blocking thresholds
    retrieval_eval.py  recall@k, precision@k, the reranker A/B, the swap gate
    regression.py      §13.2 tolerance arithmetic, affected-set detection
    sync.py            YAML -> database materialisation
    review.py          the content review surface
    summative.py       the closed-book assessment flow
    credentials.py     Ed25519 signing, canonicalisation, verification
    cli.py             studium eval / review

  ops/             subsystem 7
    secrets.py         the §6 registry; .env.example is generated from it
    alerts.py          §8.1's table as data, with probes for the half we can see
    retention.py       the §12 worker: batching, holds, the audit trail
    nightly.py         every scheduled job, including three that had no caller
    scheduler.py       §12.1's 02:00 UTC loop, guarded by an advisory lock
    cost.py            §13.1's report; §13.2's estimates, labelled as estimates
    restore.py         §9.2's verify-restore, eight read-only checks
    smoke.py           §10.3's post-deploy verification
    keys.py            §11's signing-key lifecycle and compromise response
    cli.py             studium ops

  storage/         §4.5's byte interface: local volume today, R2 when §14.3 fires
  observability/   §7: correlation ids, Sentry scrubbing, OpenTelemetry spans

content/           the authored tree the two CLIs read
  evaluation/        golden datasets (evaluation §4)
  subjects/          concept graphs, rubrics, assessment definitions

fly.toml           §4.2's topology. Committed, and carries no secrets.
Dockerfile         two-stage, non-root, one uvicorn worker (§4.2)
.env.example       GENERATED from studium/ops/secrets.py; a Tier 1 test diffs it

migrations/        Alembic: 0001-0003 v1.0, 0004-0008 v1.1, 0009 retrieval,
                   0010 ingestion, 0011 evaluation, 0012 retention operations
scripts/           seed.py, migrate_from_draft.py,
                   seed_volume.py + measure_queries.py    (learner-activity scale)
                   measure_retrieval.py                   (corpus scale, §9 budgets)
                   chunk_diagnostics.py                   (chunker vs real PDFs)
tests/             schema, integrity, queries, migrations, unit, fixtures,
                   agents, llm, orchestration, session, api, retrieval, ops,
                   online
```

## Running it

```sh
studium ops secrets check --environment local   # what is missing here, and why
studium ops smoke-test                          # after a deploy (§10.3)
studium ops check-alerts                        # §8's database-backed conditions
studium ops cost-report                         # §13.1
studium ops nightly --dry-run                   # §12.1's pass, changing nothing
studium ops verify-restore                      # §9.2, read-only
```

Exit codes are the interface: non-zero when something is firing, when a restore
is unusable, when a blocking smoke check failed, or when a required secret is
missing. The runbooks in [`../docs/ops`](../docs/ops) say when to run each.

Open questions the specs cannot yet answer, and defects that belong to a spec
rather than to code, are in [SPEC_DEBT.md](SPEC_DEBT.md). Each entry names the
trigger that closes it.

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
make test-offline   # 1705 tests, no Postgres needed
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

Evaluation has its own paid tier, and its own reason to run:

```sh
pip install -e ".[dev,evaluation]"
ANTHROPIC_API_KEY=... STUDIUM_RUN_PAID_TESTS=1 \
  python -m pytest tests/online/test_evaluation_paid.py -m anthropic
```

Tier 1 proves the deterministic checks are correct against strings *we* wrote.
Only this tier can tell you whether they are correct against strings a *model*
wrote — and the failure it exists to catch is a check that scores 1.0 on every
real output and therefore measures nothing. A dataset every real run passes
perfectly is not a passing dataset; it is a dataset with no discriminating
power, and the two look identical from Tier 1.

The regression gate itself is a separate thing from the test suite, and is what
§13.1 asks you to run before merging a prompt change:

```sh
make eval-validate                       # parse and check the datasets
python -m studium.eval.cli eval affected --changed $(git diff --name-only main)
make eval-gate d="lecturer_formal_stance_grounding"
```

`eval affected` exits non-zero when a changed prompt resolves to no dataset,
rather than reporting "nothing affected" — that silence is how a change ships
unevaluated. `eval gate` exits non-zero on a §13.2 tolerance breach or a §8
threshold violation. Neither is wired into CI yet; see
[SPEC_DEBT.md](SPEC_DEBT.md) SD9 for the budget question that blocks it.

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
- **Query vectors travel as binary, never as text.** `studium.db` registers
  pgvector's adapter on every connection and `search._as_vector` wraps the
  embedding so psycopg selects it. Sending the same vector as a text literal
  costs 43 ms of Postgres-side parsing against 2.8 ms of actual execution — a
  14x latency difference that no correctness test can see. There is a Tier 2
  guard (`TestVectorTransport`); do not route around it.
- **Cache warming is opt-in.** `STUDIUM_WARM_CACHE=1` starts the §14 loop. It
  is off by default because it makes a billable retrieval call every five
  minutes forever, which is right in a deployment and a surprise anywhere else.
  `/health` reports whether it is running.

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

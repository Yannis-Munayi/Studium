# Scaling

Infrastructure §14. None of these triggers is close at MVP scale; they are
written down so the response is planned rather than improvised.

## §14.1 Backend instances

**Trigger:** sustained CPU > 70% over 15 minutes at peak, OR concurrent
sessions consistently > 20.

**Action:** auto-scaling, min=1 max=3.

```toml
# backend/fly.toml
[[services]]
  auto_stop_machines = true
  auto_start_machines = true
  min_machines_running = 1
```

**Three things break when this fires, and two of them are silent.**

1. **Cold-start latency hurts the interruption UX.** §4.2 disabled
   auto-scaling for exactly this reason; watch it after enabling.
2. **The Orchestrator registry is process-local.** `studium/api/app.py` keeps
   one Orchestrator per session in a dict, by design (agent runtime §7). With
   two instances, a turn that lands on the other one rebuilds state from the
   `session_turns` tail — which mostly works and loses a mid-stream interrupt.
   The docstring there calls this "the first thing to move to Redis when it is
   not [single-node]".
3. **The cache warmer runs per instance.** Retrieval §14's warmer bills a
   reranker on a timer; three instances is three timers warming three separate
   in-process caches. §14.5 is the fix (Redis), and until then this is a cost
   multiplier nobody would attribute to a scaling decision.

The **retention scheduler** is already guarded — a Postgres advisory lock means
the second and third instances skip the pass rather than running it
concurrently. That guard exists because this trigger was foreseeable, not
because it fired.

## §14.2 Postgres

**Trigger:** p95 query latency past data layer §14's targets (sub-10ms warm
cache), OR storage > 80% of the volume.

**Actions, in order:** read replicas; tune; upgrade the instance.

Before any of them, check the retention worker is running. A database filling
because nothing has been deleted in three weeks is not a database that needs a
bigger volume.

```sh
studium ops check-alerts --all | grep -E "postgres_storage|retention_worker"
studium ops retention log --limit 20
```

**And read SPEC_DEBT SD4 before believing the latency numbers.** Retrieval §9
budgets vector search at 50ms and attributes the cost to HNSW; measurement put
HNSW at 6% of it and parameter marshalling at the rest. Keyword search, the
half §9 treats as cheap, is the binding constraint and its p95 reached 79% of
budget at classroom tier. If p95 is what fired this trigger, measure which half
before adding replicas — a read replica does not help a query that is slow for
the reason SD4 documents.

## §14.3 Object storage

**Trigger:** source volume > 30GB. See
[`storage-migration.md`](storage-migration.md).

## §14.4 Multi-region

**Trigger:** substantial users outside eastern North America, OR uptime
requirements too strict for one region.

Real engineering work and a v2 concern (§2, §14.4). It brings Postgres
replication, replica lag as a correctness question, and Fly global load
balancing. Do not start it because latency *looks* high — §4.1's numbers put
Toronto→eastern Canada at 15–40ms and Toronto→Anthropic at 40–80ms, and neither
dominates a per-turn budget measured in seconds.

## §14.5 Redis

**Trigger:** multi-instance backend, which is §14.1 firing.

**Action:** Fly managed Redis; move the retrieval result cache and the intent
classifier's caches off-process. Retrieval §14 says it is a small code change,
and the Orchestrator registry (point 2 above) should move at the same time —
that one is not small, and doing it separately means shipping the multi-node
deployment twice.

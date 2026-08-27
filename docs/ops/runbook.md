# Incident runbook

Infrastructure §15. The index for "something is wrong and I want to know where
to look".

## First three commands, always

```sh
curl -s https://studium.app/health | jq        # frontend, and the backend through it
flyctl status -a studium-backend
studium ops check-alerts --all
```

`/health` distinguishes the layers on purpose. The frontend returns 503 only
when it cannot reach the backend; the backend returns 503 only when it cannot
reach Postgres. Everything else — a model provider down, the retention worker
stopped, Langfuse unconfigured — comes back 200 with the detail in the body,
because taking instances out of Fly's routing pool for a degraded dependency
converts a degraded product into an absent one.

## §15's table, with the commands

| Failure | Detection | What to do |
|---|---|---|
| **Fly region outage** | Uptime alert; [status.flyio.net](https://status.flyio.net) confirms | Wait. Nothing is possible from our side. Tell learners if it passes 2 hours. |
| **Postgres primary failure** | Backend `/health` 503; Sentry connection errors | Fly auto-failover if configured, else [`postgres-restore.md`](postgres-restore.md). Check `retention_actions` before restoring — "rows are missing" and "the worker did its job" look identical. |
| **Anthropic outage** | Sentry `APIStatusError` 5xx; learners see the degradation copy | Wait. Retrieval falls back to keyword-only (retrieval §16); agents surface graceful errors. Nothing to deploy. |
| **Voyage outage** | Retrieval falls back to keyword-only | Wait. Quality is degraded, not broken. `studium ops smoke-test` reports it non-blocking. |
| **Frontend down** | Uptime alert | `flyctl releases list` — if a deploy is recent, `flyctl releases rollback`. Else Sentry and `flyctl logs`. |
| **Backend down** | Uptime alert (frontend cannot reach backend) | As above, against `studium-backend`. |
| **Signing key compromise** | A human tells you; nothing detects it | [`signing-keys.md`](signing-keys.md) → Compromise. Rotate first, mark second, publish third. |
| **Data corruption** | Sentry inconsistency errors; a number looks wrong | `studium ops verify-restore` against the *live* database first — it runs read-only and checks the integrity Postgres cannot re-check itself. Then [`postgres-restore.md`](postgres-restore.md). |
| **Budget cap exceeded systematically** | Cost alert | `studium ops cost-report` — the anomaly section names the user, the by-line section names the cost line. Raise caps, tighten limits, or find the loop. |
| **Unhandled prompt injection** | Sentry sees odd agent behaviour; the content review queue fills with anomalies | `studium review list --severity 3`. Analyse the inputs, add filtering at the boundary, consider prompt hardening. |
| **Retention worker stopped** | `retention_worker_stale` alert; `/health` shows `retention.running: false` | Check `STUDIUM_RETENTION_WORKER=1` and read `last_result` in `/health`. `studium ops nightly` catches up. |
| **Content cost booking to the system account** | `unattributed_content` alert | The content pipeline stopped setting `generated_for_session_id`. Until fixed, per-user cost figures understate real spend. |

## Deciding whether to roll back

Rollback is 30 seconds and is almost always safe for a code-only release. The
question that matters is whether the release ran a migration:

```sh
flyctl releases list -a studium-backend
git log --oneline <previous>..<current> -- backend/migrations/versions/
```

Additive migration → roll the code back, leave the schema. Non-additive → roll
forward with a fix. `CHANGELOG.md` says which each migration is, in the entry's
last paragraph.

## What to write down afterwards

Even for something small. The next person's first question is "has this
happened before", and the only place that can be answered is
[`drill-log.md`](drill-log.md) — which takes real restores as well as drills,
because what actually took the time is the thing worth knowing.

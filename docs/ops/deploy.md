# Deploying

Infrastructure §10.3, §10.4.

**Manual promotion.** Merges to `main` do not auto-deploy. §10.3: "This is
safer than auto-deploy for a project where the operator is human, single, and
prefers a moment of intention before shipping."

## Shipping a backend change

```sh
# 1. CI is green on the branch you are shipping.
gh run list --branch yannis --limit 5

# 2. Deploy. Migrations run as a release command first; a migration failure
#    aborts the deploy and the previous version keeps serving (§10.3).
cd backend && flyctl deploy

# 3. Verify.
python -m studium.ops.cli ops smoke-test --base-url https://studium.app
```

`smoke-test` checks the database, the schema head, the deployed `/health`, the
provider keys and a signing round-trip. It exits non-zero on a blocking
failure, which is what §10.3's "Failure alerts" reads.

The **session round trip** §10.3 also asks for is behind `--full`, because it
is a real Opus call on every invocation. Run it after a change that touches the
agent path; skip it for a config change. See DIVERGENCES-INFRASTRUCTURE (N6).

## Shipping a frontend change

```sh
cd studium-web && npm run check && flyctl deploy
curl -s https://studium.app/health | jq
```

## Rolling back

```sh
flyctl releases list -a studium-backend
flyctl releases rollback -a studium-backend      # ~30 seconds
```

**Code changes roll back. Schema changes roll forward.** §10.4: "Alembic
downgrade is available but rarely the right choice — data changes typically
can't be safely reversed."

The trap is a release that contained both. Rolling back the code leaves the new
schema in place, which is usually fine — the migrations in this repo are
additive — but is not guaranteed, and the two migrations that were not additive
(0004, and 0011's enum rebuild on downgrade) would lose data. Before rolling
back a release that ran a migration, check what it did:

```sh
git log --oneline -- backend/migrations/versions/
# then read the CHANGELOG entry, which describes the semantic change
```

If the migration was additive, roll the code back and leave the schema. If it
was not, roll forward with a fix.

## Preview deployments

§5.4. Not enabled by default; turn it on for a specific PR when it warrants
one:

```sh
flyctl deploy --strategy blue-green --app studium-backend-pr123
```

A preview app needs its own secrets and its own database, which is why it is
not the default: §5.2 declined a permanent staging environment for the same
reason, and a preview is a staging environment with a shorter life.

## What a deploy does not do

- **It does not seed content.** `studium ingest` and `studium publish` are
  separate, deliberate operations.
- **It does not rotate anything.** See [`secrets.md`](secrets.md).
- **It does not run the evaluation suite.** That costs $5–15 a run (evaluation
  §7.4) and is wired to prompt changes, not to deploys — see
  `.github/workflows/prompt-regression.yml`.

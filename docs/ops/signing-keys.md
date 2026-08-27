# Signing keys

Infrastructure §11. Evaluation §12.3 for what the keys are used for.

Portfolio credentials are signed with Ed25519. A verifier with the published
public key can check a credential without contacting Studium at all — which is
the whole value of the scheme, and the reason everything below is careful.

**`studium ops rotate-signing-key` prints this procedure at the moment you need
it.** This document is the copy for when there is no shell.

## The one irreversible loss

§9.1: the signing key is "the one thing whose loss cannot be recovered from the
running system — a lost signing key means every portfolio item signed with it
becomes unverifiable."

Two independent encrypted copies: Fly secrets, and the password manager. The
database holds the **public** half only, by design — a signing key in a
database is a signing key in every backup, every replica and every `pg_dump`
anyone ever took.

## First key (§11.1)

```sh
studium ops mint-signing-key            # prints a seed, stores nothing
flyctl secrets set STUDIUM_SIGNING_KEY=<seed> \
                   STUDIUM_SIGNING_KEY_ID=studium-2026 -a studium-backend
studium ops publish-signing-key
studium ops smoke-test                  # signs and verifies a probe
```

## Rotation (§11.2) — annually

Same four commands, in the same order. **The order is the content:**

> Step 2 (set the Fly secret) comes before step 3 (publish). Inverted, there is
> a window where the database names the new key as current while the process is
> still signing with the old one, and every credential issued in that window
> cites a key that did not sign it. Only a verifier finds out, months later.

Then:

- **Old private key**: keep the encrypted backup for 90 days (§11.2 step 6, in
  case of dispute), then destroy it. Nothing in the system will remind you —
  diary it.
- **Old public key**: leave it published forever (§11.2 step 7). Every
  credential it signed still verifies against it, and a verifier that cannot
  find it cannot tell "rotated" from "forged".

`studium ops keys list` flags a current key past the annual interval as
OVERDUE.

## Compromise (§11.4)

Suspicion is enough. Do not wait for confirmation.

```sh
studium ops rotate-signing-key --compromised    # prints the urgent variant
```

1. **Rotate immediately** — the four commands above, "bumped to hours, not
   scheduled".
2. **Mark it, with the earliest time it *could* have started:**

   ```sh
   studium ops compromise-signing-key studium-2026 \
     --earliest 2026-08-01T00:00:00Z \
     --reason "seed found in a shared terminal scrollback"
   ```

   Not when you noticed. §11.4 step 3 publishes the window "between the
   earliest possible compromise and rotation", and stamping the moment of
   discovery makes that window too narrow by exactly the interval the attacker
   was using the key. The command refuses a naive timestamp and refuses to move
   an existing mark later, because narrowing a published window retroactively
   tells verifiers that credentials they were warned about are fine.

3. **Publish the notice.** The command prints every credential signed inside
   the window; those are what the notice has to name. `GET /api/signing-keys`
   reports `compromised_at` per key, and `GET /api/portfolio/verify/{id}`
   returns `key_compromised_at` on any credential inside the window, so a
   verifier who never reads the notice still learns about it.

   The credentials **still verify**. The mathematics did not change, and
   invalidating every credential the key ever issued would punish the learners
   rather than the attacker. "Additional scrutiny" is the claim, not
   "invalid".

4. **Investigate scope.** What accessed the key, when, from where — Sentry and
   `flyctl logs` are the primary sources (§11.4 step 4). Then apply §6.4's
   48-hour rule to every other secret the same person or process could reach.

§11.4: "Compromise has never happened in the project's history. This procedure
exists so it's answered before it does."

## Restoring a key (§9.2)

Retrieve the encrypted backup from the password manager, decrypt, and:

```sh
flyctl secrets set STUDIUM_SIGNING_KEY=<seed> -a studium-backend
studium ops smoke-test
```

5–10 minutes. The smoke test's signing check is the verification: it signs a
probe with the configured private key and verifies it against what
`signing_keys` publishes, which catches the failure a botched restore actually
produces — a private key that does not match the published public half.

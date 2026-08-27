# Migrating source storage to R2

Infrastructure §4.5, §14.3. **Trigger: source volume over 30GB** (roughly 300
sources at ~100MB each including intermediates).

R2 over S3 for egress: R2 charges none, S3 charges per GB out (§4.5).

## Check where you are

```sh
python - <<'PY'
from studium.storage import default_storage
storage = default_storage()
total = storage.total_bytes()
print(f"{storage.name}: {total / 1024**3:.1f} GB")
print(f"§14.3 fires at 30 GB; the volume is 50 GB")
PY
```

## What the migration is, and is not

§4.5: "Migration is a configuration change plus a one-time data migration, not
a code change."

That is true because every byte of source I/O goes through
`studium.storage` — `LocalStorage` turns a key into a path on the volume,
`R2Storage` turns the same key into an object in a bucket, and
`studium.ingestion.storage` composes the keys and knows nothing about either.

**One thing is a code change, and it is named rather than hidden.**
`pdfplumber` opens a file, not a byte stream, so `studium.ingestion.extract`
needs a real filesystem path. `Storage.local_path` raises
`StorageUnavailable` on the R2 backend rather than inventing one — deliberately
loud, so the day this migration happens the failure is a clear exception at a
known call site instead of a subtle one at runtime. The fix is to download to a
temporary file before extraction, in that one function. See
DIVERGENCES-INFRASTRUCTURE (N5).

Re-extraction is the only path that needs it. Serving, chunking and embedding
all read through the byte interface.

## Procedure

1. **Provision.** Create the bucket in Cloudflare, then:

   ```sh
   flyctl secrets set \
     STUDIUM_R2_BUCKET=studium-sources \
     STUDIUM_R2_ENDPOINT=https://<account>.r2.cloudflarestorage.com \
     STUDIUM_R2_ACCESS_KEY_ID=... \
     STUDIUM_R2_SECRET_ACCESS_KEY=... \
     -a studium-backend
   ```

2. **Snapshot the volume first.** §9.1's "manual snapshot before any risky
   operation", and this is one:

   ```sh
   flyctl volumes snapshots create <volume_id>
   ```

3. **Copy, with the backend still on `local`.** Both backends address the same
   keys, so this is a read from one and a write to the other:

   ```sh
   pip install -e ".[r2]"
   python - <<'PY'
   from studium.storage import LocalStorage, R2Storage
   source, target = LocalStorage(), R2Storage()
   copied = skipped = 0
   for key in source.keys():
       if target.exists(key):
           skipped += 1
           continue
       target.write(key, source.read(key))
       copied += 1
   print(f"copied {copied}, already present {skipped}")
   PY
   ```

   Idempotent, so a run that dies halfway is fixed by running it again.

4. **Verify before switching.** Every key, and every byte count:

   ```sh
   python - <<'PY'
   from studium.storage import LocalStorage, R2Storage
   source, target = LocalStorage(), R2Storage()
   missing, wrong = [], []
   for key in source.keys():
       if not target.exists(key):
           missing.append(key)
       elif target.size(key) != source.size(key):
           wrong.append(key)
   print(f"missing {len(missing)}, size mismatch {len(wrong)}")
   for key in (missing + wrong)[:20]:
       print("  ", key)
   PY
   ```

   Sizes rather than hashes because the objects are large and unchanged bytes
   are the claim; if anything mismatches, re-copy that key rather than trusting
   it.

5. **Switch.** `STUDIUM_STORAGE_BACKEND=r2` in `backend/fly.toml`, then
   `flyctl deploy`. An unrecognised value raises rather than falling back:
   silently serving from the volume while the deployment believes it is serving
   from R2 is the failure that looks like a partial migration.

6. **Verify the running system**, then leave the volume in place for at least a
   month. Detaching it is the step that cannot be undone, and R2's first month
   is when you find out what the migration missed.

## Cost, for the record

§13.2 puts R2 at ~$5–15/month at the scale that triggers this. The volume it
replaces is part of the ~$50–100 Fly line. The migration is not about saving
money; it is about a 50GB volume that stops growing.

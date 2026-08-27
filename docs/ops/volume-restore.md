# Restoring the source volume

Infrastructure §9.2. Typical time: 10–15 minutes.

The Fly volume mounted at `/data` holds source PDFs and the extracted and
normalised intermediates (ingestion §4). Fly snapshots it daily and keeps 7
days — a shorter window than Postgres's 14, which matters: **the two backups
have different retention, so a restore older than a week can recover the
database and not the files it points at.**

## Before a risky operation

§9.1: "Manual snapshot before any risky operation." Re-extracting a corpus,
running a storage migration, or anything that writes across many sources:

```sh
flyctl volumes list -a studium-backend
flyctl volumes snapshots create <volume_id>
```

## Restoring

```sh
flyctl volumes snapshots list <volume_id>
flyctl volumes create studium_sources --snapshot-id <snapshot_id> \
  --region yyz --size 50 -a studium-backend
```

A volume is attached to one machine, so the machine has to be pointed at the
new one and restarted. With a single instance that is a brief outage; §4.2
accepts that trade for MVP.

## Then check the database still agrees with the files

This is the step that gets skipped and the one that matters. `sources` rows
carry `storage_path`, and a volume restored to an earlier point has fewer files
than the database has rows. Nothing raises: extraction fails per-source, later,
with a `FileNotFoundError` that reads as a bad upload.

```sh
python - <<'PY'
from studium.db import SessionLocal
from studium.storage import default_storage
from sqlalchemy import text

storage = default_storage()
with SessionLocal() as s:
    rows = s.execute(text(
        "SELECT id, title, storage_path FROM sources WHERE deleted_at IS NULL"
    )).all()

missing = [r for r in rows if r.storage_path and not storage.exists(f"{r.id}.pdf")]
print(f"{len(rows)} sources, {len(missing)} with no file on the volume")
for row in missing:
    print(f"  {row.id}  {row.title}")
PY
```

For each missing source, either re-upload the PDF (`studium ingest pdf`) or
soft-delete the row. Leaving it is the bad option: retrieval keeps returning
chunks derived from bytes that are gone, and citations resolve to passages
nobody can check against a document.

## Why the intermediates are worth restoring rather than recomputing

`extracted.jsonl` and `normalized.jsonl` can both be regenerated from the PDF.
Doing so costs a full re-extraction — minutes per 500-page book — and, more to
the point, regenerates them with the *current* extractor rather than the one
that produced the chunks in the database. `sources.extractor_version` would
then describe text nobody has, which is precisely the confident-wrong
provenance ingestion §13's invariant exists to forbid.

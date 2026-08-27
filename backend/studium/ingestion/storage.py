"""Where a source's bytes and intermediate artefacts live (ingestion §4).

    {source_id}.pdf                  the original upload, never rewritten
    {source_id}/extracted.jsonl      one line per page, from §6.3
    {source_id}/normalized.jsonl     one line per page, from §6.4

The intermediates are kept rather than recomputed for two reasons. A reviewer
triaging an ``extractor_failure`` needs to see what the extractor actually
produced, and diffing extracted against normalized is the only way to tell an
extraction problem from a normalisation one. And re-chunking after a chunker
change should not have to re-run extraction, which on a 500-page book is the
expensive stage.

JSONL rather than one JSON document: the stages stream page by page, and a
partially written array is unreadable where a partially written JSONL file is
just short. That matters on the failure path, which is when someone reads it.

``sources.storage_path`` stays opaque to the schema (data layer §6.3), so this
module is the only thing that knows the layout.

**The bytes now go through ``studium.storage``** (infrastructure §4.5), which
is what makes moving to R2 a configuration change rather than a rewrite of this
file. The names above became *keys*; what turns a key into a file on the Fly
volume, or into an object in a bucket, is the backend's business and no longer
this module's. The path-returning helpers below are kept because ingestion's
own call sites and tests read them, and because ``pdfplumber`` genuinely needs
a filesystem path -- they delegate to ``Storage.local_path``, which raises on
the R2 backend rather than inventing one. See DIVERGENCES-INFRASTRUCTURE (N5).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from studium.storage import Storage, default_storage

#: Key suffixes, in one place so the layout is stated once.
PDF_SUFFIX = ".pdf"
EXTRACTED_NAME = "extracted.jsonl"
NORMALIZED_NAME = "normalized.jsonl"


def _storage() -> Storage:
    """Resolved per call, not cached at import.

    Same reason ``storage_root`` was: tests point the backend and the root at a
    temporary directory, and a module-level constant would freeze the
    deployment default before they got the chance.
    """
    return default_storage()


# --- keys ------------------------------------------------------------------


def source_pdf_key(source_id: uuid.UUID) -> str:
    return f"{source_id}{PDF_SUFFIX}"


def extracted_key(source_id: uuid.UUID) -> str:
    return f"{source_id}/{EXTRACTED_NAME}"


def normalized_key(source_id: uuid.UUID) -> str:
    return f"{source_id}/{NORMALIZED_NAME}"


# --- paths (local backend only) --------------------------------------------


def storage_root() -> Path:
    """The local backend's root. Raises on a backend that has no filesystem."""
    storage = _storage()
    if not hasattr(storage, "root"):
        from studium.storage import StorageUnavailable

        raise StorageUnavailable(
            f"the {storage.name!r} backend has no filesystem root"
        )
    return storage.root  # type: ignore[attr-defined]


def source_pdf_path(source_id: uuid.UUID) -> Path:
    return _storage().local_path(source_pdf_key(source_id))


def source_dir(source_id: uuid.UUID) -> Path:
    return _storage().local_path(str(source_id))


def extracted_path(source_id: uuid.UUID) -> Path:
    return _storage().local_path(extracted_key(source_id))


def normalized_path(source_id: uuid.UUID) -> Path:
    return _storage().local_path(normalized_key(source_id))


# --- reads and writes ------------------------------------------------------


def store_pdf(source_id: uuid.UUID, data: bytes) -> Path:
    """Write the upload. Refuses to overwrite an existing object.

    A source's bytes are immutable by §2: a new version of a document is a new
    ``sources`` row, never a rewrite of an old one. Silently overwriting would
    leave every chunk, citation and embedding already derived from the old
    bytes pointing at content that no longer exists, with the content hash on
    the row still describing what was replaced.
    """
    key = source_pdf_key(source_id)
    storage = _storage()
    if storage.exists(key):
        raise FileExistsError(
            f"{key} already exists; a new version of a source is a new row (§2)"
        )
    storage.write(key, data)
    return storage.local_path(key)


def write_jsonl(path_or_key: Path | str, records: Sequence[dict[str, Any]]) -> Path:
    """Write records atomically.

    Accepts a key or a path; a path is converted back to a key relative to the
    storage root. The dual signature is transitional and deliberate -- every
    caller in ``pipeline.py`` passes the result of ``extracted_path`` or
    ``normalized_path``, and changing those call sites and this function in one
    step would have meant no test running against either shape.

    Atomicity is the backend's, not this function's: ``LocalStorage.write``
    goes through a temporary file and a rename, because a stage that crashes
    midway through a direct write leaves a truncated file the next stage reads
    as a short document.
    """
    key = _as_key(path_or_key)
    body = "".join(
        json.dumps(record, ensure_ascii=False) + "\n" for record in records
    )
    storage = _storage()
    storage.write(key, body.encode("utf-8"), overwrite=True)
    return storage.local_path(key)


def read_jsonl(path_or_key: Path | str) -> Iterator[dict[str, Any]]:
    key = _as_key(path_or_key)
    storage = _storage()
    if not storage.exists(key):
        raise FileNotFoundError(f"no file at {key}")
    for line in storage.read(key).decode("utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def _as_key(path_or_key: Path | str) -> str:
    """Normalise a caller's path or key into a storage key."""
    if isinstance(path_or_key, Path):
        try:
            return path_or_key.resolve().relative_to(storage_root().resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"{path_or_key} is outside the storage root {storage_root()}; "
                f"pass a key relative to it"
            ) from exc
    return str(path_or_key)

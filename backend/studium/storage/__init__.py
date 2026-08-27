"""Object storage, local or R2 (infrastructure §4.5).

§4.5: "All code accesses source files via ``studium.storage`` interface. Local
implementation reads from the Fly volume; R2 implementation reads via
S3-compatible API. Migration is a configuration change plus a one-time data
migration, not a code change."

**The interface is bytes and keys, not paths.** That is the whole design
decision, and it is the one that makes the promise above true. A ``Path``-based
interface cannot be implemented over an object store: ``Path.exists`` on a
bucket is a network call with a different failure mode, and an ``open()``
handle over HTTP is not the same object. So the protocol below is four
operations over opaque string keys, and ``LocalStorage`` is the one that
happens to turn a key into a path.

``studium.ingestion.storage`` keeps its ``{source_id}.pdf`` and
``{source_id}/extracted.jsonl`` layout, because that layout is ingestion's
business and is described in ingestion §4. What changed is that it now composes
those names into *keys* and hands them here, rather than opening files itself.

**One leak, named rather than hidden.** ``pdfplumber`` opens a file, not a byte
stream we control the lifetime of, so ``studium.ingestion.extract`` still needs
a real path. :meth:`Storage.local_path` exists for exactly that caller and
raises on the R2 backend, which is the honest shape: the day storage moves to
R2, extraction downloads to a temp file first, and that is a code change in one
function rather than a surprise at runtime. See DIVERGENCES-INFRASTRUCTURE (N5).

**The R2 backend is written and unexercised.** §4.5 provisions R2 and does not
use it until §14.3's 30GB trigger fires, so there is no bucket to test against
and ``boto3`` is not a dependency the MVP install pulls. It is here because a
migration path nobody has written is a migration path nobody can cost;
:func:`default_storage` will not select it silently, and the Tier 1 tests cover
its key handling and not its network behaviour.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

__all__ = [
    "LocalStorage",
    "R2Storage",
    "Storage",
    "StorageError",
    "StorageUnavailable",
    "default_storage",
    "reset_default_storage",
]

BACKEND_ENV = "STUDIUM_STORAGE_BACKEND"


class StorageError(RuntimeError):
    """A storage operation failed for a reason the caller can act on."""


class StorageUnavailable(StorageError):
    """The configured backend cannot be constructed (missing SDK or config)."""


@runtime_checkable
class Storage(Protocol):
    """What every backend provides.

    Deliberately small. Every operation ingestion performs is one of these,
    and each additional method is one more thing the R2 implementation has to
    get right before the migration §4.5 promises is a configuration change.
    """

    name: str

    def read(self, key: str) -> bytes: ...

    def write(self, key: str, data: bytes, *, overwrite: bool = False) -> None: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> bool: ...

    def size(self, key: str) -> int: ...

    def keys(self, prefix: str = "") -> Iterator[str]: ...

    def local_path(self, key: str) -> Path:
        """A real filesystem path, for the one caller that needs one.

        Raises :class:`StorageUnavailable` on backends that have none.
        """
        ...

    def total_bytes(self, prefix: str = "") -> int:
        """Everything under ``prefix``. Feeds §14.3's 30GB scaling trigger."""
        ...


class LocalStorage:
    """Files on the Fly volume (§4.5's MVP backend)."""

    name = "local"

    def __init__(self, root: str | Path | None = None) -> None:
        self._root = Path(root) if root is not None else None

    @property
    def root(self) -> Path:
        """Read per call rather than frozen at construction.

        The tests point ``STUDIUM_SOURCE_STORAGE_ROOT`` at a temporary
        directory with monkeypatch, and a root captured in ``__init__`` would
        freeze the deployment default (``/data/sources``) before they got the
        chance -- which on a developer's machine means Tier 2 writing into a
        path that does not exist, and on CI means writing into one that does.
        """
        if self._root is not None:
            return self._root
        from studium.config import settings

        return Path(settings.source_storage_root)

    def _path(self, key: str) -> Path:
        path = (self.root / _safe_key(key)).resolve()
        root = self.root.resolve()
        # Defence in depth behind _safe_key. Source ids are UUIDs the pipeline
        # generates, so a traversal is not reachable from a learner today --
        # but "the key is trustworthy" is a property of the caller, and this is
        # the layer that turns a key into a filesystem write.
        if not str(path).startswith(str(root)):
            raise StorageError(f"key {key!r} escapes the storage root")
        return path

    def read(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise FileNotFoundError(f"no object at {key}")
        return path.read_bytes()

    def write(self, key: str, data: bytes, *, overwrite: bool = False) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            raise FileExistsError(f"{key} already exists")
        # Through a temporary file and a rename, so a crash mid-write leaves
        # the old object or nothing rather than a truncated one. Ingestion
        # reads these back as JSONL and a short file parses cleanly as a short
        # document -- which surfaces as "the second half of this book did not
        # get chunked" long after the crash that caused it.
        temporary = path.with_suffix(path.suffix + ".partial")
        temporary.write_bytes(data)
        temporary.replace(path)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if not path.exists():
            return False
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True

    def size(self, key: str) -> int:
        return self._path(key).stat().st_size

    def keys(self, prefix: str = "") -> Iterator[str]:
        root = self.root
        if not root.exists():
            return
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            key = path.relative_to(root).as_posix()
            if key.startswith(prefix):
                yield key

    def local_path(self, key: str) -> Path:
        return self._path(key)

    def total_bytes(self, prefix: str = "") -> int:
        return sum(self.size(k) for k in self.keys(prefix))


class R2Storage:
    """Cloudflare R2 over the S3-compatible API (§4.5, after §14.3 fires).

    R2 over S3 for egress cost: "R2 has no egress fees; S3 charges per GB out"
    (§4.5). The API is S3's, so the client is boto3 pointed at R2's endpoint.

    Unexercised against a real bucket. See the module docstring.
    """

    name = "r2"

    def __init__(
        self,
        *,
        bucket: str | None = None,
        endpoint: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ) -> None:
        self.bucket = bucket or os.environ.get("STUDIUM_R2_BUCKET", "")
        self.endpoint = endpoint or os.environ.get("STUDIUM_R2_ENDPOINT", "")
        self._access_key_id = access_key_id or os.environ.get(
            "STUDIUM_R2_ACCESS_KEY_ID", ""
        )
        self._secret_access_key = secret_access_key or os.environ.get(
            "STUDIUM_R2_SECRET_ACCESS_KEY", ""
        )
        self._client = None

    def client(self):  # noqa: ANN201 -- boto3 has no stable public type
        if self._client is not None:
            return self._client
        missing = [
            name
            for name, value in (
                ("STUDIUM_R2_BUCKET", self.bucket),
                ("STUDIUM_R2_ENDPOINT", self.endpoint),
                ("STUDIUM_R2_ACCESS_KEY_ID", self._access_key_id),
                ("STUDIUM_R2_SECRET_ACCESS_KEY", self._secret_access_key),
            )
            if not value
        ]
        if missing:
            raise StorageUnavailable(
                f"R2 backend selected but {', '.join(missing)} not set"
            )
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise StorageUnavailable(
                "boto3 is not installed; install the 'r2' extra "
                "(pip install -e '.[r2]'). §4.5 provisions R2 but does not use "
                "it until §14.3's 30GB trigger fires, so it is not in the MVP "
                "install."
            ) from exc
        self._client = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=self._access_key_id,
            aws_secret_access_key=self._secret_access_key,
            # R2 ignores the region but the SDK requires one.
            region_name="auto",
        )
        return self._client

    def read(self, key: str) -> bytes:
        key = _safe_key(key)
        try:
            response = self.client().get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 -- botocore's exceptions are dynamic
            if _is_not_found(exc):
                raise FileNotFoundError(f"no object at {key}") from exc
            raise StorageError(f"R2 read failed for {key}: {exc}") from exc
        return response["Body"].read()

    def write(self, key: str, data: bytes, *, overwrite: bool = False) -> None:
        key = _safe_key(key)
        if not overwrite and self.exists(key):
            raise FileExistsError(f"{key} already exists")
        self.client().put_object(Bucket=self.bucket, Key=key, Body=data)

    def exists(self, key: str) -> bool:
        try:
            self.client().head_object(Bucket=self.bucket, Key=_safe_key(key))
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return False
            raise StorageError(f"R2 head failed for {key}: {exc}") from exc
        return True

    def delete(self, key: str) -> bool:
        if not self.exists(key):
            return False
        self.client().delete_object(Bucket=self.bucket, Key=_safe_key(key))
        return True

    def size(self, key: str) -> int:
        response = self.client().head_object(Bucket=self.bucket, Key=_safe_key(key))
        return int(response["ContentLength"])

    def keys(self, prefix: str = "") -> Iterator[str]:
        paginator = self.client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                yield item["Key"]

    def local_path(self, key: str) -> Path:
        raise StorageUnavailable(
            f"the R2 backend has no local path for {key!r}. The one caller that "
            f"needs one is pdfplumber in studium.ingestion.extract; it has to "
            f"download to a temporary file first. See "
            f"DIVERGENCES-INFRASTRUCTURE (N5)."
        )

    def total_bytes(self, prefix: str = "") -> int:
        paginator = self.client().get_paginator("list_objects_v2")
        return sum(
            item["Size"]
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix)
            for item in page.get("Contents", [])
        )


def _is_not_found(exc: Exception) -> bool:
    code = getattr(getattr(exc, "response", None), "get", lambda _: None)("Error") or {}
    return str(code.get("Code", "")) in {"404", "NoSuchKey", "NotFound"}


def _safe_key(key: str) -> str:
    """Normalise a key and refuse traversal.

    Object stores have no directories, so ``a/../b`` is a *different key* from
    ``b`` in R2 and the *same file* on a volume. Refusing both spellings keeps
    the two backends addressing the same object, which is the property §4.5's
    "migration is a configuration change" quietly depends on.
    """
    cleaned = key.strip().lstrip("/")
    if not cleaned:
        raise StorageError("empty storage key")
    parts = cleaned.split("/")
    if any(part in {"..", "."} for part in parts):
        raise StorageError(f"key {key!r} contains a relative segment")
    return "/".join(parts)


_default: Storage | None = None


def default_storage() -> Storage:
    """The configured backend, built once.

    Selected by ``STUDIUM_STORAGE_BACKEND``. Defaults to local, and an
    unrecognised value raises rather than falling back: silently serving from
    the volume when the deployment believes it is serving from R2 is the
    failure that looks like a partial migration.
    """
    global _default
    if _default is None:
        _default = build_storage(os.environ.get(BACKEND_ENV, "local"))
        log.info("storage backend: %s", _default.name)
    return _default


def build_storage(backend: str) -> Storage:
    backend = (backend or "local").strip().lower()
    if backend == "local":
        return LocalStorage()
    if backend == "r2":
        return R2Storage()
    raise StorageUnavailable(
        f"{BACKEND_ENV}={backend!r} is not a known backend; expected 'local' or 'r2'"
    )


def reset_default_storage() -> None:
    """Drop the cached backend. For tests that change the environment."""
    global _default
    _default = None

"""The storage abstraction (infrastructure §4.5).

§4.5 promises that moving from the Fly volume to R2 is "a configuration change
plus a one-time data migration, not a code change". These tests defend the two
properties that promise rests on:

* **Both backends address the same keys.** An object store has no directories,
  so ``a/../b`` is a different key in R2 and the same file on a volume.
  Normalising identically in both is what keeps them addressing the same
  object across the migration.
* **The one leak is loud.** ``local_path`` raises on R2 rather than inventing a
  path, so the day the migration happens the failure is a clear exception at a
  known call site rather than a subtle one at runtime.

``R2Storage``'s network behaviour is deliberately not tested. There is no
bucket to test against until §14.3's trigger fires, and a test against a
hand-rolled S3 double would assert what the double does. Its key handling is
tested, because that is the half that has to agree with the local backend.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from studium import storage as storage_module
from studium.storage import (
    LocalStorage,
    R2Storage,
    StorageError,
    StorageUnavailable,
    build_storage,
)


@pytest.fixture
def local(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path)


# --- the protocol ----------------------------------------------------------


def test_write_read_exists_delete(local: LocalStorage) -> None:
    key = "abc/extracted.jsonl"
    assert not local.exists(key)
    local.write(key, b"one\ntwo\n")
    assert local.exists(key)
    assert local.read(key) == b"one\ntwo\n"
    assert local.size(key) == 8
    assert local.delete(key) is True
    assert local.delete(key) is False, "deleting a missing key reports False, not raises"


def test_write_refuses_to_overwrite_by_default(local: LocalStorage) -> None:
    """Ingestion §2: a source's bytes are immutable.

    Silently overwriting leaves every chunk, citation and embedding derived
    from the old bytes pointing at content that no longer exists, with the
    content hash on the row still describing what was replaced.
    """
    local.write("a.pdf", b"first")
    with pytest.raises(FileExistsError):
        local.write("a.pdf", b"second")
    local.write("a.pdf", b"second", overwrite=True)
    assert local.read("a.pdf") == b"second"


def test_read_of_a_missing_key_raises_filenotfound(local: LocalStorage) -> None:
    """The same exception type both backends raise, so callers need one branch."""
    with pytest.raises(FileNotFoundError):
        local.read("nope")


def test_write_is_atomic(local: LocalStorage) -> None:
    """No `.partial` survives a completed write.

    A stage that crashes midway through a direct write leaves a truncated file
    that the next stage reads as a short document -- which surfaces as "the
    second half of this book did not get chunked" long after the crash.
    """
    local.write("x/normalized.jsonl", b"{}\n")
    leftovers = [p.name for p in local.root.rglob("*.partial")]
    assert not leftovers, leftovers


def test_keys_lists_files_under_a_prefix(local: LocalStorage) -> None:
    local.write("s1.pdf", b"a")
    local.write("s1/extracted.jsonl", b"b")
    local.write("s2.pdf", b"c")
    assert sorted(local.keys()) == ["s1.pdf", "s1/extracted.jsonl", "s2.pdf"]
    assert sorted(local.keys("s1")) == ["s1.pdf", "s1/extracted.jsonl"]


def test_total_bytes_feeds_the_scaling_trigger(local: LocalStorage) -> None:
    """§14.3 fires at 30GB of source volume, measured through this."""
    local.write("a.pdf", b"x" * 100)
    local.write("b/extracted.jsonl", b"y" * 50)
    assert local.total_bytes() == 150


def test_keys_on_a_missing_root_is_empty_not_an_error(tmp_path: Path) -> None:
    """A fresh deployment has no directory until the first write."""
    assert list(LocalStorage(tmp_path / "never-created").keys()) == []


# --- key normalisation, which both backends must agree on ------------------


@pytest.mark.parametrize(
    "key", ["../etc/passwd", "a/../../b", "./a", "a/./b", "/", "", "   "]
)
def test_traversal_and_relative_segments_are_refused(key: str) -> None:
    with pytest.raises(StorageError):
        storage_module._safe_key(key)


def test_leading_slashes_are_stripped_identically() -> None:
    """The migration depends on this.

    ``/a/b`` and ``a/b`` are the same file on a volume and two different keys
    in a bucket. Normalising in one place is what stops half the corpus
    becoming unreachable at the moment the backend changes.
    """
    assert storage_module._safe_key("/a/b") == "a/b"
    assert storage_module._safe_key("  a/b  ") == "a/b"


def test_local_backend_refuses_to_escape_its_root(local: LocalStorage) -> None:
    """Defence in depth behind _safe_key.

    Source ids are UUIDs the pipeline generates, so a traversal is not
    reachable from a learner today -- but "the key is trustworthy" is a
    property of the caller, and this is the layer that turns a key into a
    filesystem write.
    """
    with pytest.raises(StorageError):
        local.write("../escaped", b"x")


# --- the R2 backend's shape ------------------------------------------------


def test_r2_local_path_raises_and_names_the_caller() -> None:
    """The one leak in the abstraction, made loud.

    pdfplumber opens a file rather than a byte stream, so
    studium.ingestion.extract needs a real path. Raising here means the
    migration's one code change announces itself at a known call site instead
    of failing subtly at runtime.
    """
    with pytest.raises(StorageUnavailable) as caught:
        R2Storage(bucket="b", endpoint="e", access_key_id="k", secret_access_key="s").local_path("a.pdf")
    assert "pdfplumber" in str(caught.value)


def test_r2_reports_its_missing_configuration_by_name() -> None:
    """"R2 is not working" is not a diagnosis anyone can act on."""
    with pytest.raises(StorageUnavailable) as caught:
        R2Storage(bucket="", endpoint="", access_key_id="", secret_access_key="").client()
    message = str(caught.value)
    assert "STUDIUM_R2_BUCKET" in message
    assert "STUDIUM_R2_ENDPOINT" in message


# --- backend selection -----------------------------------------------------


def test_build_storage_selects_by_name() -> None:
    assert build_storage("local").name == "local"
    assert build_storage("r2").name == "r2"
    assert build_storage("").name == "local", "empty defaults to local"
    assert build_storage("LOCAL").name == "local"


def test_an_unknown_backend_raises_rather_than_falling_back() -> None:
    """Silently serving from the volume while the deployment believes it is
    serving from R2 is the failure that looks like a partial migration."""
    with pytest.raises(StorageUnavailable, match="not a known backend"):
        build_storage("s3")


def test_default_storage_is_cached_and_resettable(monkeypatch: pytest.MonkeyPatch) -> None:
    storage_module.reset_default_storage()
    monkeypatch.setenv(storage_module.BACKEND_ENV, "local")
    first = storage_module.default_storage()
    assert storage_module.default_storage() is first
    storage_module.reset_default_storage()
    assert storage_module.default_storage() is not first


def test_local_root_follows_the_setting_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tests point the root at a temporary directory with monkeypatch.

    A root captured in __init__ would freeze the deployment default
    (/data/sources) before they got the chance -- which on a developer's
    machine means writing to a path that does not exist, and in CI means
    writing to one that does.
    """
    monkeypatch.setattr("studium.config.settings.source_storage_root", str(tmp_path))
    assert LocalStorage().root == tmp_path


# --- ingestion still addresses the same layout -----------------------------


def test_ingestion_keys_match_ingestion_section_4(monkeypatch, tmp_path: Path) -> None:
    """The layout ingestion §4 documents, now expressed as keys.

    Pinned because it is the contract between two subsystems: ingestion owns
    the names, storage owns what a name resolves to, and a change to either
    that did not change the other would leave existing sources unreachable.
    """
    from studium.ingestion import storage as ingestion_storage

    source_id = uuid.UUID("00000000-0000-7000-8000-000000000001")
    assert ingestion_storage.source_pdf_key(source_id) == f"{source_id}.pdf"
    assert ingestion_storage.extracted_key(source_id) == f"{source_id}/extracted.jsonl"
    assert ingestion_storage.normalized_key(source_id) == f"{source_id}/normalized.jsonl"


def test_ingestion_round_trips_jsonl_through_the_abstraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from studium.ingestion import storage as ingestion_storage

    monkeypatch.setattr("studium.config.settings.source_storage_root", str(tmp_path))
    storage_module.reset_default_storage()
    monkeypatch.setenv(storage_module.BACKEND_ENV, "local")

    source_id = uuid.uuid4()
    records = [{"page": 1, "text": "one"}, {"page": 2, "text": "twö"}]
    path = ingestion_storage.write_jsonl(
        ingestion_storage.extracted_path(source_id), records
    )
    assert path.exists()
    assert list(ingestion_storage.read_jsonl(path)) == records
    # And by key, which is what the R2 backend would be handed.
    assert list(
        ingestion_storage.read_jsonl(ingestion_storage.extracted_key(source_id))
    ) == records


def test_store_pdf_refuses_a_second_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from studium.ingestion import storage as ingestion_storage

    monkeypatch.setattr("studium.config.settings.source_storage_root", str(tmp_path))
    storage_module.reset_default_storage()
    monkeypatch.setenv(storage_module.BACKEND_ENV, "local")

    source_id = uuid.uuid4()
    ingestion_storage.store_pdf(source_id, b"%PDF-1.4")
    with pytest.raises(FileExistsError, match="new row"):
        ingestion_storage.store_pdf(source_id, b"%PDF-1.4 different")

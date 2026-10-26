"""BlobStorage: local directory store, in-memory mock, S3 stub (REQUIREMENTS.md SEL-12, INT-26,
ENG-Q9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from wholesaling.config.settings import Settings
from wholesaling.integrations.blob import (
    BlobKeyError,
    BlobNotFound,
    BlobStorage,
    LocalDirStorage,
    MockBlobStorage,
    get_blob_storage,
    validate_key,
)


@pytest.mark.parametrize("key", ["sell/abc/01.jpg", "a", "x.y/z-1_2.png"])
def test_validate_key_accepts_safe_paths(key: str) -> None:
    assert validate_key(key) == key


@pytest.mark.parametrize(
    "key", ["", "/abs", "../up", "a/../b", "a//b", "sp ace", ".hidden", "a" * 600]
)
def test_validate_key_rejects_unsafe_paths(key: str) -> None:
    with pytest.raises(BlobKeyError):
        validate_key(key)


async def test_local_dir_storage_round_trip(tmp_path: Path) -> None:
    store = LocalDirStorage(tmp_path / "blobs")
    assert isinstance(store, BlobStorage)
    record = await store.put("sell/s1/01.jpg", b"\xff\xd8photo", content_type="image/jpeg")
    assert record.key == "sell/s1/01.jpg" and record.size == 7 and len(record.sha256) == 64
    assert (tmp_path / "blobs" / "sell" / "s1" / "01.jpg").read_bytes() == b"\xff\xd8photo"
    assert await store.exists("sell/s1/01.jpg")
    assert await store.get("sell/s1/01.jpg") == b"\xff\xd8photo"
    assert await store.delete("sell/s1/01.jpg") is True
    assert await store.delete("sell/s1/01.jpg") is False
    with pytest.raises(BlobNotFound):
        await store.get("sell/s1/01.jpg")
    health = await store.healthcheck()
    assert health.ok and health.name == "blob"


async def test_local_dir_storage_overwrites_and_leaves_no_temp_files(tmp_path: Path) -> None:
    store = LocalDirStorage(tmp_path)
    await store.put("k/one.bin", b"1", content_type="application/octet-stream")
    await store.put("k/one.bin", b"22", content_type="application/octet-stream")
    assert await store.get("k/one.bin") == b"22"
    assert not [p for p in (tmp_path / "k").iterdir() if p.name.startswith(".tmp-")]


async def test_local_dir_storage_refuses_escaping_keys(tmp_path: Path) -> None:
    store = LocalDirStorage(tmp_path)
    with pytest.raises(BlobKeyError):
        await store.put("../escape.txt", b"x", content_type="text/plain")


async def test_mock_storage_records_calls() -> None:
    mock = MockBlobStorage()
    await mock.put("a/b.png", b"png", content_type="image/png")
    assert await mock.get("a/b.png") == b"png"
    assert [c["method"] for c in mock.calls] == ["put", "get"]
    assert await mock.exists("a/b.png") and not await mock.exists("nope")
    with pytest.raises(BlobNotFound):
        await mock.get("nope")
    mock.fail_next_put = OSError("disk full")
    with pytest.raises(OSError, match="disk full"):
        await mock.put("c", b"x", content_type="text/plain")
    mock.reset()
    assert not mock.blobs and not mock.calls


def test_factory_local_default_and_s3_stub(tmp_path: Path) -> None:
    s = Settings(_env_file=None, app_env="test", blob_local_dir=str(tmp_path))  # type: ignore[call-arg]
    store = get_blob_storage(s)
    assert isinstance(store, LocalDirStorage) and store.root == tmp_path.resolve()
    with pytest.raises(NotImplementedError, match="ENG-Q9"):
        get_blob_storage(Settings(_env_file=None, app_env="test", blob_mode="s3"))  # type: ignore[call-arg]

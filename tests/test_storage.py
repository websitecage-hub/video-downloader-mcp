import time

import pytest

from app.storage import FileStore, StorageFullError


def make_file(path, size=1024):
    path.write_bytes(b"x" * size)
    return path


def test_add_get_and_stats(tmp_path):
    store = FileStore(tmp_path, ttl_seconds=3600, max_total_bytes=10_000)
    source = make_file(tmp_path / "source.mp4", 2048)

    item = store.add(
        source,
        title="My Video",
        source_url="https://example.com/v",
        platform="Example",
        duration=12.5,
    )

    assert item.filename == "My Video.mp4"
    assert item.content_type == "video/mp4"
    assert item.size == 2048
    assert not source.exists()
    assert store.get(item.token) is item

    stats = store.stats()
    assert stats["files"] == 1
    assert stats["total_mb"] == 0.0


def test_expired_file_is_not_served(tmp_path):
    store = FileStore(tmp_path, ttl_seconds=0, max_total_bytes=10_000)
    item = store.add(
        make_file(tmp_path / "source.mp4"),
        title="Gone",
        source_url="https://example.com",
        platform="Example",
    )
    time.sleep(0.01)
    assert store.get(item.token) is None


def test_cleanup_removes_expired_files(tmp_path):
    store = FileStore(tmp_path, ttl_seconds=0, max_total_bytes=10_000)
    item = store.add(
        make_file(tmp_path / "source.mp4"),
        title="Gone",
        source_url="https://example.com",
        platform="Example",
    )
    time.sleep(0.01)
    assert store.cleanup() == 1
    assert not item.path.exists()


def test_storage_limit(tmp_path):
    store = FileStore(tmp_path, ttl_seconds=3600, max_total_bytes=1000)
    with pytest.raises(StorageFullError):
        store.add(
            make_file(tmp_path / "big.mp4", 2000),
            title="Big",
            source_url="https://example.com",
            platform="Example",
        )


def test_second_file_respects_total_limit(tmp_path):
    store = FileStore(tmp_path, ttl_seconds=3600, max_total_bytes=3000)
    store.add(
        make_file(tmp_path / "one.mp4", 2000),
        title="One",
        source_url="https://example.com",
        platform="Example",
    )
    with pytest.raises(StorageFullError):
        store.add(
            make_file(tmp_path / "two.mp4", 2000),
            title="Two",
            source_url="https://example.com",
            platform="Example",
        )

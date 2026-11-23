"""Tests for the shared image-cache policy module.

The point of `app.core.cache` is that the cover cache can never grow without
bound again, so the eviction behaviour is the part worth pinning: which entries
go, in what order, and that the cheap "nothing to do" path really is cheap.

`prune()` and `clear()` record their result in a state file next to the cache, so
an autouse fixture redirects that file into `tmp_path` — and the directory under
test is a `covers/` subdirectory, keeping the two from ever colliding.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from app.core import cache


@pytest.fixture(autouse=True)
def _isolated_state_file(tmp_path: Path, monkeypatch) -> None:
    """Keep every test off the repo's real `.cache/covers.state.json`."""
    monkeypatch.setattr(cache, "_STATE_FILE", tmp_path / "covers.state.json")


@pytest.fixture
def covers(tmp_path: Path) -> Path:
    """The directory to treat as the cache (kept apart from the state file)."""
    path = tmp_path / "covers"
    path.mkdir()
    return path


def _make(directory: Path, name: str, size: int, mtime: float) -> Path:
    path = directory / name
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


# -- keys ------------------------------------------------------------------- #


def test_cache_key_is_stable_and_md5_shaped() -> None:
    url = "https://cdn.example.com/a.webp?e=1&m=2"
    key = cache.cache_key(url)
    assert key == cache.cache_key(url)
    assert len(key) == 32
    # The query string stays part of the key: that is the legacy scheme, and
    # changing it would invalidate every cached cover.
    assert key != cache.cache_key("https://cdn.example.com/a.webp?e=1&m=3")


def test_cache_file_lives_under_the_given_directory(tmp_path: Path) -> None:
    path = cache.cache_file("https://x/y.jpg", directory=tmp_path)
    assert path.parent == tmp_path
    assert path.name == f"{cache.cache_key('https://x/y.jpg')}.jpg"


# -- stats ------------------------------------------------------------------ #


def test_size_bytes_and_count(covers: Path) -> None:
    _make(covers, "a.jpg", 10, 1000)
    _make(covers, "b.jpg", 20, 2000)
    assert cache.count(covers) == 2
    assert cache.size_bytes(covers) == 30


def test_a_missing_directory_reads_as_empty(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert cache.count(missing) == 0
    assert cache.size_bytes(missing) == 0


def test_subdirectories_are_not_counted(covers: Path) -> None:
    (covers / "sub").mkdir()
    _make(covers, "a.jpg", 10, 1000)
    assert cache.count(covers) == 1


# -- prune ------------------------------------------------------------------ #


def test_prune_evicts_the_least_recently_used_first(covers: Path) -> None:
    _make(covers, "old.jpg", 100, 1000)
    _make(covers, "mid.jpg", 100, 2000)
    _make(covers, "new.jpg", 100, 3000)

    result = cache.prune(200, directory=covers)

    assert result.files_removed == 1
    assert result.bytes_freed == 100
    assert result.files_kept == 2
    assert result.bytes_after == 200
    assert not (covers / "old.jpg").exists()
    assert (covers / "mid.jpg").exists()
    assert (covers / "new.jpg").exists()


def test_prune_evicts_only_down_to_the_cap(covers: Path) -> None:
    for i in range(5):
        _make(covers, f"{i}.jpg", 100, 1000 + i)

    result = cache.prune(250, directory=covers)

    assert result.files_removed == 3
    assert result.bytes_after == 200
    assert result.files_kept == 2


def test_prune_is_a_noop_when_already_under_the_cap(covers: Path) -> None:
    _make(covers, "a.jpg", 10, 1000)
    result = cache.prune(1000, directory=covers)
    assert result.files_removed == 0
    assert result.bytes_freed == 0
    assert result.files_kept == 1
    assert result.bytes_after == 10


def test_prune_of_a_missing_directory_is_harmless(tmp_path: Path) -> None:
    result = cache.prune(1000, directory=tmp_path / "nope")
    assert result == cache.PruneResult()


# -- clear ------------------------------------------------------------------ #


def test_clear_removes_everything(covers: Path) -> None:
    _make(covers, "a.jpg", 10, 1000)
    _make(covers, "b.jpg", 20, 2000)

    result = cache.clear(directory=covers)

    assert result.files_removed == 2
    assert result.bytes_freed == 30
    assert result.files_kept == 0
    assert result.bytes_after == 0
    assert list(covers.iterdir()) == []


# -- maybe_prune ------------------------------------------------------------ #


def _write_state(state: Path, directory: Path, age_seconds: float, bytes_after: int) -> None:
    state.write_text(
        json.dumps(
            {
                "at": time.time() - age_seconds,
                "directory": str(directory),
                "bytes_after": bytes_after,
                "files_kept": 1,
            }
        ),
        encoding="utf-8",
    )


def test_maybe_prune_short_circuits_once_the_state_is_fresh(covers: Path) -> None:
    _make(covers, "a.jpg", 10, 1000)

    assert cache.maybe_prune(1000, directory=covers) is not None  # first pass walks
    assert cache.maybe_prune(1000, directory=covers) is None  # now skips the walk


def test_maybe_prune_walks_again_when_the_state_is_stale(covers: Path) -> None:
    _make(covers, "a.jpg", 10, 1000)
    _write_state(cache._STATE_FILE, covers, age_seconds=48 * 3600, bytes_after=10)

    assert cache.maybe_prune(1000, directory=covers) is not None


def test_maybe_prune_walks_again_when_the_state_says_over_cap(covers: Path) -> None:
    _make(covers, "a.jpg", 10, 1000)
    _write_state(cache._STATE_FILE, covers, age_seconds=0, bytes_after=9999)

    assert cache.maybe_prune(1000, directory=covers) is not None


def test_maybe_prune_ignores_state_recorded_for_another_directory(covers: Path) -> None:
    _make(covers, "a.jpg", 10, 1000)
    _write_state(cache._STATE_FILE, covers.parent / "somewhere-else", age_seconds=0, bytes_after=0)

    assert cache.maybe_prune(1000, directory=covers) is not None


def test_maybe_prune_does_nothing_when_the_cache_does_not_exist(tmp_path: Path) -> None:
    assert cache.maybe_prune(1000, directory=tmp_path / "missing") is None


def test_a_corrupt_state_file_just_means_walk(covers: Path) -> None:
    cache._STATE_FILE.write_text("{not json", encoding="utf-8")
    _make(covers, "a.jpg", 10, 1000)

    assert cache.maybe_prune(1000, directory=covers) is not None


# -- MemoryBytesCache ------------------------------------------------------- #


def test_memory_cache_evicts_by_entry_count() -> None:
    mc = cache.MemoryBytesCache(max_entries=2, max_bytes=1_000_000)
    mc.put("a", b"1")
    mc.put("b", b"2")
    mc.put("c", b"3")

    assert len(mc) == 2
    assert mc.get("a") is None
    assert mc.get("c") == b"3"


def test_memory_cache_evicts_by_byte_budget() -> None:
    mc = cache.MemoryBytesCache(max_entries=100, max_bytes=10)
    mc.put("a", b"x" * 6)
    mc.put("b", b"y" * 6)

    assert len(mc) == 1
    assert mc.get("a") is None
    assert mc.size_bytes == 6


def test_memory_cache_get_refreshes_recency() -> None:
    mc = cache.MemoryBytesCache(max_entries=2, max_bytes=1_000_000)
    mc.put("a", b"1")
    mc.put("b", b"2")
    mc.get("a")  # 'a' is now the most recent
    mc.put("c", b"3")  # so 'b' is the one evicted

    assert mc.get("a") == b"1"
    assert mc.get("b") is None


def test_memory_cache_never_stores_an_empty_body() -> None:
    mc = cache.MemoryBytesCache()
    mc.put("a", b"")

    assert len(mc) == 0
    assert mc.get("a") is None


def test_memory_cache_replacing_a_key_does_not_double_count() -> None:
    mc = cache.MemoryBytesCache()
    mc.put("a", b"12345")
    mc.put("a", b"123")

    assert len(mc) == 1
    assert mc.size_bytes == 3


def test_memory_cache_drops_a_body_larger_than_the_whole_budget() -> None:
    mc = cache.MemoryBytesCache(max_entries=10, max_bytes=4)
    mc.put("a", b"12345")

    assert len(mc) == 0
    assert mc.size_bytes == 0


def test_memory_cache_clear() -> None:
    mc = cache.MemoryBytesCache()
    mc.put("a", b"123")
    mc.clear()

    assert len(mc) == 0
    assert mc.size_bytes == 0

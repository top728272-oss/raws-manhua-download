"""Pure-helper tests for the cover loader (no QApplication needed)."""

from __future__ import annotations

import os

import pytest

from app.gui.cover_loader import CoverLoader, CoverWorker, resolve_referer


def test_resolve_referer_by_source_id() -> None:
    referers = {"manhuagui": "https://www.manhuagui.com", "dumanwu": "https://www.dumanwu.com"}
    assert resolve_referer("https://eu1.hamreus.com/x.jpg", "manhuagui", referers) == (
        "https://www.manhuagui.com"
    )
    assert resolve_referer("https://p6.ecombdimg.com/x.jpg", "dumanwu", referers) == (
        "https://www.dumanwu.com"
    )


def test_resolve_referer_unknown_source() -> None:
    assert resolve_referer("https://cdn.example.com/x.jpg", "unknown", {"a": "https://a"}) is None
    assert resolve_referer("https://cdn.example.com/x.jpg", None, {"a": "https://a"}) is None
    assert resolve_referer("https://cdn.example.com/x.jpg", "a", {}) is None


def test_worker_build_headers_with_referer() -> None:
    worker = CoverWorker(
        "https://x/y.jpg",
        None,
        user_agent="UA-test",
        referer="https://site.com",  # type: ignore[arg-type]
    )
    assert worker.build_headers() == {"User-Agent": "UA-test", "Referer": "https://site.com"}


def test_worker_build_headers_without_referer() -> None:
    worker = CoverWorker("https://x/y.jpg", None, user_agent="UA-test")  # type: ignore[arg-type]
    assert worker.build_headers() == {"User-Agent": "UA-test"}


class _FakePool:
    """Captures the worker instead of running it (no threads, no network)."""

    def __init__(self) -> None:
        self.workers: list = []

    def start(self, worker) -> None:
        self.workers.append(worker)


def _bare_loader(source_referers: dict[str, str]):
    """CoverLoader without __init__ (avoids touching the Qt thread pool)."""
    from pathlib import Path as _Path

    loader = CoverLoader.__new__(CoverLoader)
    loader._proxy = None
    loader._ua = None
    loader._source_referers = source_referers
    loader._cache_dir = _Path("__no_such_cache_dir__")
    loader._mem_cache = {}
    loader._pending = set()
    loader._pool = _FakePool()
    return loader


def test_load_passes_source_referer_to_worker() -> None:
    """Regression: load() must forward source_id -> Referer to the worker.

    The wiring was once silently lost while resolve_referer/build_headers kept
    passing in isolation, so this asserts the end-to-end hop.
    """
    referers = {"manhuagui": "https://www.manhuagui.com"}
    loader = _bare_loader(referers)

    loader.load("https://eu1.hamreus.com/xx/1.jpg", source_id="manhuagui")

    assert len(loader._pool.workers) == 1
    worker = loader._pool.workers[0]
    assert worker.build_headers()["Referer"] == "https://www.manhuagui.com"


def test_load_without_source_id_sends_no_referer() -> None:
    loader = _bare_loader({"manhuagui": "https://www.manhuagui.com"})
    loader.load("https://cdn.example.com/a.jpg")
    worker = loader._pool.workers[0]
    assert "Referer" not in worker.build_headers()


def test_load_accepts_source_id_keyword() -> None:
    """ResultCard calls load(url, source_id=...); the signature must allow it."""
    import inspect

    params = inspect.signature(CoverLoader.load).parameters
    assert "source_id" in params


@pytest.fixture(scope="module")
def qt_app():
    """A real QApplication, so CoverLoader's signals are live.

    `_bare_loader` bypasses `__init__`, which leaves the QObject uninitialised —
    emitting on it raises and the disk-hit branch swallows that in its broad
    `except Exception`, silently falling through to a network fetch. That is
    exactly the behaviour under test here, so this one needs a real object.
    """
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_disk_layout_agrees_with_the_cache_module() -> None:
    """CoverLoader must look exactly where app.core.cache puts things."""
    from app.core.cache import cache_file, cache_key

    loader = _bare_loader({})
    url = "https://cdn.example.com/a.jpg"

    assert loader._get_hash(url) == cache_key(url)
    assert loader._cache_dir / f"{loader._get_hash(url)}.jpg" == cache_file(
        url, directory=loader._cache_dir
    )


def test_load_serves_a_disk_cached_cover_without_a_worker(tmp_path, qt_app) -> None:
    """A cover already on disk is served immediately — no worker, no network."""
    from app.core.cache import cache_file

    loader = CoverLoader(source_referers={})
    loader._cache_dir = tmp_path
    loader._pool = _FakePool()
    url = "https://cdn.example.com/cached.jpg"
    cache_file(url, directory=tmp_path).write_bytes(b"cover-bytes")

    seen: list[tuple[str, bytes]] = []
    loader.loaded.connect(lambda u, d: seen.append((u, d)))

    loader.load(url)

    assert seen == [(url, b"cover-bytes")]
    assert loader._pool.workers == [], "a disk hit must not start a fetch"
    assert loader._mem_cache[url] == b"cover-bytes"
    assert url not in loader._pending


def test_load_misses_the_disk_cache_and_starts_a_worker(tmp_path) -> None:
    loader = _bare_loader({})
    loader._cache_dir = tmp_path

    loader.load("https://cdn.example.com/not-cached.jpg")

    assert len(loader._pool.workers) == 1

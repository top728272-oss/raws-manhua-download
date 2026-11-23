"""Reader window end to end: pages are fetched as they scroll into view.

The behaviour under test is the one the feature exists for - open a chapter and
see its pages *without* pulling the whole chapter down, then scroll to pull
more. A real QApplication (offscreen) and a real AsyncBridge are used, so the
lazy-load path (scroll -> request -> fetch -> decode -> relayout) is exercised
exactly as it runs in the app.
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.fetcher import BaseFetcher, FetchError  # noqa: E402
from app.core.models import Chapter, Page  # noqa: E402
from app.gui.async_bridge import AsyncBridge  # noqa: E402
from app.gui.reader_window import ReaderWindow  # noqa: E402

PAGE_W, PAGE_H = 800, 1200
PAGE_COUNT = 40


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def page_png() -> bytes:
    pixmap = QPixmap(PAGE_W, PAGE_H)
    pixmap.fill(QColor("#334455"))
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    pixmap.save(buffer, "PNG")
    return bytes(buffer.data())


@pytest.fixture
def bridge():
    bridge = AsyncBridge()
    yield bridge
    bridge.stop()


class FakeFetcher(BaseFetcher):
    def __init__(self, payload: bytes, failing: set[str] | None = None) -> None:
        self.payload = payload
        self.failing = failing or set()
        self.requests: list[str] = []

    async def get_bytes(self, url: str, **kwargs) -> bytes:
        self.requests.append(url)
        if url in self.failing:
            raise FetchError(f"403 from {url}")
        return self.payload

    def page_numbers(self) -> list[int]:
        return [int(url.rsplit("/", 1)[1].split(".")[0]) for url in self.requests]


class FakePlugin:
    """Only the two attributes the reader touches."""

    def __init__(self, fetcher: FakeFetcher, count: int = PAGE_COUNT) -> None:
        self.fetcher = fetcher
        self.pages = [
            Page(index=i, image_url=f"https://img.example/{i + 1}.png", referer="https://s/")
            for i in range(count)
        ]
        self.resolve_calls = 0

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        self.resolve_calls += 1
        return list(self.pages)


def _chapter() -> Chapter:
    return Chapter(source_id="src", manga_ref="ref", title="Ch 1", ref="c1", index=0)


def _wait_until(app: QApplication, predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


def _open(app: QApplication, bridge: AsyncBridge, plugin: FakePlugin) -> ReaderWindow:
    reader = ReaderWindow(bridge)
    reader.resize(900, 700)
    reader.show()
    app.processEvents()
    reader.open_chapter(plugin, "Series", _chapter())
    return reader


def test_only_the_pages_near_the_viewport_are_fetched(qt_app, bridge, page_png) -> None:
    fetcher = FakeFetcher(page_png)
    plugin = FakePlugin(fetcher)
    reader = _open(qt_app, bridge, plugin)

    assert _wait_until(qt_app, lambda: len(fetcher.requests) >= 1)
    assert _wait_until(qt_app, lambda: not reader._in_flight)

    numbers = fetcher.page_numbers()
    assert numbers[0] == 1, "the chapter opens on page 1"
    assert max(numbers) <= 8, f"only the first screen(s) may load, got {numbers}"
    assert len(numbers) < PAGE_COUNT
    assert len(reader._views) == PAGE_COUNT
    assert reader._loaded, "at least one page must have decoded"

    reader.close()


def test_scrolling_pulls_the_next_pages(qt_app, bridge, page_png) -> None:
    fetcher = FakeFetcher(page_png)
    plugin = FakePlugin(fetcher)
    reader = _open(qt_app, bridge, plugin)
    assert _wait_until(qt_app, lambda: len(fetcher.requests) >= 1)
    assert _wait_until(qt_app, lambda: not reader._in_flight)
    first_screen = max(fetcher.page_numbers())

    bar = reader.scroll.verticalScrollBar()
    assert bar.maximum() > 0, "the chapter must be taller than the viewport"
    bar.setValue(bar.maximum())

    assert _wait_until(qt_app, lambda: max(fetcher.page_numbers()) > first_screen)
    assert _wait_until(qt_app, lambda: not reader._in_flight)
    assert max(fetcher.page_numbers()) > 20, "scrolling to the end reaches the last pages"
    assert reader.statusBar().currentMessage().startswith(f"Page {PAGE_COUNT}/")

    reader.close()


def test_caption_reports_the_real_pixel_size_and_weight(qt_app, bridge, page_png) -> None:
    fetcher = FakeFetcher(page_png)
    reader = _open(qt_app, bridge, FakePlugin(fetcher))
    assert _wait_until(qt_app, lambda: bool(reader._loaded))

    caption = reader._views[0].caption.text()
    assert f"{PAGE_W}×{PAGE_H}" in caption
    assert "KB" in caption
    assert f"{PAGE_W}×{PAGE_H}" in reader.summary_lbl.text()

    reader.close()


def test_a_page_that_fails_is_marked_and_can_be_retried(qt_app, bridge, page_png) -> None:
    fetcher = FakeFetcher(page_png, failing={"https://img.example/1.png"})
    reader = _open(qt_app, bridge, FakePlugin(fetcher))

    assert _wait_until(qt_app, lambda: 0 in reader._failed)
    assert reader._views[0].failed
    assert "could not load" in reader._views[0].image.text()
    assert reader.retry_btn.isEnabled(), "a failed page must offer a retry"

    fetcher.failing.clear()
    reader._retry_failed()

    assert _wait_until(qt_app, lambda: 0 in reader._loaded)
    assert not reader._views[0].failed
    assert not reader.retry_btn.isEnabled()
    assert reader._views[0].image.pixmap().isNull() is False

    reader.close()


def test_opening_a_second_chapter_discards_the_first(qt_app, bridge, page_png) -> None:
    fetcher = FakeFetcher(page_png)
    plugin = FakePlugin(fetcher)
    reader = _open(qt_app, bridge, plugin)
    assert _wait_until(qt_app, lambda: bool(reader._loaded))

    other = FakePlugin(FakeFetcher(page_png), count=3)
    reader.open_chapter(other, "Series", _chapter())
    assert _wait_until(qt_app, lambda: len(reader._views) == 3)
    assert _wait_until(qt_app, lambda: bool(reader._loaded))
    assert len(reader._views) == 3
    assert reader._generation >= 2

    reader.close()

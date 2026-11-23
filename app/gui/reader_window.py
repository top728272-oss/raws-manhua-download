"""Chapter reader: a scroll view that loads pages as they come into view.

The point is to judge a source's image quality *before* committing to a
download, so the bytes come from exactly the same path a download uses - the
plugin's own fetcher, with the page's Referer - which means ComicBox chunk
decryption, signed URLs and hotlink rules all behave identically.

Only the pages inside the viewport (plus one screen of preload) are fetched.
Pixmaps of pages that scrolled far away are dropped and re-fetched on the way
back, so a 200-page chapter cannot exhaust memory.
"""

from __future__ import annotations

import logging
from bisect import bisect_right

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from app.core.models import Chapter, Page
from app.gui.async_bridge import AsyncBridge
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

CONTENT_MARGIN = 8
PAGE_SPACING = 8
CAPTION_HEIGHT = 18

DEFAULT_ASPECT = 1.45
MAX_IN_FLIGHT = 4

MAX_RESIDENT_PAGES = 24


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class _PageView(QFrame):
    """One page: the image, plus a caption with its real pixel size and weight."""

    def __init__(self, index: int, page: Page) -> None:
        super().__init__()
        self.index = index
        self.page = page
        self.ratio = DEFAULT_ASPECT
        self.loaded = False
        self.failed = False
        self.width_px = 0

        self.setObjectName("ReaderPage")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.image = QLabel()
        self.image.setObjectName("ReaderPageImage")
        self.image.setAlignment(Qt.AlignCenter)

        self.caption = QLabel()
        self.caption.setObjectName("ReaderPageCaption")
        self.caption.setAlignment(Qt.AlignCenter)
        self.caption.setFixedHeight(CAPTION_HEIGHT)

        layout.addWidget(self.image)
        layout.addWidget(self.caption)

        self.show_state("queued")

    def set_width(self, width: int) -> None:
        self.width_px = width
        self.setFixedWidth(width)
        if not self.loaded:
            self._set_image_height(int(width * self.ratio))

    def _set_image_height(self, height: int) -> None:
        self.image.setFixedHeight(height)
        self.setFixedHeight(height + CAPTION_HEIGHT)

    def show_state(self, text: str) -> None:
        self.loaded = False
        self.image.setPixmap(QPixmap())
        self.image.setText(text)
        self.caption.setText(f"Page {self.index + 1}")
        if self.width_px:
            self._set_image_height(int(self.width_px * self.ratio))

    def clear_image(self) -> None:
        """Drop the pixmap but keep the layout height, so scrolling back is seamless."""
        self.show_state("scroll to load")

    def show_error(self, message: str) -> None:
        self.failed = True
        self.show_state(f"could not load this page\n{message}")

    def show_image(self, pixmap: QPixmap, byte_count: int) -> None:
        self.failed = False
        self.loaded = True
        if pixmap.width() > 0:
            self.ratio = pixmap.height() / pixmap.width()
        scaled = pixmap.scaledToWidth(max(self.width_px, 1), Qt.SmoothTransformation)
        self.image.setText("")
        self.image.setPixmap(scaled)
        self._set_image_height(scaled.height())
        self.caption.setText(
            f"Page {self.index + 1} · {pixmap.width()}×{pixmap.height()} · "
            f"{byte_count / 1024:,.0f} KB"
        )


class ReaderWindow(QMainWindow):
    """Standalone, non-modal chapter preview window."""

    _resolve_done = Signal(int, object)
    _resolve_failed = Signal(int, str)
    _page_loaded = Signal(int, int, bytes)
    _page_failed = Signal(int, int, str)

    def __init__(self, bridge: AsyncBridge) -> None:
        super().__init__()
        self.setObjectName("ReaderWindow")
        self.setWindowTitle("Chapter preview")
        self.resize(940, 900)

        self._bridge = bridge
        self._generation = 0
        self._plugin: SourcePlugin | None = None
        self._manga_title = ""
        self._chapter: Chapter | None = None
        self._pages: list[Page] = []
        self._views: list[_PageView] = []
        self._offsets: list[int] = []
        self._in_flight: set[int] = set()
        self._loaded: set[int] = set()
        self._failed: dict[int, str] = {}
        self._stats: dict[int, tuple[int, int, int]] = {}
        self._current_page = 0
        self._zoom = 1.0
        self._last_width = 0
        self._ratio_hint = DEFAULT_ASPECT

        self._build_ui()

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(150)
        self._resize_timer.timeout.connect(self._on_resize_settled)

        self._resolve_done.connect(self._on_pages_resolved)
        self._resolve_failed.connect(self._on_resolve_failed)
        self._page_loaded.connect(self._on_page_loaded)
        self._page_failed.connect(self._on_page_failed)

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("ReaderCentral")
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("ReaderHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 8, 12, 8)
        header_layout.setSpacing(10)

        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title_lbl = QLabel("")
        self.title_lbl.setObjectName("ReaderTitle")
        self.summary_lbl = QLabel("")
        self.summary_lbl.setObjectName("ReaderSummary")
        titles.addWidget(self.title_lbl)
        titles.addWidget(self.summary_lbl)
        header_layout.addLayout(titles, stretch=1)

        self.quality_lbl = QLabel("")
        self.quality_lbl.setObjectName("ReaderQuality")
        self.quality_lbl.setProperty("quality", "none")
        header_layout.addWidget(self.quality_lbl)

        self.zoom_picker = QComboBox()
        self.zoom_picker.setObjectName("ReaderZoom")
        for label, factor in (("Fit width", 1.0), ("Zoom 1.5x", 1.5), ("Zoom 2x", 2.0)):
            self.zoom_picker.addItem(label, factor)
        self.zoom_picker.currentIndexChanged.connect(self._on_zoom_changed)
        header_layout.addWidget(self.zoom_picker)

        self.retry_btn = QPushButton("Retry failed")
        self.retry_btn.setObjectName("ReaderRetry")
        self.retry_btn.clicked.connect(self._retry_failed)
        self.retry_btn.setEnabled(False)
        header_layout.addWidget(self.retry_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        header_layout.addWidget(close_btn)

        root.addWidget(header)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("ReaderScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.scroll.verticalScrollBar().valueChanged.connect(self._update_visible)

        self._content = QWidget()
        self._content.setObjectName("ReaderContent")
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(
            CONTENT_MARGIN, CONTENT_MARGIN, CONTENT_MARGIN, CONTENT_MARGIN
        )
        self._layout.setSpacing(PAGE_SPACING)
        self._layout.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self.scroll.setWidget(self._content)
        root.addWidget(self.scroll, stretch=1)

        self.setCentralWidget(central)
        self.statusBar().showMessage("No chapter loaded")

    def open_chapter(self, plugin: SourcePlugin, manga_title: str, chapter: Chapter) -> None:
        self._generation += 1
        generation = self._generation
        self._plugin = plugin
        self._manga_title = manga_title
        self._chapter = chapter
        self._pages = []
        self._stats.clear()
        self._clear_views()

        heading = f"{manga_title} - {chapter.title}"
        self.setWindowTitle(f"Preview: {heading}")
        self.title_lbl.setText(heading)
        self.summary_lbl.setText("Resolving pages…")
        self.quality_lbl.setText("")
        self.quality_lbl.setProperty("quality", "none")
        _repolish(self.quality_lbl)
        self.retry_btn.setEnabled(False)
        self.statusBar().showMessage("Resolving page list…")

        self.show()
        self.raise_()
        self.activateWindow()

        async def resolve() -> None:
            try:
                pages = await plugin.get_pages(chapter)
            except Exception as exc:
                logger.warning("Reader: page resolution failed for %s: %s", chapter.title, exc)
                self._resolve_failed.emit(generation, str(exc))
                return
            self._resolve_done.emit(generation, list(pages))

        self._bridge.submit(resolve())

    def _on_pages_resolved(self, generation: int, pages: list[Page]) -> None:
        if not isValid(self) or generation != self._generation:
            return
        self._pages = pages
        if not pages:
            self.summary_lbl.setText("This chapter has no pages.")
            self.statusBar().showMessage("No pages")
            return
        self._build_views()
        self.summary_lbl.setText(f"{len(pages)} pages")
        QTimer.singleShot(0, self._update_visible)

    def _on_resolve_failed(self, generation: int, message: str) -> None:
        if not isValid(self) or generation != self._generation:
            return
        self.summary_lbl.setText(f"Could not resolve this chapter: {message}")
        self.statusBar().showMessage("Page list failed")

    def _build_views(self) -> None:
        self._clear_views()
        width = self._target_width()
        self._last_width = width
        for i, page in enumerate(self._pages):
            view = _PageView(i, page)
            view.set_width(width)
            self._layout.addWidget(view, 0, Qt.AlignHCenter)
            self._views.append(view)
        self._content.setMinimumWidth(width + 2 * CONTENT_MARGIN)
        self._recompute_offsets()
        self.scroll.verticalScrollBar().setValue(0)

    def _clear_views(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._views.clear()
        self._offsets.clear()
        self._in_flight.clear()
        self._loaded.clear()
        self._failed.clear()
        self._current_page = 0
        self._content.setMinimumHeight(0)

    def _recompute_offsets(self) -> None:
        offsets: list[int] = []
        y = CONTENT_MARGIN
        for view in self._views:
            offsets.append(y)
            y += view.height() + PAGE_SPACING
        self._offsets = offsets
        self._content.setMinimumHeight(y if self._views else 0)

    def _index_at(self, y: int) -> int:
        if not self._offsets:
            return 0
        index = bisect_right(self._offsets, y) - 1
        return max(0, min(index, len(self._offsets) - 1))

    def _target_width(self) -> int:

        available = self.scroll.width() or self.width()
        bar_width = self.scroll.verticalScrollBar().sizeHint().width()
        fit = max(200, available - 2 * CONTENT_MARGIN - bar_width)
        return max(200, int(fit * self._zoom))

    def _update_visible(self) -> None:
        if not self._views or not self._offsets:
            return
        top = self.scroll.verticalScrollBar().value()
        height = self.scroll.viewport().height() or self.height()

        page_height = int(self._target_width() * self._ratio_hint)
        margin = max(height, page_height)
        first = self._index_at(max(0, top - margin))
        last = self._index_at(top + height + margin)

        for index in range(first, last + 1):
            if index in self._loaded or index in self._in_flight or index in self._failed:
                continue
            if len(self._in_flight) >= MAX_IN_FLIGHT:
                break
            self._request(index)

        self._current_page = self._index_at(top + 12)
        self._refresh_status()
        self._evict_outside(first, last)

    def _request(self, index: int) -> None:
        if self._plugin is None or index >= len(self._pages):
            return
        page = self._pages[index]
        view = self._views[index]
        view.show_state(f"loading page {index + 1}…")
        self._in_flight.add(index)

        generation = self._generation
        fetcher = self._plugin.fetcher
        headers = {"Referer": page.referer} if page.referer else None

        async def fetch() -> None:
            try:
                data = await fetcher.get_bytes(page.image_url, headers=headers)
            except Exception as exc:
                logger.warning("Reader: page %d failed: %s", index + 1, exc)
                self._page_failed.emit(generation, index, str(exc))
                return
            self._page_loaded.emit(generation, index, data)

        self._bridge.submit(fetch())

    def _on_page_loaded(self, generation: int, index: int, data: bytes) -> None:
        if not isValid(self) or generation != self._generation or index >= len(self._views):
            return
        self._in_flight.discard(index)
        view = self._views[index]
        pixmap = QPixmap()
        if not pixmap.loadFromData(data) or pixmap.isNull():
            self._failed[index] = "not a decodable image"
            view.show_error("the downloaded bytes are not an image")
            self.retry_btn.setEnabled(True)
            self._recompute_offsets()
            self._update_visible()
            return

        self._loaded.add(index)
        self._stats[index] = (pixmap.width(), pixmap.height(), len(data))
        view.show_image(pixmap, len(data))
        self._adopt_ratio(view.ratio)
        self._recompute_offsets()
        self._update_summary()
        self._update_visible()

    def _on_page_failed(self, generation: int, index: int, message: str) -> None:
        if not isValid(self) or generation != self._generation or index >= len(self._views):
            return
        self._in_flight.discard(index)
        self._failed[index] = message
        self._views[index].show_error(message)
        self.retry_btn.setEnabled(True)
        self._recompute_offsets()
        self._update_visible()

    def _adopt_ratio(self, ratio: float) -> None:
        """Give every not-yet-loaded page the aspect ratio the source really uses."""
        if not 0.3 <= ratio <= 5.0:
            return
        self._ratio_hint = ratio
        for view in self._views:
            if view.loaded or view.failed:
                continue
            view.ratio = ratio
            view.set_width(view.width_px)

    def _evict_outside(self, first: int, last: int) -> None:
        if len(self._loaded) <= MAX_RESIDENT_PAGES:
            return
        outside = sorted(
            (i for i in self._loaded if i < first or i > last),
            key=lambda i: min(abs(i - first), abs(i - last)),
            reverse=True,
        )
        for index in outside:
            if len(self._loaded) <= MAX_RESIDENT_PAGES:
                break
            self._views[index].clear_image()
            self._loaded.discard(index)

    def _update_summary(self) -> None:
        seen = len(self._stats)
        if not seen:
            return
        avg_w = sum(w for w, _h, _b in self._stats.values()) // seen
        avg_h = sum(h for _w, h, _b in self._stats.values()) // seen
        total_bytes = sum(b for _w, _h, b in self._stats.values())
        weight = (
            f"{total_bytes / 1_000_000:.1f} MB"
            if total_bytes >= 1_000_000
            else f"{total_bytes / 1024:.0f} KB"
        )
        self.summary_lbl.setText(
            f"{len(self._pages)} pages · avg {avg_w}×{avg_h} · "
            f"{weight} · {seen}/{len(self._pages)} checked"
        )

        if avg_w >= 1000:
            label, quality = "high resolution", "high"
        elif avg_w >= 720:
            label, quality = "standard resolution", "ok"
        else:
            label, quality = "low resolution", "low"
        self.quality_lbl.setText(label)
        if self.quality_lbl.property("quality") != quality:
            self.quality_lbl.setProperty("quality", quality)
            _repolish(self.quality_lbl)

    def _refresh_status(self) -> None:
        total = len(self._pages)
        if not total:
            return
        self.statusBar().showMessage(
            f"Page {self._current_page + 1}/{total} · "
            f"{len(self._loaded)} loaded · {len(self._failed)} failed"
        )

    def _retry_failed(self) -> None:
        if not self._failed:
            return
        for index in list(self._failed):
            self._failed.pop(index, None)
            if index < len(self._views):
                self._views[index].failed = False
                self._views[index].show_state("queued")
        self.retry_btn.setEnabled(bool(self._failed))
        self._recompute_offsets()
        self._update_visible()

    def _on_zoom_changed(self) -> None:
        self._zoom = float(self.zoom_picker.currentData() or 1.0)
        self._last_width = 0
        self._relayout_pages()

    def _relayout_pages(self) -> None:
        if not self._views:
            return
        width = self._target_width()
        if width == self._last_width:
            return
        self._last_width = width
        for index in list(self._loaded):
            self._views[index].clear_image()
        self._loaded.clear()
        for view in self._views:
            view.set_width(width)
        self._content.setMinimumWidth(width + 2 * CONTENT_MARGIN)
        self._recompute_offsets()
        self._update_visible()

    def _on_resize_settled(self) -> None:
        self._relayout_pages()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._views:
            self._resize_timer.start()

    def showEvent(self, event) -> None:
        super().showEvent(event)

        if self._views:
            self._resize_timer.start()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        self._resize_timer.stop()
        super().closeEvent(event)

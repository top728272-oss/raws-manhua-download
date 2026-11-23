"""Cover-art results grid.

Displays SearchResult items as a responsive 3-column grid of cards (cover image + title).
Clicking a card emits `result_selected`. Disk-cached async cover loading is supported.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from app.core.models import SearchResult
from app.gui.cover_loader import CoverLoader

COLUMNS = 3


class ResultCard(QFrame):
    clicked = Signal(object)

    def __init__(self, result: SearchResult, cover_loader: CoverLoader | None = None) -> None:
        super().__init__()
        self.result = result
        self._cover_loader = cover_loader
        self.setObjectName("ResultCard")
        self.setFixedSize(165, 260)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"{result.title} ({result.source_id})")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        self.cover = QLabel()
        self.cover.setObjectName("CoverImage")
        self.cover.setAlignment(Qt.AlignCenter)
        self.cover.setFixedSize(149, 170)
        self.cover.setText("")

        self.title = QLabel(result.title)
        self.title.setObjectName("CardTitle")
        self.title.setWordWrap(True)
        self.title.setAlignment(Qt.AlignCenter)

        self.source_badge = QLabel(result.source_id.upper())
        self.source_badge.setObjectName("CardSourceBadge")
        self.source_badge.setAlignment(Qt.AlignCenter)

        layout.addWidget(self.cover)
        layout.addWidget(self.title, stretch=1)
        layout.addWidget(self.source_badge)

        if self._cover_loader:
            if result.cover_url:
                self._cover_loader.loaded.connect(self._on_cover_loaded)
                self._cover_loader.failed.connect(self._on_cover_failed)
                self.destroyed.connect(self._disconnect_cover)
                self._cover_loader.load(result.cover_url, source_id=result.source_id)
            else:
                self._set_cover_placeholder()

    def _set_cover_placeholder(self) -> None:
        if not isValid(self.cover):
            return
        self.cover.setPixmap(QPixmap())
        self.cover.setText("🖼")

    def _disconnect_cover(self) -> None:
        if self._cover_loader:
            try:
                self._cover_loader.loaded.disconnect(self._on_cover_loaded)
            except (RuntimeError, TypeError):
                pass
            try:
                self._cover_loader.failed.disconnect(self._on_cover_failed)
            except (RuntimeError, TypeError):
                pass

    def _on_cover_failed(self, url: str) -> None:
        if not isValid(self) or not isValid(self.cover):
            return
        if not self.result.cover_url or self.result.cover_url != url:
            return
        self._set_cover_placeholder()

    def _on_cover_loaded(self, url: str, data: bytes) -> None:
        if not isValid(self) or not isValid(self.cover):
            return
        if not self.result.cover_url or self.result.cover_url != url:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            scaled = pixmap.scaled(149, 170, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            w_offset = max(0, (scaled.width() - 149) // 2)
            h_offset = max(0, (scaled.height() - 170) // 2)
            cropped = scaled.copy(w_offset, h_offset, 149, 170)
            self.cover.setPixmap(cropped)
            self.cover.setText("")

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.result)
        super().mouseReleaseEvent(event)


class ResultsGrid(QScrollArea):
    result_selected = Signal(object)

    def __init__(self, cover_loader: CoverLoader | None = None) -> None:
        super().__init__()
        self._cover_loader = cover_loader or CoverLoader()
        self.setObjectName("ResultsGrid")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self._container = QWidget()
        self._container.setObjectName("ResultsList")
        self._grid = QGridLayout(self._container)
        self._grid.setContentsMargins(6, 6, 6, 6)
        self._grid.setSpacing(10)
        self.setWidget(self._container)

        self.set_results([])

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.angleDelta().x() != 0 and event.angleDelta().y() == 0:
            event.ignore()
            return
        super().wheelEvent(event)

    def _build_placeholder(self, title: str, sub: str) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(8)

        icon_lbl = QLabel("🔍")
        icon_lbl.setObjectName("PlaceholderIcon")
        icon_lbl.setAlignment(Qt.AlignCenter)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("PlaceholderTitle")
        title_lbl.setAlignment(Qt.AlignCenter)

        sub_lbl = QLabel(sub)
        sub_lbl.setObjectName("PlaceholderSub")
        sub_lbl.setAlignment(Qt.AlignCenter)

        layout.addWidget(icon_lbl)
        layout.addWidget(title_lbl)
        layout.addWidget(sub_lbl)
        return w

    def set_results(self, results: list[SearchResult]) -> None:
        self._clear_grid()
        if not results:
            self._grid.setRowStretch(0, 1)
            placeholder = self._build_placeholder(
                "Start Your Search", "Select a source and enter keywords above to discover manhua."
            )
            self._grid.addWidget(placeholder, 1, 0, 1, COLUMNS, Qt.AlignCenter)
            self._grid.setRowStretch(2, 1)
            return

        self._grid.setRowStretch(0, 0)
        self._grid.setRowStretch(2, 0)
        display_results = results[:60]
        for i, result in enumerate(display_results):
            card = ResultCard(result, cover_loader=self._cover_loader)
            card.clicked.connect(self.result_selected.emit)
            self._grid.addWidget(card, i // COLUMNS, i % COLUMNS)

    def _clear_grid(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

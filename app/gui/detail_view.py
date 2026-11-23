"""Detail view: series metadata + chapter selection + output-format picker.

Chapter selection supports all / range / individual. Emits
`download_requested(selected_chapters, formats)` and `preview_requested(chapter)`
for the scroll reader.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from app.core.models import Chapter, MangaDetail, OutputFormat
from app.gui.cover_loader import CoverLoader


class DetailView(QWidget):
    download_requested = Signal(list, list)
    preview_requested = Signal(object)

    def __init__(self, default_formats: list[str], cover_loader: CoverLoader | None = None) -> None:
        super().__init__()
        self.setObjectName("DetailView")
        self._chapters: list[Chapter] = []
        self._cover_loader = cover_loader or CoverLoader()
        self._current_cover_url: str | None = None

        self._stack = QStackedLayout(self)

        self._empty_widget = self._build_empty_widget()
        self._content_widget = self._build_content_widget(default_formats)

        self._stack.addWidget(self._empty_widget)
        self._stack.addWidget(self._content_widget)
        self._stack.setCurrentWidget(self._empty_widget)

        self._cover_loader.loaded.connect(self._on_cover_loaded)

    def _build_empty_widget(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(8)

        icon = QLabel("📖")
        icon.setObjectName("PlaceholderIcon")
        icon.setAlignment(Qt.AlignCenter)

        title = QLabel("No Manhua Selected")
        title.setObjectName("PlaceholderTitle")
        title.setAlignment(Qt.AlignCenter)

        sub = QLabel(
            "Click any manhua from the search results\nto view details and download chapters."
        )
        sub.setObjectName("PlaceholderSub")
        sub.setAlignment(Qt.AlignCenter)

        layout.addWidget(icon)
        layout.addWidget(title)
        layout.addWidget(sub)
        return widget

    def _build_content_widget(self, default_formats: list[str]) -> QWidget:
        widget = QWidget()
        root = QVBoxLayout(widget)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(14)

        self.cover = QLabel()
        self.cover.setObjectName("CoverImage")
        self.cover.setAlignment(Qt.AlignCenter)
        self.cover.setFixedSize(100, 140)
        self.cover.setText("")
        header_layout.addWidget(self.cover)

        meta_layout = QVBoxLayout()
        meta_layout.setSpacing(4)
        self.title = QLabel("")
        self.title.setObjectName("DetailTitle")
        self.title.setWordWrap(True)
        self.meta = QLabel("")
        self.meta.setObjectName("DetailMeta")
        self.meta.setWordWrap(True)
        self.description = QLabel("")
        self.description.setObjectName("DetailDesc")
        self.description.setWordWrap(True)

        meta_layout.addWidget(self.title)
        meta_layout.addWidget(self.meta)
        meta_layout.addWidget(self.description)
        meta_layout.addStretch(1)

        header_layout.addLayout(meta_layout, stretch=1)
        root.addLayout(header_layout)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.select_all = QCheckBox("Select all")
        self.select_all.stateChanged.connect(self._on_select_all)

        self.range_from = QSpinBox()
        self.range_to = QSpinBox()
        for sb in (self.range_from, self.range_to):
            sb.setMinimum(1)
            sb.setMaximum(1)
        apply_range = QPushButton("Select range")
        apply_range.clicked.connect(self._on_apply_range)

        controls.addWidget(self.select_all)
        controls.addStretch(1)
        controls.addWidget(QLabel("From"))
        controls.addWidget(self.range_from)
        controls.addWidget(QLabel("To"))
        controls.addWidget(self.range_to)
        controls.addWidget(apply_range)
        root.addLayout(controls)

        self.chapter_list = QListWidget()
        self.chapter_list.setObjectName("ChapterList")
        self.chapter_list.setToolTip(
            "Double-click a chapter to preview its pages before downloading"
        )
        self.chapter_list.itemDoubleClicked.connect(self._on_chapter_activated)
        root.addWidget(self.chapter_list, stretch=1)

        fmt_row = QHBoxLayout()
        fmt_row.setSpacing(10)
        self._fmt_boxes: dict[OutputFormat, QCheckBox] = {}
        for fmt in (OutputFormat.FOLDER, OutputFormat.CBZ):
            box = QCheckBox(fmt.value.upper())
            box.setChecked(fmt.value in default_formats)
            self._fmt_boxes[fmt] = box
            fmt_row.addWidget(box)
        fmt_row.addStretch(1)
        self.preview_btn = QPushButton("Preview")
        self.preview_btn.setToolTip("Open the highlighted chapter in the scroll reader")
        self.preview_btn.clicked.connect(self._on_preview)
        fmt_row.addWidget(self.preview_btn)
        self.download_btn = QPushButton("Download selected")
        self.download_btn.setObjectName("PrimaryButton")
        self.download_btn.clicked.connect(self._on_download)
        fmt_row.addWidget(self.download_btn)
        root.addLayout(fmt_row)

        return widget

    def show_detail(self, detail: MangaDetail, chapters: list[Chapter]) -> None:
        self._stack.setCurrentWidget(self._content_widget)
        self._chapters = chapters
        self.title.setText(detail.title)
        bits = [b for b in (detail.author, detail.status) if b]
        if detail.tags:
            bits.append(" · ".join(detail.tags[:6]))
        self.meta.setText("  |  ".join(bits))
        self.description.setText(detail.description or "")

        self._current_cover_url = detail.cover_url
        self.cover.setPixmap(QPixmap())
        self.cover.setText("🖼" if not detail.cover_url else "")
        if detail.cover_url:
            self._cover_loader.load(detail.cover_url, source_id=detail.source_id)

        self.chapter_list.clear()
        for ch in chapters:
            item = QListWidgetItem(ch.title)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.chapter_list.addItem(item)

        n = max(1, len(chapters))
        for sb in (self.range_from, self.range_to):
            sb.setMaximum(n)
        self.range_from.setValue(1)
        self.range_to.setValue(n)
        self.select_all.setChecked(False)

        self.chapter_list.setCurrentRow(0 if chapters else -1)

    def reset_view(self) -> None:
        self._stack.setCurrentWidget(self._empty_widget)

    def _on_cover_loaded(self, url: str, data: bytes) -> None:
        if not isValid(self) or not isValid(self.cover):
            return
        if not self._current_cover_url or self._current_cover_url != url:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            scaled = pixmap.scaled(100, 140, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            w_offset = max(0, (scaled.width() - 100) // 2)
            h_offset = max(0, (scaled.height() - 140) // 2)
            cropped = scaled.copy(w_offset, h_offset, 100, 140)
            self.cover.setPixmap(cropped)
            self.cover.setText("")

    def _on_select_all(self, state: int) -> None:
        check = Qt.Checked if state == Qt.Checked.value else Qt.Unchecked
        for i in range(self.chapter_list.count()):
            self.chapter_list.item(i).setCheckState(check)

    def _on_apply_range(self) -> None:
        lo = self.range_from.value() - 1
        hi = self.range_to.value() - 1
        for i in range(self.chapter_list.count()):
            check = Qt.Checked if lo <= i <= hi else Qt.Unchecked
            self.chapter_list.item(i).setCheckState(check)

    def _selected_chapters(self) -> list[Chapter]:
        out = []
        for i in range(self.chapter_list.count()):
            if self.chapter_list.item(i).checkState() == Qt.Checked:
                out.append(self._chapters[i])
        return out

    def _selected_formats(self) -> list[OutputFormat]:
        return [f for f, box in self._fmt_boxes.items() if box.isChecked()]

    def _chapter_at(self, item: QListWidgetItem) -> Chapter | None:
        row = self.chapter_list.row(item)
        if 0 <= row < len(self._chapters):
            return self._chapters[row]
        return None

    def _on_chapter_activated(self, item: QListWidgetItem) -> None:
        chapter = self._chapter_at(item)
        if chapter is not None:
            self.preview_requested.emit(chapter)

    def _on_preview(self) -> None:
        item = self.chapter_list.currentItem()
        if item is None:
            return
        chapter = self._chapter_at(item)
        if chapter is not None:
            self.preview_requested.emit(chapter)

    def _on_download(self) -> None:
        chapters = self._selected_chapters()
        formats = self._selected_formats()
        if chapters and formats:
            self.download_requested.emit(chapters, formats)

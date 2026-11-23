"""Download panel: live queue + per-chapter progress bars + run controls."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from app.core.models import DownloadTask


class _TaskRow(QFrame):
    def __init__(self, task: DownloadTask) -> None:
        super().__init__()
        self.task = task
        self.setObjectName("TaskRow")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        top = QHBoxLayout()
        self.title_lbl = QLabel(f"{task.manga_title} - {task.chapter.title}")
        self.title_lbl.setObjectName("TaskTitle")

        self.status_lbl = QLabel(task.status)
        self.status_lbl.setObjectName("TaskStatus")

        top.addWidget(self.title_lbl, stretch=1)
        top.addWidget(self.status_lbl)
        layout.addLayout(top)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        layout.addWidget(self.bar)

    def refresh(self) -> None:
        self.bar.setValue(int(self.task.progress * 100))
        status = self.task.status
        suffix = f" ({self.task.error})" if self.task.error else ""
        pct = f" {int(self.task.progress * 100)}%" if status in ("running", "downloading") else ""
        note = f" [{self.task.note}]" if self.task.note else ""
        pages = self.task.pages_label
        pages_txt = f" {pages}" if pages else ""
        self.status_lbl.setText(f"{status}{pct}{pages_txt}{note}{suffix}")


class DownloadPanel(QFrame):
    pause_toggled = Signal(bool)
    stop_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("DownloadContainer")

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(4)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(4, 0, 0, 0)
        header_row.setSpacing(6)

        self._header = QLabel("DOWNLOAD QUEUE")
        self._header.setStyleSheet(
            "font-size: 11px; font-weight: 700; color: #818CF8; padding-left: 4px;"
        )
        header_row.addWidget(self._header)
        header_row.addStretch(1)

        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setObjectName("PanelButton")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setToolTip("Hold the download; pages already in flight still finish")
        self.pause_btn.toggled.connect(self._on_pause_toggled)
        header_row.addWidget(self.pause_btn)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("DangerButton")
        self.stop_btn.setToolTip("Abort the run; chapters not written yet are left alone")
        self.stop_btn.clicked.connect(self.stop_requested.emit)
        header_row.addWidget(self.stop_btn)

        root.addLayout(header_row)

        scroll = QScrollArea()
        scroll.setObjectName("DownloadPanel")
        scroll.setWidgetResizable(True)

        self._container = QWidget()
        self._container.setObjectName("DownloadList")
        self._stack = QStackedLayout(self._container)

        self._empty = QLabel("No active downloads in queue.")
        self._empty.setObjectName("PlaceholderSub")
        self._empty.setAlignment(Qt.AlignCenter)

        self._tasks_widget = QWidget()
        self._layout = QVBoxLayout(self._tasks_widget)
        self._layout.setAlignment(Qt.AlignTop)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(6)

        self._stack.addWidget(self._empty)
        self._stack.addWidget(self._tasks_widget)
        self._stack.setCurrentWidget(self._empty)

        scroll.setWidget(self._container)
        root.addWidget(scroll)

        self._rows: dict[int, _TaskRow] = {}
        self.set_active(False)

    def _on_pause_toggled(self, paused: bool) -> None:
        self.pause_btn.setText("Resume" if paused else "Pause")
        self._header.setText("DOWNLOAD QUEUE - PAUSED" if paused else "DOWNLOAD QUEUE")
        self.pause_toggled.emit(paused)

    def set_active(self, active: bool) -> None:
        """Enable the run controls only while an engine is actually running."""
        self.pause_btn.setEnabled(active)
        self.stop_btn.setEnabled(active)
        if not active:
            if self.pause_btn.isChecked():
                self.pause_btn.setChecked(False)
            self._header.setText("DOWNLOAD QUEUE")

    def set_stopping(self) -> None:
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self._header.setText("STOPPING…")

    def mark_unstarted_stopped(self) -> None:
        """Queued rows will never run after a stop - say so instead of 'queued'."""
        for row in self._rows.values():
            if row.task.status == "queued":
                row.task.status = "stopped"
                row.refresh()

    def add_tasks(self, tasks: list[DownloadTask]) -> None:
        self._stack.setCurrentWidget(self._tasks_widget)
        for task in tasks:
            row = _TaskRow(task)
            self._rows[id(task)] = row
            self._layout.addWidget(row)

    def update_task(self, task: DownloadTask) -> None:
        row = self._rows.get(id(task))
        if row:
            row.refresh()

    def clear(self) -> None:
        for row in self._rows.values():
            row.deleteLater()
        self._rows.clear()
        self._stack.setCurrentWidget(self._empty)

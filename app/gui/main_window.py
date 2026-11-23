"""Main window - the app shell that wires everything together.

Async work (search / detail / download) runs on a background asyncio loop in a
dedicated thread; results are marshaled back to the Qt thread via signals.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app import __app_name__
from app.config import AppConfig
from app.core import cache
from app.core.downloader import DownloadControl, DownloadEngine, RunSummary
from app.core.fallback import SourceFallback
from app.core.models import Chapter, DownloadTask, MangaDetail, OutputFormat, SearchResult
from app.core.output import OutputWriter
from app.core.registry import PluginRegistry
from app.gui.async_bridge import AsyncBridge
from app.gui.cover_loader import CoverLoader
from app.gui.detail_view import DetailView
from app.gui.download_panel import DownloadPanel
from app.gui.reader_window import ReaderWindow
from app.gui.results_grid import ResultsGrid

logger = logging.getLogger(__name__)


class MainBridge(AsyncBridge):
    search_done = Signal(object)
    detail_done = Signal(object, object)
    task_progress = Signal(object)
    run_done = Signal(object)
    run_finished = Signal()
    error = Signal(str)


class CacheClearSignals(QObject):
    done = Signal(int, int)


class CacheClearWorker(QRunnable):
    """Wipes the cover cache off the UI thread.

    A cold cache can hold millions of entries, so deleting it inline would freeze
    the window for minutes.
    """

    def __init__(self, signals: CacheClearSignals) -> None:
        super().__init__()
        self._signals = signals

    def run(self) -> None:
        try:
            result = cache.clear()
        except Exception as exc:
            logger.warning("Cache clear failed: %s", exc)
            self._signals.done.emit(0, 0)
            return
        self._signals.done.emit(result.files_removed, result.bytes_freed)


class MainWindow(QMainWindow):
    def __init__(self, cfg: AppConfig, registry: PluginRegistry) -> None:
        super().__init__()
        self._cfg = cfg
        self._registry = registry
        self._detail: MangaDetail | None = None
        self._reader: ReaderWindow | None = None
        self._active_control: DownloadControl | None = None

        self.setWindowTitle(__app_name__)
        self.resize(1080, 640)

        self._bridge = MainBridge()
        self._bridge.search_done.connect(self._on_search_done)
        self._bridge.detail_done.connect(self._on_detail_done)
        self._bridge.task_progress.connect(self._on_task_progress)
        self._bridge.run_done.connect(self._on_run_done)
        self._bridge.run_finished.connect(self._on_run_finished)
        self._bridge.error.connect(self._on_error)

        self._build_ui()

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("CentralWidget")
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(6, 4, 6, 4)
        top_layout.setSpacing(8)

        self.category_picker = QComboBox()
        self.category_picker.setObjectName("CategoryPicker")
        self.category_picker.addItem("All Categories", "All")
        self.category_picker.addItem("General", "General")
        self.category_picker.addItem("18+", "18+")
        self.category_picker.currentIndexChanged.connect(self._on_category_changed)

        self.source_picker = QComboBox()
        self.source_picker.setObjectName("SourcePicker")
        self._populate_sources()

        self.search_bar = QLineEdit()
        self.search_bar.setObjectName("SearchBar")
        self.search_bar.setPlaceholderText("Search manhua by title...")
        self.search_bar.returnPressed.connect(self._on_search)
        self.search_bar.textChanged.connect(self._on_search_text_changed)

        search_btn = QPushButton("Search")
        search_btn.setObjectName("PrimaryButton")
        search_btn.clicked.connect(self._on_search)

        top_layout.addWidget(self.category_picker)
        top_layout.addWidget(self.source_picker)
        top_layout.addWidget(self.search_bar, stretch=1)
        top_layout.addWidget(search_btn)
        root.addWidget(top_bar)

        source_referers = {}
        for plugin in self._registry.all():
            base = str(getattr(plugin, "base_url", "") or "")
            if base:
                source_referers[plugin.id] = base.rstrip("/") + "/"
        self._source_referers = source_referers
        self._cover_loader = CoverLoader(
            proxy=self._cfg.network.proxy or None,
            user_agent=self._cfg.network.user_agent,
            source_referers=source_referers,
        )

        self._cover_loader.enable_fetcher_retry()
        self._cover_loader.fetcher_retry.connect(self._on_cover_fetcher_retry)

        self.results_grid = ResultsGrid(cover_loader=self._cover_loader)
        self.results_grid.result_selected.connect(self._on_result_selected)

        self.detail_view = DetailView(self._cfg.default_formats, cover_loader=self._cover_loader)
        self.detail_view.download_requested.connect(self._on_download_requested)
        self.detail_view.preview_requested.connect(self._on_preview_requested)

        middle = QSplitter(Qt.Horizontal)
        middle.addWidget(self.results_grid)
        middle.addWidget(self.detail_view)
        middle.setStretchFactor(0, 3)
        middle.setStretchFactor(1, 2)
        root.addWidget(middle, stretch=1)

        self.download_panel = DownloadPanel()
        self.download_panel.setFixedHeight(140)
        self.download_panel.pause_toggled.connect(self._on_pause_toggled)
        self.download_panel.stop_requested.connect(self._on_stop_requested)
        root.addWidget(self.download_panel)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Ready")
        self._build_menu()

    def _build_menu(self) -> None:
        cache_menu = self.menuBar().addMenu("&Cache")
        clear_action = cache_menu.addAction("Clear cover cache…")
        clear_action.setStatusTip("Delete every cached cover image (downloads are untouched)")
        clear_action.triggered.connect(self._on_clear_cache)

    def _on_clear_cache(self) -> None:
        answer = QMessageBox.question(
            self,
            "Clear cover cache",
            "Delete all cached cover images?\n\nThis only affects the image cache - "
            "your downloaded series are not touched.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        self.statusBar().showMessage("Clearing cover cache…")
        signals = CacheClearSignals()
        signals.done.connect(self._on_cache_cleared)
        QThreadPool.globalInstance().start(CacheClearWorker(signals))

    def _on_cache_cleared(self, files_removed: int, bytes_freed: int) -> None:
        freed_mb = bytes_freed / 1_000_000
        message = f"Removed {files_removed:,} cached file(s), freed {freed_mb:.1f} MB."
        self.statusBar().showMessage(message, 20000)
        QMessageBox.information(self, "Cache cleared", message)

    def _populate_sources(self) -> None:
        selected_cat = (
            self.category_picker.currentData() if hasattr(self, "category_picker") else "All"
        )
        previous_source_id = (
            self.source_picker.currentData() if hasattr(self, "source_picker") else None
        )

        self.source_picker.clear()

        multi_label = (
            "🌐 All Sources (Multi-Search)"
            if selected_cat == "All"
            else f"🌐 All {selected_cat} Sources (Multi-Search)"
        )
        self.source_picker.addItem(multi_label, "__all__")

        for plugin in self._registry.all():
            cat = getattr(plugin, "category", "General")
            if selected_cat == "All" or cat == selected_cat:
                display_name = f"{plugin.name} [{cat}]" if selected_cat == "All" else plugin.name
                self.source_picker.addItem(display_name, plugin.id)

        if previous_source_id:
            idx = self.source_picker.findData(previous_source_id)
            if idx >= 0:
                self.source_picker.setCurrentIndex(idx)

    def _on_category_changed(self) -> None:
        self._populate_sources()

    def _current_plugin(self):
        source_id = self.source_picker.currentData()
        if source_id == "__all__":
            return None
        return self._registry.get(source_id)

    def _on_search_text_changed(self, text: str) -> None:
        if not text.strip():
            self.results_grid.set_results([])
            self.statusBar().showMessage("Ready")

    def _on_search(self) -> None:
        query = self.search_bar.text().strip()
        if not query:
            return

        selected_source_id = self.source_picker.currentData()
        selected_cat = self.category_picker.currentData()

        if selected_source_id == "__all__":
            target_plugins = [
                p
                for p in self._registry.all()
                if selected_cat == "All" or getattr(p, "category", "General") == selected_cat
            ]
            logger.info("Multi-search '%s' across %d plugins", query, len(target_plugins))
            self.statusBar().showMessage(
                f"Multi-searching '{query}' across {len(target_plugins)} sources..."
            )

            async def task() -> None:
                try:

                    async def fetch_top_5(plugin):
                        try:
                            res = await plugin.search(query)
                            return res[:5]
                        except Exception as err:
                            logger.warning("Multi-search failed on %s: %s", plugin.id, err)
                            return []

                    nested_results = await asyncio.gather(*(fetch_top_5(p) for p in target_plugins))
                    combined = [r for sublist in nested_results for r in sublist]
                    self._bridge.search_done.emit(combined)
                except Exception as exc:
                    self._bridge.error.emit(f"Multi-search failed: {exc}")

            self._bridge.submit(task())

        else:
            plugin = self._registry.get(selected_source_id)
            logger.info("Search '%s' on %s", query, plugin.id)
            self.statusBar().showMessage(f"Searching '{query}' on {plugin.name}...")

            async def task() -> None:
                try:
                    results = await plugin.search(query)
                    self._bridge.search_done.emit(results[:20])
                except Exception as exc:
                    self._bridge.error.emit(f"Search failed: {exc}")

            self._bridge.submit(task())

    def _on_search_done(self, results: list[SearchResult]) -> None:
        self.results_grid.set_results(results)
        self.statusBar().showMessage(f"Showing {len(results)} results.", 5000)

    def _on_result_selected(self, result: SearchResult) -> None:
        plugin = self._registry.get(result.source_id)
        logger.info("Open detail: %s", result.ref)
        self.statusBar().showMessage(f"Loading '{result.title}'...")

        async def task() -> None:
            try:
                detail = await plugin.get_detail(result)
                chapters = await plugin.list_chapters(detail)
                self._bridge.detail_done.emit(detail, chapters)
            except Exception as exc:
                self._bridge.error.emit(f"Load detail failed: {exc}")

        self._bridge.submit(task())

    def _on_cover_fetcher_retry(self, url: str, source_id: str) -> None:
        """Re-fetch a 403'd cover through the plugin fetcher."""
        try:
            plugin = self._registry.get(source_id)
        except (KeyError, StopIteration):
            self._cover_loader.retry_dead.emit(url)
            return
        referer = self._source_referers.get(source_id)

        async def task() -> None:
            try:
                data = await plugin.fetcher.get_bytes(
                    url, headers={"Referer": referer} if referer else None
                )
            except Exception as exc:
                logger.debug("Cover fetcher retry failed for %s: %s", url, exc)
                self._cover_loader.retry_dead.emit(url)
                return
            self._cover_loader.retry_done.emit(url, data)

        self._bridge.submit(task())

    def _on_detail_done(self, detail: MangaDetail, chapters: list[Chapter]) -> None:
        self._detail = detail
        self.detail_view.show_detail(detail, chapters)
        self.statusBar().showMessage(
            f"Loaded '{detail.title}' with {len(chapters)} chapters.", 5000
        )

    def _ensure_reader(self) -> ReaderWindow:
        if self._reader is None:
            self._reader = ReaderWindow(self._bridge)
        return self._reader

    def _on_preview_requested(self, chapter: Chapter) -> None:
        """Open a chapter in the scroll reader, without touching the download queue."""
        if not self._detail:
            return
        plugin = self._registry.get(self._detail.source_id)
        reader = self._ensure_reader()
        reader.open_chapter(plugin, self._detail.title, chapter)
        self.statusBar().showMessage(f"Previewing '{chapter.title}'...", 5000)

    def _on_download_requested(self, chapters: list[Chapter], formats: list[OutputFormat]) -> None:
        if not self._detail:
            return
        if self._active_control is not None:
            self.statusBar().showMessage(
                "A download is already running - stop it before starting another one.", 6000
            )
            return

        plugin = self._registry.get(self._detail.source_id)
        tasks = [
            DownloadTask(
                source_id=self._detail.source_id,
                manga_title=self._detail.title,
                chapter=ch,
                formats=formats,
            )
            for ch in chapters
        ]
        self.download_panel.add_tasks(tasks)

        control = DownloadControl()
        self._active_control = control
        self.download_panel.set_active(True)
        self.detail_view.download_btn.setEnabled(False)

        writer = OutputWriter(self._cfg)

        fallback = SourceFallback(self._cfg, self._registry.all())
        engine = DownloadEngine(
            self._cfg,
            fetcher=plugin.fetcher,
            writer=writer,
            on_progress=lambda t: self._bridge.task_progress.emit(t),
            fallback=fallback,
            control=control,
        )

        async def task() -> None:
            try:
                summary = await engine.run(tasks, resolve_pages=plugin.get_pages)
                self._bridge.run_done.emit(summary)
            except Exception as exc:
                self._bridge.error.emit(f"Download failed: {exc}")
            finally:
                self._bridge.run_finished.emit()

        self._bridge.submit(task())

    def _on_pause_toggled(self, paused: bool) -> None:
        control = self._active_control
        if control is None:
            return
        if paused:
            control.pause()
            self.statusBar().showMessage(
                "Download paused - pages already in flight will finish.", 8000
            )
        else:
            control.resume()
            self.statusBar().showMessage("Download resumed.", 5000)

    def _on_stop_requested(self) -> None:
        control = self._active_control
        if control is None:
            return
        control.stop()
        self.download_panel.set_stopping()
        self.statusBar().showMessage("Stopping the download…", 8000)
        logger.info("User requested a download stop")

    def _on_task_progress(self, task: DownloadTask) -> None:
        self.download_panel.update_task(task)

    def _on_run_finished(self) -> None:
        """Runs after the engine returns, success or failure - re-arm the UI."""
        self._active_control = None
        self.download_panel.set_active(False)
        self.detail_view.download_btn.setEnabled(True)

    def _on_run_done(self, summary: RunSummary) -> None:
        """Explicit end-of-run verdict - after every fallback has been tried."""
        self._on_run_finished()
        message = summary.warning_message()

        if summary.stopped:
            self.download_panel.mark_unstarted_stopped()
            logger.info(message)
            self.statusBar().showMessage(message, 15000)
            return

        if summary.ok:
            self.statusBar().showMessage(message, 8000)
            return

        logger.warning(message)
        self.statusBar().showMessage(message, 20000)
        details = summary.detail_lines()
        shown = details[:10]
        body = "\n".join(f"• {line}" for line in shown)
        if len(details) > len(shown):
            body += f"\n… and {len(details) - len(shown)} more (see manhua-dl.log)"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Some chapters were not saved")
        box.setText(message)
        box.setInformativeText(body)
        box.exec()

    def _on_error(self, message: str) -> None:
        logger.error(message)
        self.statusBar().showMessage(message, 8000)

    def closeEvent(self, event) -> None:
        if self._reader is not None:
            self._reader.close()
        self._bridge.stop()
        super().closeEvent(event)


def load_stylesheet() -> str:
    qss = Path(__file__).with_name("style.qss")
    return qss.read_text(encoding="utf-8") if qss.exists() else ""

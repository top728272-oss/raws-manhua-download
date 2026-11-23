"""Application entry point.

Run with:  python -m app.main
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from app import __app_name__
from app.config import AppConfig, load_config, setup_logging
from app.core.cache import maybe_prune
from app.core.library import ensure_state_dir
from app.core.registry import PluginRegistry
from app.gui.main_window import MainWindow, load_stylesheet

logger = logging.getLogger(__name__)


def _prune_cache(cfg: AppConfig) -> None:
    """Trim the cover cache in the background so startup is never delayed.

    A cold cache of millions of entries makes the first scan slow, so it runs on
    a daemon thread; `maybe_prune` then short-circuits on later launches via its
    state file.
    """
    if not cfg.cache.prune_on_startup:
        return

    def work() -> None:
        try:
            result = maybe_prune(cfg.cache.max_size_mb * 1024 * 1024)
        except Exception as exc:
            logger.warning("Cache prune failed: %s", exc)
            return
        if result is None:
            logger.debug("Cache prune skipped (fresh and under cap).")
            return
        if result.files_removed:
            logger.info(
                "Cache prune: removed %d file(s), freed %.1f MB (%.1f MB remain)",
                result.files_removed,
                result.bytes_freed_mb,
                result.bytes_after_mb,
            )
        else:
            logger.info(
                "Cache prune: under cap (%.1f/%d MB), nothing to evict",
                result.bytes_after_mb,
                cfg.cache.max_size_mb,
            )

    threading.Thread(target=work, name="cache-prune", daemon=True).start()


def main() -> int:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    cfg = load_config()
    setup_logging(cfg)
    logger.info("Booting %s", __app_name__)

    ensure_state_dir(cfg.output_path)
    _prune_cache(cfg)

    app = QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(load_stylesheet())

    registry = PluginRegistry(cfg)
    registry.discover()
    if not registry.all():
        logger.warning("No source plugins registered - check config.yaml.")

    window = MainWindow(cfg, registry)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

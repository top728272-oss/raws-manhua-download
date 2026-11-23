"""Plugin registry - auto-discovers SourcePlugin subclasses in app.plugins.

Walks the `app.plugins` package, imports each module, finds SourcePlugin
subclasses, and instantiates the ones that are `enabled` in config. Each plugin
is paired with a fetcher matching its declared fetch_mode.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil

import app.plugins as plugins_pkg
from app.config import AppConfig
from app.core.fetcher import BaseFetcher, make_fetcher
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)


class PluginRegistry:
    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._plugins: dict[str, SourcePlugin] = {}
        self._fetchers: list[BaseFetcher] = []

    def discover(self) -> None:
        self._plugins.clear()
        for mod_info in pkgutil.iter_modules(plugins_pkg.__path__):
            name = mod_info.name
            if name in ("base",) or name.startswith("_"):
                continue
            module = importlib.import_module(f"{plugins_pkg.__name__}.{name}")
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, SourcePlugin)
                    and obj is not SourcePlugin
                    and obj.__module__ == module.__name__
                ):
                    self._register(obj)

    def _register(self, cls: type[SourcePlugin]) -> None:
        source_id = cls.id
        settings = self._cfg.source_settings(source_id)
        if not settings.get("enabled", True):
            logger.info("Source '%s' disabled in config; skipping.", source_id)
            return
        fetcher = make_fetcher(cls.fetch_mode, self._cfg)
        self._fetchers.append(fetcher)
        instance = cls(self._cfg, fetcher)
        self._plugins[source_id] = instance
        logger.info("Registered source: %s (%s)", source_id, cls.fetch_mode.value)

    def get(self, source_id: str) -> SourcePlugin:
        return self._plugins[source_id]

    def all(self) -> list[SourcePlugin]:
        return list(self._plugins.values())

    def ids(self) -> list[str]:
        return list(self._plugins.keys())

    async def aclose(self) -> None:
        for f in self._fetchers:
            await f.aclose()

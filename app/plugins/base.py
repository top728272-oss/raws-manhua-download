"""The source plugin contract.

Every source implements this
same 4-method shape - the convergent pattern across the whole manga-downloader
ecosystem:

    search        -> list[SearchResult]
    get_detail    -> MangaDetail
    list_chapters -> list[Chapter]
    get_pages     -> list[Page]

A plugin declares its `fetch_mode` (HTTP or PLAYWRIGHT). The registry injects the
matching fetcher instance as `self.fetcher`, so plugins never construct their own
networking - that keeps proxy/pacing centralized.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from app.config import AppConfig
from app.core.fetcher import BaseFetcher
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult

logger = logging.getLogger(__name__)


class SourcePlugin(ABC):
    """Base class all source plugins inherit from."""

    id: str = ""
    name: str = ""
    category: str = "General"
    fetch_mode: FetchMode = FetchMode.HTTP

    def __init__(self, cfg: AppConfig, fetcher: BaseFetcher) -> None:
        self.cfg = cfg
        self.fetcher = fetcher
        self.settings = cfg.source_settings(self.id)

    @abstractmethod
    async def search(self, query: str) -> list[SearchResult]:
        """Search the source for `query`, return ranked results."""

    @abstractmethod
    async def get_detail(self, result: SearchResult) -> MangaDetail:
        """Fetch full metadata for a chosen search result."""

    @abstractmethod
    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        """List all chapters for a series, **in reading order**.

        Reading order means ``index`` 1 is the FIRST chapter of the series (the
        one a reader opens first) - not the newest. Sites very often list the
        newest chapter first, and the GUI renders this list verbatim, so passing
        a newest-first list through shows the user an inverted chapter list.
        Sort or reverse the site's order before assigning ``index`` (see
        guazimanhua / hanman18 / one99mh for the bare-``reverse()`` shape, and
        wmanhua for a sort that is also idempotent if the site flips its order).
        """

    @abstractmethod
    async def get_pages(self, chapter: Chapter) -> list[Page]:
        """Resolve a chapter into its ordered image pages."""

    def __repr__(self) -> str:
        return f"<SourcePlugin {self.id} ({self.fetch_mode.value})>"

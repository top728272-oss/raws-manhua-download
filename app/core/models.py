"""Data models shared across the app.

These are plain dataclasses that flow through the whole pipeline:
    SearchResult -> MangaDetail -> Chapter -> Page

Plugins produce these; the GUI + downloader consume them. Keeping them source-
agnostic is what lets the plugin architecture stay clean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FetchMode(StrEnum):
    HTTP = "http"
    PLAYWRIGHT = "playwright"


class OutputFormat(StrEnum):
    FOLDER = "folder"
    CBZ = "cbz"


@dataclass
class SearchResult:
    source_id: str
    title: str
    ref: str
    cover_url: str | None = None
    author: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class MangaDetail:
    source_id: str
    title: str
    ref: str
    cover_url: str | None = None
    author: str | None = None
    description: str | None = None
    status: str | None = None
    tags: list[str] = field(default_factory=list)
    chapter_count: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Chapter:
    source_id: str
    manga_ref: str
    title: str
    ref: str
    index: int = 0
    group: str = "default"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Page:
    index: int
    image_url: str
    referer: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DownloadTask:
    source_id: str
    manga_title: str
    chapter: Chapter
    formats: list[OutputFormat]
    total_pages: int = 0
    completed_pages: int = 0
    status: str = "queued"
    error: str | None = None

    fallback_source_id: str | None = None
    fallback_title: str | None = None

    pages_saved: int = 0

    complete: bool = False

    missing_pages: int = 0

    skipped: bool = False

    recovered_pages: int = 0

    note: str | None = None

    server_error: bool = False

    @property
    def progress(self) -> float:
        if self.total_pages <= 0:
            return 0.0
        return min(self.completed_pages / self.total_pages, 1.0)

    @property
    def saved(self) -> bool:
        return self.pages_saved > 0

    @property
    def pages_label(self) -> str | None:
        """``"8/12 pages"`` while a chapter is short of pages, else None."""
        if self.saved and not self.complete and self.total_pages:
            return f"{self.pages_saved}/{self.total_pages} pages"
        return None

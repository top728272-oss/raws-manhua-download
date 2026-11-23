"""Cross-source fallback for chapters that cannot be downloaded in place.

When a chapter's own source answers with an HTTP **5xx** (dead host, overloaded
edge, half-rotated CDN) - or simply resolves to zero pages - the download engine
asks :class:`SourceFallback` to find the *same series* on another enabled source
and re-resolve the chapter there. The images are then fetched from that source
and written under the **original** series folder, so a run that hits one broken
site still leaves the user with a complete, coherent library. The task records
the provenance (``fallback_source_id`` / ``note``) and the log says so.

Matching is deliberately conservative, because a wrong match silently downloads
the wrong comic - far worse than reporting a failure:

* A candidate series must clear ``min_similarity`` against the original title.
* A candidate chapter must agree on its leading chapter number; failing that, on
  its exact title; failing that, on its 1-based reading-order index - and the
  index is refused outright when the two sites number chapters differently.

Resolved series are cached per (title, series ref) so a run that loses its
source mid-queue searches the other sites once, not once per chapter.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from app.config import AppConfig
from app.core.models import Chapter, Page, SearchResult
from app.core.output import leading_chapter_number
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

DEFAULT_MIN_SIMILARITY = 0.7


_NOISE = re.compile(r"[\s\-_·・:：;；!！?？.,，。、'\"“”‘’()（）\[\]【】<>《》/\\|+~-…^&*#@$%]+")


@dataclass
class FallbackChapter:
    """A chapter re-resolved from a source other than the one that failed."""

    source_id: str
    source_name: str
    manga_title: str
    chapter: Chapter
    pages: list[Page]
    similarity: float
    matched_by: str


class FallbackResolver(Protocol):
    """What the download engine needs from a fallback provider."""

    async def find_chapter(
        self, manga_title: str, chapter: Chapter, failed_source_id: str
    ) -> FallbackChapter | None: ...


def normalise_title(title: str | None) -> str:
    """Casefold, NFKC-fold and strip punctuation/space so titles compare cleanly."""
    text = unicodedata.normalize("NFKC", title or "").casefold()
    return _NOISE.sub("", text)


def title_similarity(a: str | None, b: str | None) -> float:
    """0..1 similarity between two series titles.

    An exact match after normalisation is 1.0. Otherwise the best of the
    character-level ratio and a containment score, so "斗破苍穹" still scores
    highly against "斗破苍穹漫画" (which is the same series, re-titled) without
    letting a short unrelated title match inside a long one.
    """
    na, nb = normalise_title(a), normalise_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0

    score = difflib.SequenceMatcher(None, na, nb).ratio()
    if na in nb or nb in na:
        shorter, longer = sorted((na, nb), key=len)

        score = max(score, 0.5 + 0.5 * (len(shorter) / len(longer)))
    return score


def pick_best_match(
    title: str,
    results: list[SearchResult],
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> tuple[SearchResult, float] | None:
    """Best candidate above `min_similarity`, or None when nothing is close enough."""
    best: SearchResult | None = None
    best_score = 0.0
    for result in results or []:
        score = title_similarity(title, result.title)
        if score > best_score:
            best, best_score = result, score
    if best is None or best_score < min_similarity:
        if best is not None:
            logger.info(
                "fallback: best candidate '%s' only scored %.2f (< %.2f); ignoring",
                best.title,
                best_score,
                min_similarity,
            )
        return None
    return best, best_score


def match_chapter(chapters: list[Chapter], wanted: Chapter) -> tuple[Chapter, str] | None:
    """Find `wanted`'s counterpart in another source's chapter list.

    Tries, in order: same leading chapter number ("第12话" == "第12話"), same
    normalised title, then the same 1-based reading-order index. Returns the
    chapter plus a human-readable note of what matched, or None.

    The index is the last resort **and is skipped when both sides number their
    chapters but none of the numbers agree** - that means the two sites count
    chapters differently (split volumes, merged arcs), so the reading position
    no longer points at the same chapter and guessing would download the wrong
    one. Refusing is better than a silent mis-match.
    """
    if not chapters:
        return None

    wanted_number = leading_chapter_number(wanted.title)
    if wanted_number:
        for chapter in chapters:
            if leading_chapter_number(chapter.title) == wanted_number:
                return chapter, f"chapter number {wanted_number}"

    wanted_title = normalise_title(wanted.title)
    if wanted_title:
        for chapter in chapters:
            if normalise_title(chapter.title) == wanted_title:
                return chapter, "exact chapter title"

    if wanted_number and any(leading_chapter_number(c.title) for c in chapters):
        logger.info(
            "fallback: chapter numbering disagrees (%r is absent there); refusing to guess",
            wanted.title,
        )
        return None

    index = int(wanted.index or 0)
    if 1 <= index <= len(chapters):
        return chapters[index - 1], f"reading-order index {index}"
    return None


@dataclass
class _Series:
    plugin: SourcePlugin
    title: str
    chapters: list[Chapter]
    similarity: float


class SourceFallback:
    """Finds a series on another enabled source and resolves chapters there."""

    def __init__(
        self,
        cfg: AppConfig,
        plugins: list[SourcePlugin],
        min_similarity: float | None = None,
    ) -> None:
        self._plugins = list(plugins)
        download = cfg.download
        self._enabled = bool(getattr(download, "fallback_enabled", True))
        if min_similarity is None:
            min_similarity = float(
                getattr(download, "fallback_min_similarity", DEFAULT_MIN_SIMILARITY)
            )
        self._min_similarity = min_similarity
        self._series: dict[tuple[str, str], _Series | None] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def find_chapter(
        self, manga_title: str, chapter: Chapter, failed_source_id: str
    ) -> FallbackChapter | None:
        if not self._enabled:
            logger.info("fallback: disabled in config; not looking for '%s'", manga_title)
            return None

        key = (normalise_title(manga_title), chapter.manga_ref or "")
        async with self._lock:
            series = await self._resolve_series(key, manga_title, failed_source_id)
        if series is None:
            return None

        match = match_chapter(series.chapters, chapter)
        if match is None:
            logger.warning(
                "fallback: %s has '%s' but no counterpart for chapter '%s' (%d chapter(s))",
                series.plugin.id,
                series.title,
                chapter.title,
                len(series.chapters),
            )
            return None
        fallback_chapter, matched_by = match

        try:
            pages = list(await series.plugin.get_pages(fallback_chapter))
        except Exception as err:
            logger.warning(
                "fallback: %s failed to resolve pages for '%s': %s",
                series.plugin.id,
                fallback_chapter.title,
                err,
            )
            return None
        if not pages:
            logger.warning(
                "fallback: %s listed 0 pages for '%s'", series.plugin.id, fallback_chapter.title
            )
            return None

        return FallbackChapter(
            source_id=series.plugin.id,
            source_name=series.plugin.name or series.plugin.id,
            manga_title=series.title,
            chapter=fallback_chapter,
            pages=pages,
            similarity=series.similarity,
            matched_by=matched_by,
        )

    def _candidates(self, failed_source_id: str) -> list[SourcePlugin]:
        """Other sources, same category first (a General series should not jump
        straight to an 18+ mirror, but it may still use one as a last resort)."""
        failed = next((p for p in self._plugins if p.id == failed_source_id), None)
        category = getattr(failed, "category", None) if failed is not None else None

        others: list[SourcePlugin] = []
        seen: set[str] = set()
        for plugin in self._plugins:
            if plugin.id == failed_source_id or plugin.id in seen:
                continue
            seen.add(plugin.id)
            others.append(plugin)
        if category:
            others.sort(key=lambda p: 0 if getattr(p, "category", None) == category else 1)
        return others

    async def _resolve_series(
        self, key: tuple[str, str], manga_title: str, failed_source_id: str
    ) -> _Series | None:
        if key in self._series:
            return self._series[key]
        found = await self._search_other_sources(manga_title, failed_source_id)
        self._series[key] = found
        return found

    async def _search_other_sources(
        self, manga_title: str, failed_source_id: str
    ) -> _Series | None:
        if not normalise_title(manga_title):
            return None

        candidates = self._candidates(failed_source_id)
        logger.info(
            "fallback: looking for '%s' on %d other source(s): %s",
            manga_title,
            len(candidates),
            ", ".join(p.id for p in candidates),
        )

        for plugin in candidates:
            try:
                results = await plugin.search(manga_title)
            except Exception as err:
                logger.debug("fallback: search on %s failed: %s", plugin.id, err)
                continue

            best = pick_best_match(manga_title, list(results or []), self._min_similarity)
            if best is None:
                continue
            result, score = best

            try:
                detail = await plugin.get_detail(result)
                chapters = list(await plugin.list_chapters(detail))
            except Exception as err:
                logger.warning("fallback: %s detail/chapters failed: %s", plugin.id, err)
                continue
            if not chapters:
                logger.warning("fallback: %s listed no chapters for '%s'", plugin.id, detail.title)
                continue

            logger.info(
                "fallback: '%s' -> %s '%s' (%.2f similarity, %d chapter(s))",
                manga_title,
                plugin.id,
                detail.title,
                score,
                len(chapters),
            )
            return _Series(plugin=plugin, title=detail.title, chapters=chapters, similarity=score)

        logger.warning("fallback: no other source has a confident match for '%s'", manga_title)
        return None

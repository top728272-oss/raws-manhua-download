"""Tests for the cross-source fallback (app/core/fallback.py).

The fallback is what rescues a chapter when its own source answers HTTP 5xx.
Its risk is *silent mis-matching* — downloading the wrong comic — so the
matching helpers are tested directly, and the plugin-facing path is exercised
with fake sources.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import AppConfig, DownloadConfig
from app.core.fallback import (
    SourceFallback,
    match_chapter,
    normalise_title,
    pick_best_match,
    title_similarity,
)
from app.core.fetcher import is_server_error
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.core.output import leading_chapter_number
from app.plugins.base import SourcePlugin

# --------------------------------------------------------------------------- #
# helpers under test
# --------------------------------------------------------------------------- #


def test_normalise_title_strips_decoration_and_case() -> None:
    assert normalise_title("《斗破苍穹》") == normalise_title("斗破苍穹")
    assert normalise_title("One Piece!") == normalise_title("one  piece")
    assert normalise_title("斗破苍穹（漫画）") == "斗破苍穹漫画"
    assert normalise_title(None) == ""
    assert normalise_title("") == ""


def test_title_similarity_exact_match_is_one() -> None:
    assert title_similarity("斗破苍穹", "《斗破苍穹》") == 1.0


def test_title_similarity_containment_scores_high() -> None:
    # Same series, re-titled by the other site — must clear the 0.7 gate.
    assert title_similarity("斗破苍穹", "斗破苍穹漫画") >= 0.7
    # A short unrelated title inside a long one must NOT clear it.
    assert title_similarity("火", "火影忍者") < 0.7


def test_title_similarity_unrelated_is_low() -> None:
    assert title_similarity("斗破苍穹", "海贼王") < 0.3
    assert title_similarity("", "anything") == 0.0


def test_pick_best_match_picks_highest_and_enforces_threshold() -> None:
    results = [
        SearchResult(source_id="s", title="海贼王", ref="a"),
        SearchResult(source_id="s", title="斗破苍穹漫画", ref="b"),
        SearchResult(source_id="s", title="斗破苍穹", ref="c"),
    ]
    best = pick_best_match("斗破苍穹", results)
    assert best is not None
    result, score = best
    assert result.ref == "c"
    assert score == 1.0

    # Nothing close enough -> None, never a "best of a bad bunch".
    unrelated = [SearchResult(source_id="s", title="海贼王", ref="a")]
    assert pick_best_match("斗破苍穹", unrelated) is None
    assert pick_best_match("斗破苍穹", []) is None


def test_leading_chapter_number_variants() -> None:
    assert leading_chapter_number("第12话 诡秘世界") == "12"
    assert leading_chapter_number("第12話") == "12"
    assert leading_chapter_number("Chapter 7") == "7"
    assert leading_chapter_number("Ch. 3.5 特别篇") == "3.5"
    assert leading_chapter_number("01 起点") == "01"
    assert leading_chapter_number("1.5 特别篇") == "1.5"
    # content numbers are not chapter numbers
    assert leading_chapter_number("1秒后末日") is None
    assert leading_chapter_number("诡秘世界") is None


def _chapter(title: str, index: int = 1) -> Chapter:
    return Chapter(source_id="a", manga_ref="x", title=title, ref=f"c{index}", index=index)


def test_match_chapter_by_number_beats_index() -> None:
    # The other site has an extra prologue, so the index is off by one —
    # the number must win.
    chapters = [_chapter("第0话 序章", 1), _chapter("第1话 开始", 2), _chapter("第2话 继续", 3)]
    matched = match_chapter(chapters, _chapter("第1话 开始", 1))
    assert matched is not None
    chapter, how = matched
    assert chapter.title == "第1话 开始"
    assert "chapter number 1" in how


def test_match_chapter_by_exact_title_when_numberless() -> None:
    chapters = [_chapter("起点", 1), _chapter("觉醒", 2)]
    matched = match_chapter(chapters, _chapter("觉醒", 1))
    assert matched is not None
    assert matched[0].title == "觉醒"
    assert matched[1] == "exact chapter title"


def test_match_chapter_falls_back_to_reading_order_index() -> None:
    chapters = [_chapter("甲", 1), _chapter("乙", 2), _chapter("丙", 3)]
    matched = match_chapter(chapters, _chapter("完全不同的名字", 2))
    assert matched is not None
    assert matched[0].title == "乙"
    assert "index 2" in matched[1]


def test_match_chapter_none_when_nothing_lines_up() -> None:
    chapters = [_chapter("甲", 1)]
    assert match_chapter(chapters, _chapter("完全不同的名字", 9)) is None
    assert match_chapter([], _chapter("甲", 1)) is None


# --------------------------------------------------------------------------- #
# 5xx detection
# --------------------------------------------------------------------------- #


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com/x")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def test_is_server_error_detects_5xx() -> None:
    for status in (500, 502, 503, 504, 599):
        assert is_server_error(_status_error(status)) is True


def test_is_server_error_ignores_4xx_and_other_errors() -> None:
    for status in (400, 401, 403, 404, 429):
        assert is_server_error(_status_error(status)) is False
    assert is_server_error(RuntimeError("boom")) is False
    assert is_server_error(None) is False


def test_is_server_error_walks_the_cause_chain() -> None:
    # Plugins routinely wrap the httpx error; the 5xx must still be seen.
    wrapped = RuntimeError("copymanga: no usable host")
    wrapped.__cause__ = _status_error(503)
    assert is_server_error(wrapped) is True


# --------------------------------------------------------------------------- #
# SourceFallback
# --------------------------------------------------------------------------- #


class FakePlugin(SourcePlugin):
    fetch_mode = FetchMode.HTTP

    def __init__(
        self,
        pid: str,
        *,
        category: str = "General",
        results: list[SearchResult] | None = None,
        chapters: list[Chapter] | None = None,
        pages: list[Page] | None = None,
        search_error: Exception | None = None,
        cfg: AppConfig | None = None,
    ) -> None:
        self.id = pid
        self.name = f"{pid} site"
        self.category = category
        self._results = list(results or [])
        self._chapters = list(chapters or [])
        self._pages = list(pages if pages is not None else [Page(index=0, image_url="u/1.webp")])
        self._search_error = search_error
        self.search_calls = 0
        self.pages_calls = 0
        super().__init__(cfg or AppConfig(), None)  # type: ignore[arg-type]

    async def search(self, query: str) -> list[SearchResult]:
        self.search_calls += 1
        if self._search_error is not None:
            raise self._search_error
        return list(self._results)

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        return MangaDetail(source_id=self.id, title=result.title, ref=result.ref)

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        return list(self._chapters)

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        self.pages_calls += 1
        return list(self._pages)


def _other_plugin(pid: str = "other", **kwargs) -> FakePlugin:
    results = kwargs.pop(
        "results", [SearchResult(source_id=pid, title="斗破苍穹", ref="dou-po")]
    )
    chapters = kwargs.pop("chapters", [_chapter("第1话", 1), _chapter("第2话", 2)])
    return FakePlugin(pid, results=results, chapters=chapters, **kwargs)


def _wanted() -> Chapter:
    return Chapter(source_id="broken", manga_ref="ref", title="第1话", ref="c1", index=1)


async def test_fallback_finds_chapter_on_another_source() -> None:
    plugin = _other_plugin()
    fallback = SourceFallback(AppConfig(), [plugin])

    found = await fallback.find_chapter("斗破苍穹", _wanted(), "broken")

    assert found is not None
    assert found.source_id == "other"
    assert found.source_name == "other site"
    assert found.chapter.title == "第1话"
    assert found.pages and found.pages[0].image_url == "u/1.webp"
    assert found.similarity == 1.0
    assert "chapter number 1" in found.matched_by


async def test_fallback_excludes_the_failed_source() -> None:
    broken = _other_plugin("broken")
    fallback = SourceFallback(AppConfig(), [broken])
    assert await fallback.find_chapter("斗破苍穹", _wanted(), "broken") is None
    assert broken.search_calls == 0


async def test_fallback_rejects_a_low_similarity_match() -> None:
    plugin = _other_plugin(results=[SearchResult(source_id="other", title="海贼王", ref="op")])
    fallback = SourceFallback(AppConfig(), [plugin])
    assert await fallback.find_chapter("斗破苍穹", _wanted(), "broken") is None


async def test_fallback_caches_the_series_lookup() -> None:
    plugin = _other_plugin()
    fallback = SourceFallback(AppConfig(), [plugin])

    await fallback.find_chapter("斗破苍穹", _wanted(), "broken")
    await fallback.find_chapter("斗破苍穹", _wanted(), "broken")

    assert plugin.search_calls == 1  # second chapter reuses the cached series


async def test_fallback_skips_sources_that_error_or_have_no_chapters() -> None:
    erroring = FakePlugin("dead", search_error=RuntimeError("connection reset"))
    match = SearchResult(source_id="empty", title="斗破苍穹", ref="d")
    empty = FakePlugin("empty", results=[match], chapters=[])
    good = _other_plugin("good")
    fallback = SourceFallback(AppConfig(), [erroring, empty, good])

    found = await fallback.find_chapter("斗破苍穹", _wanted(), "broken")
    assert found is not None and found.source_id == "good"
    assert erroring.search_calls == 1 and empty.search_calls == 1


async def test_fallback_returns_none_when_the_other_source_has_no_pages() -> None:
    plugin = _other_plugin(pages=[])
    fallback = SourceFallback(AppConfig(), [plugin])
    assert await fallback.find_chapter("斗破苍穹", _wanted(), "broken") is None


async def test_fallback_returns_none_when_chapter_is_missing_there() -> None:
    # The other site numbers its chapters, but none of the numbers agree with
    # ours — the reading-order index must NOT be used to guess a replacement.
    plugin = _other_plugin(chapters=[_chapter("第50话", 1)])
    fallback = SourceFallback(AppConfig(), [plugin])
    assert await fallback.find_chapter("斗破苍穹", _wanted(), "broken") is None


async def test_fallback_prefers_same_category() -> None:
    broken = FakePlugin("broken", category="General", chapters=[])
    adult = _other_plugin("adult", category="18+")
    general = _other_plugin("general", category="General")
    fallback = SourceFallback(AppConfig(), [adult, broken, general])
    found = await fallback.find_chapter("斗破苍穹", _wanted(), "broken")
    assert found is not None and found.source_id == "general"


async def test_fallback_uses_index_when_the_other_site_has_no_numbers() -> None:
    # Unnumbered chapter titles there -> the reading position is the only
    # signal available, and it is safe to use.
    plugin = _other_plugin(chapters=[_chapter("起点", 1), _chapter("觉醒", 2)])
    fallback = SourceFallback(AppConfig(), [plugin])
    wanted = Chapter(source_id="broken", manga_ref="ref", title="第2话", ref="c2", index=2)

    found = await fallback.find_chapter("斗破苍穹", wanted, "broken")

    assert found is not None
    assert found.chapter.title == "觉醒"
    assert "index 2" in found.matched_by


async def test_fallback_can_be_disabled_by_config() -> None:
    plugin = _other_plugin()
    cfg = AppConfig(download=DownloadConfig(fallback_enabled=False))
    fallback = SourceFallback(cfg, [plugin])

    assert fallback.enabled is False
    assert await fallback.find_chapter("斗破苍穹", _wanted(), "broken") is None
    assert plugin.search_calls == 0


async def test_fallback_respects_a_configured_similarity_floor() -> None:
    plugin = _other_plugin(results=[SearchResult(source_id="other", title="斗破苍穹漫画", ref="d")])
    strict = SourceFallback(AppConfig(), [plugin], min_similarity=0.99)
    assert await strict.find_chapter("斗破苍穹", _wanted(), "broken") is None

    loose = SourceFallback(AppConfig(), [plugin], min_similarity=0.7)
    found = await loose.find_chapter("斗破苍穹", _wanted(), "broken")
    assert found is not None and found.source_id == "other"


async def test_fallback_min_similarity_from_config() -> None:
    cfg = AppConfig(download=DownloadConfig(fallback_min_similarity=0.99))
    plugin = _other_plugin(results=[SearchResult(source_id="other", title="斗破苍穹漫画", ref="d")])
    fallback = SourceFallback(cfg, [plugin])
    assert await fallback.find_chapter("斗破苍穹", _wanted(), "broken") is None


@pytest.mark.parametrize("title", ["", "   "])
async def test_fallback_ignores_an_empty_title(title: str) -> None:
    plugin = _other_plugin()
    fallback = SourceFallback(AppConfig(), [plugin])
    assert await fallback.find_chapter(title, _wanted(), "broken") is None
    assert plugin.search_calls == 0

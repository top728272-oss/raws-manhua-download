"""Tests for Mhua5Plugin (漫画屋, www.mhua5.com).

Fixtures are trimmed captures of the live site (see
``scratch/build_new_site_fixtures.py``). The two that matter most are the search
pair: ``mhua5_search.html`` (real hits) and ``mhua5_search_miss.html`` (a miss
that still renders a full grid of recommendation cards).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.config import AppConfig
from app.core.models import Chapter, MangaDetail, SearchResult
from app.core.registry import PluginRegistry
from app.plugins.mhua5 import Mhua5Plugin, _comic_slug, _search_count

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _plugin(fetcher=None) -> Mhua5Plugin:
    return Mhua5Plugin(AppConfig(), fetcher or AsyncMock())


# ---------------------------------------------------------------------------
# zero-hit detection
# ---------------------------------------------------------------------------


def test_search_count_reads_the_result_caption() -> None:
    assert _search_count(_fixture("mhua5_search.html")) == 17


def test_search_count_is_zero_on_a_miss_page() -> None:
    # The miss page says "很遗憾…我们为您推荐了以下漫画~" and lists 12 cards.
    assert _search_count(_fixture("mhua5_search_miss.html")) == 0


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        ('“<b class="hl">斗罗</b>”搜索结果（17）', 17),
        ("搜索结果(0)", 0),
        ("很遗憾，您搜索的内容暂时没有找到", 0),
        ("", 0),
    ],
)
def test_search_count_parses_caption_variants(caption: str, expected: int) -> None:
    assert _search_count(caption) == expected


# ---------------------------------------------------------------------------
# ref normalisation
# ---------------------------------------------------------------------------


def test_comic_slug() -> None:
    slug = "douluodalu4zhongjidouluo"
    assert _comic_slug(slug) == slug
    assert _comic_slug(f"https://www.mhua5.com/index.php/comic/{slug}") == slug
    assert _comic_slug(f"/index.php/comic/{slug}") == slug
    assert _comic_slug("") == ""


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_parse_search_returns_titled_cards_with_covers() -> None:
    results = _plugin()._parse_search(_fixture("mhua5_search.html"))

    assert len(results) == 8
    assert results[0].title == "斗罗大陆3龙王传说"
    assert results[0].ref == "douluodalu3longwangchuanshuo"
    assert results[0].cover_url == (
        "https://static-tw.baozimh.com/cover/"
        "douluodalu3longwangchuanshuo-shenman.jpg?w=285&h=375&q=100"
    )
    assert all(r.title != r.ref for r in results), "titles must not fall back to the slug"
    assert len({r.ref for r in results}) == 8


def test_parse_search_returns_nothing_when_the_caption_reports_a_miss() -> None:
    """The miss page renders 12 recommendation cards under the same container."""
    assert _plugin()._parse_search(_fixture("mhua5_search_miss.html")) == []


def test_parse_search_without_container_returns_empty() -> None:
    assert _plugin()._parse_search("<html><body>nope</body></html>") == []


async def test_search_hits_the_search_endpoint() -> None:
    fetcher = AsyncMock()
    fetcher.get_text.return_value = _fixture("mhua5_search.html")
    plugin = _plugin(fetcher)

    results = await plugin.search("斗罗")

    assert len(results) == 8
    assert fetcher.get_text.await_args.args[0] == (
        "https://www.mhua5.com/index.php/search?key=%E6%96%97%E7%BD%97"
    )


async def test_search_returns_empty_when_request_fails() -> None:
    fetcher = AsyncMock()
    fetcher.get_text.side_effect = RuntimeError("boom")

    assert await _plugin(fetcher).search("斗罗") == []


async def test_search_with_comic_url_resolves_directly() -> None:
    fetcher = AsyncMock()
    fetcher.get_text.return_value = _fixture("mhua5_comic.html")
    plugin = _plugin(fetcher)

    results = await plugin.search("https://www.mhua5.com/index.php/comic/douluodalu4zhongjidouluo")

    assert [r.title for r in results] == ["斗罗大陆4终极斗罗"]
    assert results[0].ref == "douluodalu4zhongjidouluo"


async def test_search_with_chapter_url_resolves_owning_comic() -> None:
    fetcher = AsyncMock()
    fetcher.get_text.side_effect = [
        _fixture("mhua5_chapter.html"),
        _fixture("mhua5_comic.html"),
    ]
    plugin = _plugin(fetcher)

    results = await plugin.search("https://www.mhua5.com/index.php/chapter/210986")

    assert [r.ref for r in results] == ["douluodalu4zhongjidouluo"]


async def test_search_with_blank_query_does_not_hit_the_network() -> None:
    fetcher = AsyncMock()

    assert await _plugin(fetcher).search("  ") == []
    fetcher.get_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------


def test_parse_detail() -> None:
    detail = _plugin()._parse_detail(
        _fixture("mhua5_comic.html"),
        SearchResult(source_id="mhua5", title="ignored", ref="douluodalu4zhongjidouluo"),
        "douluodalu4zhongjidouluo",
    )

    assert detail.title == "斗罗大陆4终极斗罗"
    assert detail.author == "神漫"
    assert detail.status == "连载"
    assert detail.tags == ["热血"]
    assert detail.cover_url == (
        "https://static-tw.baozimh.com/cover/douluodalu4zhongjidouluo-shenman.jpg?w=285&h=375&q=100"
    )
    assert detail.description and detail.description.startswith("斗罗联邦科考队")


def test_parse_detail_ignores_template_placeholders() -> None:
    """The page's layui templates use `{{ item.name }}` in the same markup shape."""
    detail = _plugin()._parse_detail(
        _fixture("mhua5_comic.html"),
        SearchResult(source_id="mhua5", title="ignored", ref="douluodalu4zhongjidouluo"),
        "douluodalu4zhongjidouluo",
    )

    assert "{{" not in detail.title


# ---------------------------------------------------------------------------
# chapters
# ---------------------------------------------------------------------------


def test_parse_chapters_keeps_page_order() -> None:
    chapters = _plugin()._parse_chapters(_fixture("mhua5_comic.html"), "douluodalu4zhongjidouluo")

    # The fixture keeps the head (40) and tail (8) of a 719-chapter list.
    assert len(chapters) == 48
    assert chapters[0].title == "序章"
    assert chapters[0].ref == "/index.php/chapter/210986"
    assert chapters[0].extra == {"cid": "210986"}
    assert chapters[-1].title == "半步神王銀龍王"
    assert chapters[-1].ref == "/index.php/chapter/3432627"
    assert [c.index for c in chapters] == list(range(1, 49))
    assert len({c.ref for c in chapters}) == 48


def test_parse_chapters_without_list_returns_empty() -> None:
    assert _plugin()._parse_chapters("<html></html>", "x") == []


async def test_list_chapters_reuses_stored_detail_html() -> None:
    fetcher = AsyncMock()
    plugin = _plugin(fetcher)
    detail = MangaDetail(
        source_id="mhua5",
        title="t",
        ref="r",
        extra={"detail_html": _fixture("mhua5_comic.html")},
    )

    chapters = await plugin.list_chapters(detail)

    assert len(chapters) == 48
    fetcher.get_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


def test_parse_pages_reads_lazy_read_images() -> None:
    pages = _plugin()._parse_pages(_fixture("mhua5_chapter.html"))

    assert [p.index for p in pages] == [0, 1, 2]
    assert pages[0].image_url == (
        "https://s2.baozimh.com/scomic/douluodalu4zhongjidouluo-shenman/0/0-3xgi/1.jpg"
    )
    assert pages[-1].image_url.endswith("/3.jpg")
    assert all(p.referer is None for p in pages)


def test_parse_pages_skips_the_lazyload_placeholder() -> None:
    page = (
        '<img class="lazy-read" src="/template/pc/default/images/lazyload_img.png" '
        'data-original="https://s2.baozimh.com/a/1.jpg" alt="1.jpg">'
    )
    pages = _plugin()._parse_pages(page)

    assert [p.image_url for p in pages] == ["https://s2.baozimh.com/a/1.jpg"]


def test_parse_pages_without_images_returns_empty() -> None:
    assert _plugin()._parse_pages("<html><body>nothing</body></html>") == []


async def test_get_pages_fetches_the_chapter_url() -> None:
    fetcher = AsyncMock()
    fetcher.get_text.return_value = _fixture("mhua5_chapter.html")
    plugin = _plugin(fetcher)
    chapter = Chapter(source_id="mhua5", manga_ref="m", title="t", ref="/index.php/chapter/210986")

    pages = await plugin.get_pages(chapter)

    assert len(pages) == 3
    assert fetcher.get_text.await_args.args[0] == "https://www.mhua5.com/index.php/chapter/210986"


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_plugin_is_discovered_and_configured() -> None:
    registry = PluginRegistry(AppConfig())
    registry.discover()

    assert "mhua5" in registry.ids()
    assert registry.get("mhua5").base_url == "https://www.mhua5.com"

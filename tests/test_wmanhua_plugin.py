"""Tests for WManhuaPlugin."""

from __future__ import annotations

from unittest.mock import AsyncMock

from app.config import AppConfig
from app.core.fetcher import HttpFetcher
from app.core.models import Chapter, MangaDetail
from app.plugins.wmanhua import WManhuaPlugin, _clean_ref, _in_reading_order


def test_clean_ref() -> None:
    assert _clean_ref("1969") == "1969"
    assert _clean_ref("https://www.wmanhua.com/comic/1969.html") == "1969"
    assert _clean_ref("/comic/1969.html") == "1969"
    assert _clean_ref("https://www.wmanhua.com/chapter/1969-160013.html") == "1969-160013"
    assert _clean_ref("我是姐姐的玩具") == "我是姐姐的玩具"


async def test_wmanhua_plugin_registration(tmp_path) -> None:
    cfg = AppConfig(output_dir=str(tmp_path))
    fetcher = HttpFetcher(cfg)
    plugin = WManhuaPlugin(cfg, fetcher)

    assert plugin.id == "wmanhua"
    assert plugin.name == "W漫画 (wmanhua)"
    assert plugin.base_url == "https://www.wmanhua.com"
    await fetcher.aclose()


def test_parse_search() -> None:
    cfg = AppConfig()
    plugin = WManhuaPlugin(cfg, fetcher=None)  # type: ignore[arg-type]

    sample_html = """
    <div class="card-content">
        <a href="/comic/5348.html">
            <article class="card">
                <img class="lazy" data-src="https://image4.wmanhua.com/mh/e7341db3e85a47fcaa68b300400b7a7c/cover.webp" alt="我是姐姐的玩具" />
                <footer>
                    <h3 class="cardtitle">我是姐姐的玩具</h3>
                </footer>
            </article>
        </a>
    </div>
    """

    results = plugin._parse_search(sample_html)
    assert len(results) == 1
    assert results[0].source_id == "wmanhua"
    assert results[0].title == "我是姐姐的玩具"
    assert results[0].ref == "5348"
    assert (
        results[0].cover_url
        == "https://image4.wmanhua.com/mh/e7341db3e85a47fcaa68b300400b7a7c/cover.webp"
    )


async def test_list_chapters_json_api() -> None:
    cfg = AppConfig()
    mock_fetcher = AsyncMock()
    mock_fetcher.post_json.return_value = {
        "code": 0,
        "message": "success",
        "data": {
            "chapters": [
                {"id": 160013, "contentId": 1969, "chapterName": "26 我反手屠神使"},
                {"id": 160014, "contentId": 1969, "chapterName": "27 一二三木鱼人"},
            ]
        },
    }

    plugin = WManhuaPlugin(cfg, fetcher=mock_fetcher)
    detail = MangaDetail(source_id="wmanhua", title="Test Manga", ref="1969")
    chapters = await plugin.list_chapters(detail)

    assert len(chapters) == 2
    assert chapters[0].ref == "1969-160013"
    assert chapters[0].title == "26 我反手屠神使"
    assert chapters[1].ref == "1969-160014"
    assert chapters[1].title == "27 一二三木鱼人"


# ---------------------------------------------------------------------------
# chapter order
#
# The site's API returns `data.chapters` sorted by chapter id DESCENDING, i.e.
# the NEWEST chapter first. The plugin must hand back reading order, so that
# `index` 1 is the first chapter and the GUI list is not inverted.
# ---------------------------------------------------------------------------


def _api_payload(rows: list[tuple[int, str]], content_id: int = 1969) -> dict:
    return {
        "code": 0,
        "message": "success",
        "data": {
            "chapters": [
                {"id": cid, "contentId": content_id, "chapterName": name} for cid, name in rows
            ]
        },
    }


async def test_list_chapters_json_api_reverses_site_order() -> None:
    # Real API shape: newest chapter (highest id) first.
    mock_fetcher = AsyncMock()
    mock_fetcher.post_json.return_value = _api_payload(
        [
            (104723, "第150话：又见面了"),
            (104722, "第149话：光明会的真正目标"),
            (104721, "第148话：逃出的……？"),
            (104571, "预告"),
        ],
        content_id=1263,
    )
    plugin = WManhuaPlugin(AppConfig(), fetcher=mock_fetcher)
    detail = MangaDetail(source_id="wmanhua", title="Test Manga", ref="1263")

    chapters = await plugin.list_chapters(detail)

    assert [c.title for c in chapters] == [
        "预告",
        "第148话：逃出的……？",
        "第149话：光明会的真正目标",
        "第150话：又见面了",
    ]
    assert [c.index for c in chapters] == [1, 2, 3, 4]
    assert chapters[0].ref == "1263-104571"  # index 1 is the OLDEST chapter
    assert chapters[-1].ref == "1263-104723"  # newest chapter is last


async def test_list_chapters_is_idempotent_if_the_site_flips_order() -> None:
    # If the API ever hands the array back ascending, reading order must not
    # change — sorting (not reversing) keeps this correct either way.
    mock_fetcher = AsyncMock()
    mock_fetcher.post_json.return_value = _api_payload(
        [(104571, "预告"), (104721, "第148话"), (104722, "第149话")]
    )
    plugin = WManhuaPlugin(AppConfig(), fetcher=mock_fetcher)

    chapters = await plugin.list_chapters(MangaDetail(source_id="wmanhua", title="t", ref="1263"))
    assert [c.title for c in chapters] == ["预告", "第148话", "第149话"]


def test_in_reading_order_handles_missing_and_bad_ids() -> None:
    rows = [
        {"id": 30, "chapterName": "c"},
        {"id": "not-a-number", "chapterName": "b"},
        {"chapterName": "a"},
        {"id": 10, "chapterName": "d"},
    ]
    ordered = _in_reading_order(rows)
    assert [r["chapterName"] for r in ordered] == ["d", "c", "b", "a"]
    assert _in_reading_order([]) == []


async def test_list_chapters_html_fallback_is_reading_order() -> None:
    # The detail page also lists the newest chapters first; the fallback must
    # put them back in reading order too.
    mock_fetcher = AsyncMock()
    mock_fetcher.post_json.side_effect = RuntimeError("api down")
    mock_fetcher.get_text.return_value = """
        <div class="chapter-list">
            <a href="/chapter/1969-160014.html">27 一二三木鱼人</a>
            <a href="/chapter/1969-160013.html">26 我反手屠神使</a>
            <a href="/chapter/1969-160012.html">25 起点</a>
        </div>
    """

    plugin = WManhuaPlugin(AppConfig(), fetcher=mock_fetcher)
    chapters = await plugin.list_chapters(MangaDetail(source_id="wmanhua", title="t", ref="1969"))

    assert [c.title for c in chapters] == ["25 起点", "26 我反手屠神使", "27 一二三木鱼人"]
    assert [c.index for c in chapters] == [1, 2, 3]
    assert chapters[0].ref == "1969-160012"


async def test_get_pages() -> None:
    cfg = AppConfig()
    mock_fetcher = AsyncMock()
    mock_fetcher.get_text.return_value = """
    <script>
        var num = eval("3");
        var pasd = "https://image4.wmanhua.com/mh/test/hash/";
    </script>
    """

    plugin = WManhuaPlugin(cfg, fetcher=mock_fetcher)
    chapter = Chapter(source_id="wmanhua", manga_ref="1969", title="Ch 1", ref="1969-160013")
    pages = await plugin.get_pages(chapter)

    assert len(pages) == 3
    # 0-based: the writer names page 0 "001.webp". A 1-based index here shifted
    # every saved filename by one (a 93-page chapter wrote 002..094.webp).
    assert [p.index for p in pages] == [0, 1, 2]
    assert pages[0].image_url == "https://image4.wmanhua.com/mh/test/hash/1.webp"
    assert pages[1].image_url == "https://image4.wmanhua.com/mh/test/hash/2.webp"
    assert pages[2].image_url == "https://image4.wmanhua.com/mh/test/hash/3.webp"
    assert pages[0].referer == "https://www.wmanhua.com/"


async def test_get_pages_html_fallback_is_zero_based() -> None:
    """The <img>-scraping path must number pages from 0 too."""
    mock_fetcher = AsyncMock()
    mock_fetcher.get_text.return_value = """
    <div class="reader">
        <img data-src="https://image4.wmanhua.com/mh/x/a/1.webp" />
        <img data-src="https://image4.wmanhua.com/mh/x/a/2.webp" />
    </div>
    """
    plugin = WManhuaPlugin(AppConfig(), fetcher=mock_fetcher)
    chapter = Chapter(source_id="wmanhua", manga_ref="1263", title="第1话", ref="1263-1")

    pages = await plugin.get_pages(chapter)

    assert [p.index for p in pages] == [0, 1]

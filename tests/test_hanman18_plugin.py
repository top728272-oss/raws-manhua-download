"""Tests for the HanMan18 source plugin."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest

from app.config import AppConfig
from app.core.fetcher import HttpFetcher
from app.core.models import Chapter, SearchResult
from app.plugins.hanman18 import HanMan18Plugin, _clean_ref


async def test_hanman18_plugin_registration(tmp_path) -> None:
    cfg = AppConfig(output_dir=str(tmp_path))
    fetcher = HttpFetcher(cfg)
    plugin = HanMan18Plugin(cfg, fetcher)

    assert plugin.id == "hanman18"
    assert plugin.name == "HanMan18"
    assert plugin.category == "18+"
    assert plugin.base_url == "https://hanman18.com"
    assert plugin.image_base_url == "https://azminv2.hanman18.com"
    await fetcher.aclose()


def test_clean_ref() -> None:
    assert _clean_ref("lianaifuzhuqi") == "lianaifuzhuqi"
    assert _clean_ref("/manhwa/lianaifuzhuqi/") == "lianaifuzhuqi"
    assert _clean_ref("https://hanman18.com/manhwa/lianaifuzhuqi/110") == "lianaifuzhuqi"


def test_parse_search() -> None:
    plugin = HanMan18Plugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]
    html = """
    <div class="manga-item">
      <a href="/manhwa/lianaifuzhuqi">
        <img src="/uploads/manga/lianaifuzhuqi/cover/cover_250x350.jpg" alt="戀愛輔助器">
        <h3>戀愛輔助器</h3>
      </a>
    </div>
    <div class="manga-item">
      <a href="/manhwa/hunhunpipipaiqianyuan">
        <img src="https://cdn.example/cover.jpg" alt="混混痞痞 派遣員">
      </a>
    </div>
    """

    results = plugin._parse_search(html)
    assert len(results) == 2
    assert results[0].source_id == "hanman18"
    assert results[0].title == "戀愛輔助器"
    assert results[0].ref == "lianaifuzhuqi"
    assert (
        results[0].cover_url
        == "https://hanman18.com/uploads/manga/lianaifuzhuqi/cover/cover_250x350.jpg"
    )
    assert results[1].title == "混混痞痞 派遣員"


def test_parse_detail() -> None:
    plugin = HanMan18Plugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]
    html = """
    <title>戀愛輔助器 - hanman18.com</title>
    <meta name="title" content="戀愛輔助器 - hanman18.com">
    <meta name="description" content="秘密神器的故事">
    <meta property="og:image" content="https://img.example/cover.jpg">
    <a class="chapter_num" href="/manhwa/lianaifuzhuqi/2">第2話</a>
    <a class="chapter_num" href="/manhwa/lianaifuzhuqi/1">第1話</a>
    """
    result = SearchResult(source_id="hanman18", title="fallback", ref="lianaifuzhuqi")

    detail = plugin._parse_detail(html, result)
    assert detail.title == "戀愛輔助器"
    assert detail.description == "秘密神器的故事"
    assert detail.cover_url == "https://img.example/cover.jpg"
    assert detail.chapter_count == 2


def test_parse_chapters_reverses_newest_first_and_preserves_labels() -> None:
    plugin = HanMan18Plugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]
    html = """
    <a class="chapter_num" href="/manhwa/lianaifuzhuqi/126">外傳-第15話</a>
    <a class="chapter_num" href="/manhwa/lianaifuzhuqi/111">最終話</a>
    <a class="chapter_num" href="/manhwa/lianaifuzhuqi/110">第110話</a>
    <a class="chapter_num" href="/manhwa/lianaifuzhuqi/110">第110話</a>
    """

    chapters = plugin._parse_chapters(html, "lianaifuzhuqi")
    assert [(chapter.ref, chapter.title) for chapter in chapters] == [
        ("110", "第110話"),
        ("111", "最終話"),
        ("126", "外傳-第15話"),
    ]
    assert [chapter.index for chapter in chapters] == [1, 2, 3]


def test_parse_pages_decodes_ordered_images() -> None:
    plugin = HanMan18Plugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]
    urls = [
        f"https://azminv2.hanman18.com/uploads/chapters/29243/{index:02d}.jpg"
        for index in range(1, 35)
    ]
    encoded = ", ".join(f'"{base64.b64encode(url.encode()).decode()}"' for url in urls)
    pages = plugin._parse_pages(f"<script>var slides_p_path = [{encoded}];</script>")

    assert len(pages) == 34
    assert pages[0].index == 0
    assert pages[0].image_url.endswith("/01.jpg")
    assert pages[-1].index == 33
    assert pages[-1].image_url.endswith("/34.jpg")
    assert pages[0].referer == "https://hanman18.com/"


def test_parse_pages_rejects_missing_or_malformed_data() -> None:
    plugin = HanMan18Plugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="did not contain slides_p_path"):
        plugin._parse_pages("<html>no reader data</html>")
    with pytest.raises(ValueError, match="no valid image URLs"):
        plugin._parse_pages('var slides_p_path = ["not-base64"];')


async def test_get_pages_uses_chapter_href() -> None:
    fetcher = AsyncMock()
    fetcher.get_text.return_value = (
        'var slides_p_path = ["' + base64.b64encode(b"https://img.example/01.jpg").decode() + '"];'
    )
    plugin = HanMan18Plugin(AppConfig(), fetcher=fetcher)
    chapter = Chapter(
        source_id="hanman18",
        manga_ref="lianaifuzhuqi",
        title="第1話",
        ref="1",
        extra={"href": "/manhwa/lianaifuzhuqi/1"},
    )

    pages = await plugin.get_pages(chapter)
    assert pages[0].image_url == "https://img.example/01.jpg"
    fetcher.get_text.assert_awaited_once_with("https://hanman18.com/manhwa/lianaifuzhuqi/1")

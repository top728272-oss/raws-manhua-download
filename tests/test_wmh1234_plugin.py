"""Tests for Wmh1234Plugin."""

from __future__ import annotations

from unittest.mock import AsyncMock

from app.config import AppConfig
from app.core.fetcher import HttpFetcher
from app.core.models import Chapter, SearchResult
from app.plugins.wmh1234 import Wmh1234Plugin, _clean_ref


def test_clean_ref() -> None:
    assert _clean_ref("50665") == "50665"
    assert _clean_ref("https://www.wmh1234.com/comic/50665.html") == "50665"
    assert _clean_ref("/comic/50665.html") == "50665"
    assert _clean_ref("全书反派听见我心声后") == "全书反派听见我心声后"


async def test_wmh1234_plugin_registration(tmp_path) -> None:
    cfg = AppConfig(output_dir=str(tmp_path))
    fetcher = HttpFetcher(cfg)
    plugin = Wmh1234Plugin(cfg, fetcher)

    assert plugin.id == "wmh1234"
    assert plugin.name == "漫画1234 (wmh1234)"
    assert plugin.base_url == "https://www.wmh1234.com"
    assert plugin.reader_url == "https://reader.hqread.cc"
    await fetcher.aclose()


def test_parse_search() -> None:
    cfg = AppConfig()
    plugin = Wmh1234Plugin(cfg, fetcher=None)  # type: ignore[arg-type]

    sample_html = """
    <div class="comic-grid">
        <a href="/comic/50665.html" class="comic-card" title="全书反派听见我心声后">
            <div class="comic-cover-wrap">
                <img
                    data-original="https://wmh1234.wszwhg.net/fmtu/50665/cover.webp"
                    src="/bg.png"
                    alt="全书反派听见我心声后"
                    class="comic-cover lazy"
                >
            </div>
            <div class="comic-info">
                <h3 class="comic-title">全书反派听见我心声后</h3>
                <p class="comic-update">二三玖陆工作室</p>
            </div>
        </a>
    </div>
    """

    results = plugin._parse_search(sample_html)
    assert len(results) == 1
    assert results[0].source_id == "wmh1234"
    assert results[0].title == "全书反派听见我心声后"
    assert results[0].ref == "50665"
    assert results[0].cover_url == "https://wmh1234.wszwhg.net/fmtu/50665/cover.webp"
    assert results[0].author == "二三玖陆工作室"


def test_parse_detail() -> None:
    cfg = AppConfig()
    plugin = Wmh1234Plugin(cfg, fetcher=None)  # type: ignore[arg-type]

    sample_html = """
    <!DOCTYPE html>
    <html>
    <head>
      <title>全书反派听见我心声后漫画_二三玖陆工作室_漫画1234网</title>
      <meta property="og:title" content="全书反派听见我心声后_二三玖陆工作室_漫画1234网">
      <meta property="og:description" content="我是祝玉枝，五分钟前我穿书了...">
      <meta property="og:image" content="https://wmh1234.wszwhg.net/fmtu/50665/cover.webp">
      <meta property="book:author" content="二三玖陆工作室">
    </head>
    <body>
      <h1>全书反派听见我心声后漫画</h1>
    </body>
    </html>
    """

    res = SearchResult(source_id="wmh1234", title="全书反派听见我心声后", ref="50665")
    detail = plugin._parse_detail(sample_html, res)
    assert detail.title == "全书反派听见我心声后"
    assert detail.cover_url == "https://wmh1234.wszwhg.net/fmtu/50665/cover.webp"
    assert detail.author == "二三玖陆工作室"
    assert detail.description == "我是祝玉枝，五分钟前我穿书了..."


def test_parse_chapters() -> None:
    cfg = AppConfig()
    plugin = Wmh1234Plugin(cfg, fetcher=None)  # type: ignore[arg-type]

    sample_html = """
    <div class="chapters-list">
        <div class="chapter-item">
            <div class="chapter-info">
                <span class="chapter-number">第1话</span>
                <span class="chapter-title">001 苍天啊！天崩开局！</span>
            </div>
            <div class="chapter-meta">
                <a href="/go/NTA2NjUtMzcxODQ5MS04NmUzZDE1MjE0" class="chapter-action">阅读</a>
            </div>
        </div>
        <div class="chapter-item">
            <div class="chapter-info">
                <span class="chapter-number">第2话</span>
                <span class="chapter-title">002 世界是个巨大的传</span>
            </div>
            <div class="chapter-meta">
                <a href="/go/NTA2NjUtMzcxODQ5My02YzljNDQyMGVj" class="chapter-action">阅读</a>
            </div>
        </div>
    </div>
    """

    chapters = plugin._parse_chapters(sample_html, "50665")
    assert len(chapters) == 2
    assert chapters[0].ref == "NTA2NjUtMzcxODQ5MS04NmUzZDE1MjE0"
    assert chapters[0].title == "第1话 001 苍天啊！天崩开局！"
    assert chapters[1].ref == "NTA2NjUtMzcxODQ5My02YzljNDQyMGVj"
    assert chapters[1].title == "第2话 002 世界是个巨大的传"


async def test_get_pages() -> None:
    cfg = AppConfig()
    mock_fetcher = AsyncMock()
    mock_fetcher.get_text.return_value = """
    <div class="reader-container">
        <img
            src="/placeholder.svg"
            data-src="https://wmh1234.wszwhg.net/kantu/50665/3718491/1.webp"
            class="reader-image lazy"
        >
        <img
            src="/placeholder.svg"
            data-src="https://wmh1234.wszwhg.net/kantu/50665/3718491/2.webp"
            class="reader-image lazy"
        >
    </div>
    """

    plugin = Wmh1234Plugin(cfg, fetcher=mock_fetcher)
    token = "NTA2NjUtMzcxODQ5MS04NmUzZDE1MjE0"
    chapter = Chapter(source_id="wmh1234", manga_ref="50665", title="Ch 1", ref=token)
    pages = await plugin.get_pages(chapter)

    assert len(pages) == 2
    assert pages[0].image_url == "https://wmh1234.wszwhg.net/kantu/50665/3718491/1.webp"
    assert pages[1].image_url == "https://wmh1234.wszwhg.net/kantu/50665/3718491/2.webp"
    assert pages[0].referer == "https://reader.hqread.cc/"

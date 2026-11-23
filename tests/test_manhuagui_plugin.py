"""Tests for the ManhuaGui source plugin."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import app.plugins.manhuagui as manhuagui_module
from app.config import AppConfig
from app.core.fetcher import HttpFetcher
from app.core.models import Chapter, SearchResult
from app.plugins.manhuagui import ManhuaGuiPlugin, _clean_ref

FIXTURES = Path(__file__).parent / "fixtures"


async def test_manhuagui_plugin_registration(tmp_path) -> None:
    cfg = AppConfig(output_dir=str(tmp_path))
    fetcher = HttpFetcher(cfg)
    plugin = ManhuaGuiPlugin(cfg, fetcher)

    assert plugin.id == "manhuagui"
    assert plugin.name == "ManhuaGui"
    assert plugin.category == "General"
    assert plugin.base_url == "https://www.manhuagui.com"
    assert plugin.image_host == "https://eu1.hamreus.com"
    await fetcher.aclose()


def test_clean_ref() -> None:
    assert _clean_ref("4694") == "4694"
    assert _clean_ref("/comic/4694/") == "4694"
    assert _clean_ref("https://www.manhuagui.com/comic/4694/155818.html") == "4694"


def test_parse_search() -> None:
    plugin = ManhuaGuiPlugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]
    html = """
    <div class="book-result"><ul><li class="cf">
      <div class="book-cover"><a class="bcover" href="/comic/4694/" title="第一神拳">
        <img src="//cf.mhgui.com/cpic/b/4694.jpg" alt="第一神拳">
      </a></div>
      <div class="book-detail"><dl>
        <dt><a title="第一神拳" href="/comic/4694/">第一神拳</a></dt>
        <dd class="tags status">状态：<span class="red">连载中</span></dd>
        <dd class="tags">作者：<a href="/author/863/">森川让次</a></dd>
      </dl></div>
    </li></ul></div>
    """

    results = plugin._parse_search(html)
    assert len(results) == 1
    assert results[0].title == "第一神拳"
    assert results[0].ref == "4694"
    assert results[0].author == "森川让次"
    assert results[0].cover_url == "https://cf.mhgui.com/cpic/b/4694.jpg"


def test_parse_detail_and_chapters() -> None:
    plugin = ManhuaGuiPlugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]
    html = """
    <title>第一神拳漫画_Hajime no Ippo漫画_森川让次 - 看漫画</title>
    <meta name="description" content="原本弱小的一步为了变强而开始练拳。">
    <h1><a href="/comic/4694/">第一神拳</a></h1>
    <dd class="tags status">状态：<span class="red">连载中</span></dd>
    <dd class="tags">作者：<a href="/author/863/">森川让次</a></dd>
    <div id="chapter-list-1">
      <a href="/comic/4694/155818.html" title="第105卷"><span>第105卷<i>193p</i></span></a>
      <a href="/comic/4694/40676.html" title="开始阅读">开始阅读</a>
      <a href="/comic/4694/155818.html" title="第105卷">重复</a>
      <a href="/comic/4694/873998.html" title="第144卷"><span>第144卷</span></a>
    </div>
    """
    result = SearchResult(source_id="manhuagui", title="fallback", ref="4694")

    detail = plugin._parse_detail(html, result)
    assert detail.title == "第一神拳"
    assert detail.description == "原本弱小的一步为了变强而开始练拳。"
    assert detail.cover_url == "https://cf.mhgui.com/cpic/h/4694.jpg"
    assert detail.author == "森川让次"
    assert detail.status == "连载中"
    assert detail.chapter_count == 3

    chapters = plugin._chapter_links(html, "4694")
    assert [(ref, title, pages) for ref, title, pages in chapters] == [
        ("40676", "开始阅读", None),
        ("155818", "第105卷", 193),
        ("873998", "第144卷", None),
    ]


def test_parse_captured_reader_pages(monkeypatch) -> None:
    monkeypatch.setattr("time.time", lambda: 1787000000)
    plugin = ManhuaGuiPlugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]
    html = (FIXTURES / "manhuagui_reader_105.html").read_text(encoding="utf-8")

    pages = plugin._parse_pages(html)

    assert len(pages) == 193
    assert pages[0].index == 0
    assert pages[0].image_url == (
        "https://eu1.hamreus.com/ps1/d/dysq/105/SK_105_000.jpg.webp"
        "?e=1787664599&m=mtJjvsXj5m3pQBCS80JREQ"
    )
    assert pages[1].image_url.endswith(
        "/SK_105_000_P.jpg.webp?e=1787664599&m=mtJjvsXj5m3pQBCS80JREQ"
    )
    assert pages[-1].image_url.endswith(
        "/SK_105_096b.png.webp?e=1787664599&m=mtJjvsXj5m3pQBCS80JREQ"
    )
    assert pages[0].referer == "https://www.manhuagui.com/"


def test_parse_newer_reader_wrapper_variant(monkeypatch) -> None:
    monkeypatch.setattr("time.time", lambda: 1787000000)
    plugin = ManhuaGuiPlugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]
    html = (FIXTURES / "manhuagui_reader_225789.html").read_text(encoding="utf-8")

    pages = plugin._parse_pages(html)

    assert len(pages) == 22
    assert pages[0].image_url.endswith(
        "/14443788780372.jpg.webp?e=1787190126&m=PVJnbJvf7EVdnjZ8nst0Xw"
    )
    assert pages[-1].image_url.endswith(
        "/14443788886282.jpg.webp?e=1787190126&m=PVJnbJvf7EVdnjZ8nst0Xw"
    )


def test_parse_pages_rejects_malformed_reader_data() -> None:
    plugin = ManhuaGuiPlugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="missing its unpacker"):
        plugin._parse_pages("<html>no reader data</html>")
    with pytest.raises(ValueError, match="invalid pack arguments"):
        plugin._parse_pages("function(p,a,c,k,e,d){return p;}('bad');")


@pytest.mark.parametrize(
    ("reader_data", "message"),
    [
        ({"files": [], "path": "/x/", "sl": {"e": 1787664599, "m": "sig"}}, "no image files"),
        ({"files": ["page.jpg"], "path": "", "sl": {"e": 1787664599, "m": "sig"}}, "no image path"),
        ({"files": ["page.jpg"], "path": "/x/", "sl": {}}, "no image signature"),
        ({"files": ["page.jpg"], "path": "/x/", "sl": {"e": 1, "m": "sig"}}, "signature expired"),
    ],
)
def test_parse_pages_rejects_empty_or_invalid_reader_fields(
    monkeypatch, reader_data: dict, message: str
) -> None:
    plugin = ManhuaGuiPlugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]
    monkeypatch.setattr(manhuagui_module, "_unpack_reader_script", lambda _html: reader_data)

    with pytest.raises(ValueError, match=message):
        plugin._parse_pages("ignored")


async def test_get_pages_uses_chapter_href(monkeypatch) -> None:
    monkeypatch.setattr("time.time", lambda: 1787000000)
    fetcher = AsyncMock()
    fetcher.get_text.return_value = (FIXTURES / "manhuagui_reader_105.html").read_text(
        encoding="utf-8"
    )
    plugin = ManhuaGuiPlugin(AppConfig(), fetcher=fetcher)
    chapter = Chapter(
        source_id="manhuagui",
        manga_ref="4694",
        title="第105卷",
        ref="155818",
        extra={"href": "/comic/4694/155818.html"},
    )

    pages = await plugin.get_pages(chapter)

    assert len(pages) == 193
    fetcher.get_text.assert_awaited_once_with("https://www.manhuagui.com/comic/4694/155818.html")

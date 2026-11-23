from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.config import AppConfig
from app.core.fetcher import HttpFetcher
from app.core.models import Chapter, MangaDetail
from app.core.registry import PluginRegistry
from app.plugins.dumanwu import (
    DumanwuPlugin,
    _clean_ref,
    _decrypt_c0rst,
    _unpack_dean_edwards,
)

SAMPLE_SEARCH_HTML = """
<div class="view-body">
    <div class="view-boxs">
        <div class="view-item">
            <div class="item-title">
                <span>搜索与“<font>开局有剑域</font>” 相关 <font>1</font> 条结果<span>
            </div>
            <div class="item-data s-data">
                <div class="itemnar">
                    <div class="bookBak">
                        <div class="book">
                            <a href="/OjWxWWo/"><img src="/static/images/load.gif" data-src="https://p6.ecombdimg.com/tos-cn-i-scl3phc04j/ffb304f5a9bb4de8b6bff759e93613c7~tplv-scl3phc04j-image.jpeg" alt="开局有剑域，我能苟成剑神" title="开局有剑域，我能苟成剑神"><span class="msg">第245话 谜底？电视机？</span></a>
                        </div>
                    </div>
                    <p class="title">
                        <a href="/OjWxWWo/" title="开局有剑域，我能苟成剑神">开局有剑域，我能苟成剑神</a>
                    </p>
                    <p class="tip"></p>
                </div>
            </div>
        </div>
    </div>
</div>
"""

SAMPLE_DETAIL_HTML = """
<!DOCTYPE>
<html>
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
<title>开局有剑域，我能苟成剑神_漫画最新章节免费阅读(下拉式) - 读漫屋</title>
<meta itemprop="url" property="og:url" content="https://www.dumanwu1.com/OjWxWWo/" />
<meta itemprop="name" property="og:title" content="开局有剑域，我能苟成剑神_漫画最新章节免费阅读(下拉式) - 读漫屋" />
<meta itemprop="image" property="og:image" content="https://p6.ecombdimg.com/tos-cn-i-scl3phc04j/ffb304f5a9bb4de8b6bff759e93613c7~tplv-scl3phc04j-image.jpeg"/>
<meta itemprop="actor" property="og:author" content="星灵动漫" />
<meta itemprop="description" property="og:description" content="到异世界的张凡，好不容易成为修士..." />
</head>
<body>
    <div class="view-body">
        <div class="forminfo">
            <div class="comicInfo">
                <div class="mhcover">
                    <div class="himg">
                        <img src="/static/images/load.gif" data-src="https://p6.ecombdimg.com/tos-cn-i-scl3phc04j/ffb304f5a9bb4de8b6bff759e93613c7~tplv-scl3phc04j-image.jpeg" title="开局有剑域，我能苟成剑神">
                    </div>
                </div>
                <div class="detinfo">
                    <h1 class="name_mh">开局有剑域，我能苟成剑神</h1>
                    <p><span>作  者：  星灵动漫</span><span>更新时间：07月17日</span></p>
                    <p class="gray"><span>标  签： </span><span>状  态： 连载中</span></p>
                    <p class="content">到异世界的张凡，好不容易成为修士...</p>
                </div>
            </div>
            <div class="chapterList">
                <div class="chapterlistload">
                    <ul>
                        <a href="/OjWxWWo/XczVQcuf.html"><li>第245话 谜底？电视机？</li></a>
                        <a href="/OjWxWWo/CzaVaamY.html"><li>第244话 智力问答</li></a>
                    </ul>
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""


async def test_dumanwu_plugin_registration(tmp_path: Path) -> None:
    cfg = AppConfig(output_dir=str(tmp_path))
    fetcher = HttpFetcher(cfg)
    plugin = DumanwuPlugin(cfg, fetcher)

    assert plugin.id == "dumanwu"
    assert plugin.name == "读漫屋 (dumanwu)"
    assert plugin.category == "General"
    assert plugin.base_url == "https://www.dumanwu.com"
    await fetcher.aclose()


def test_dumanwu_plugin_discovery() -> None:
    cfg = AppConfig()
    registry = PluginRegistry(cfg)
    registry.discover()

    assert "dumanwu" in registry.ids()
    plugin = registry.get("dumanwu")
    assert isinstance(plugin, DumanwuPlugin)


def test_clean_ref() -> None:
    assert _clean_ref("https://www.dumanwu.com/OjWxWWo/") == "OjWxWWo"
    assert _clean_ref("/OjWxWWo/GGJMMNNe.html") == "OjWxWWo"
    assert _clean_ref("OjWxWWo") == "OjWxWWo"
    # regression: rstrip(".html") used to eat ids ending in h/t/m/l (404 on trKrwGl)
    assert _clean_ref("https://www.dumanwu.com/trKrwGl/") == "trKrwGl"
    assert _clean_ref("trKrwGl") == "trKrwGl"
    assert _clean_ref("GGJMMNNel.html") == "GGJMMNNel"


def test_parse_search() -> None:
    cfg = AppConfig()
    plugin = DumanwuPlugin(cfg, fetcher=None)  # type: ignore[arg-type]

    results = plugin._parse_search(SAMPLE_SEARCH_HTML)
    assert len(results) == 1
    assert results[0].source_id == "dumanwu"
    assert results[0].title == "开局有剑域，我能苟成剑神"
    assert results[0].ref == "OjWxWWo"
    assert (
        results[0].cover_url
        == "https://p6.ecombdimg.com/tos-cn-i-scl3phc04j/ffb304f5a9bb4de8b6bff759e93613c7~tplv-scl3phc04j-image.jpeg"
    )


async def test_search_direct_ref() -> None:
    cfg = AppConfig()
    mock_fetcher = AsyncMock()
    mock_fetcher.get_text.return_value = SAMPLE_DETAIL_HTML

    plugin = DumanwuPlugin(cfg, fetcher=mock_fetcher)
    results = await plugin.search("https://www.dumanwu.com/OjWxWWo/")

    assert len(results) == 1
    assert results[0].ref == "OjWxWWo"
    assert results[0].title == "开局有剑域，我能苟成剑神"


def test_parse_detail() -> None:
    cfg = AppConfig()
    plugin = DumanwuPlugin(cfg, fetcher=None)  # type: ignore[arg-type]

    detail = plugin._parse_detail(SAMPLE_DETAIL_HTML, "OjWxWWo")
    assert detail.source_id == "dumanwu"
    assert detail.title == "开局有剑域，我能苟成剑神"
    assert detail.ref == "OjWxWWo"
    assert detail.author == "星灵动漫"
    assert detail.status == "连载中"
    assert (
        detail.cover_url
        == "https://p6.ecombdimg.com/tos-cn-i-scl3phc04j/ffb304f5a9bb4de8b6bff759e93613c7~tplv-scl3phc04j-image.jpeg"
    )


async def test_list_chapters_with_morechapter_merge() -> None:
    cfg = AppConfig()
    mock_fetcher = AsyncMock()
    mock_fetcher.post_json.return_value = {
        "code": "200",
        "data": [
            {"chapterid": "OuuNNuxo", "chaptername": "第243话 大妖的考验"},
            {"chapterid": "OOJMCxCS", "chaptername": "第242话 道德绑架，不得不去"},
        ],
    }

    plugin = DumanwuPlugin(cfg, fetcher=mock_fetcher)
    detail = MangaDetail(
        source_id="dumanwu",
        title="开局有剑域，我能苟成剑神",
        ref="OjWxWWo",
        extra={"detail_html": SAMPLE_DETAIL_HTML},
    )

    chapters = await plugin.list_chapters(detail)

    # Initial HTML had 245, 244 (descending); morechapter added 243, 242 (descending)
    # Reversing for reading order yields 242 -> 243 -> 244 -> 245
    assert len(chapters) == 4
    assert chapters[0].ref == "OOJMCxCS"
    assert chapters[0].title == "第242话 道德绑架，不得不去"
    assert chapters[0].index == 1

    assert chapters[1].ref == "OuuNNuxo"
    assert chapters[1].title == "第243话 大妖的考验"
    assert chapters[1].index == 2

    assert chapters[2].ref == "CzaVaamY"
    assert chapters[2].title == "第244话 智力问答"
    assert chapters[2].index == 3

    assert chapters[3].ref == "XczVQcuf"
    assert chapters[3].title == "第245话 谜底？电视机？"
    assert chapters[3].index == 4


async def test_get_pages_from_har_capture() -> None:
    har_path = Path(__file__).resolve().parent.parent / ".examples" / "www.dumanwu.com.har"
    if not har_path.exists():
        pytest.skip(".examples/www.dumanwu.com.har not found")

    with open(har_path, encoding="utf-8") as f:
        har_data = json.load(f)

    # Entry 49 is chapter page
    chapter_entry = har_data["log"]["entries"][49]
    chapter_html = chapter_entry["response"]["content"]["text"]

    cfg = AppConfig()
    mock_fetcher = AsyncMock()
    mock_fetcher.get_text.return_value = chapter_html

    plugin = DumanwuPlugin(cfg, fetcher=mock_fetcher)
    chapter = Chapter(
        source_id="dumanwu",
        manga_ref="OjWxWWo",
        title="第240话 你动动嘴，我跑断腿",
        ref="GGJMMNNe",
    )

    pages = await plugin.get_pages(chapter)

    assert len(pages) == 108
    assert pages[0].index == 0
    assert pages[0].image_url.startswith(
        "https://p3-zhuxiaobang-sign.shimolife.com/tos-cn-i-0813/ogDmEdeGQI0LMV91c8weFeJeeQzRcIZALAAhQA"
    )
    assert pages[0].referer == "https://www.dumanwu.com/"
    assert pages[-1].index == 107
    assert pages[-1].image_url.startswith(
        "https://p9-zhuxiaobang-sign.shimolife.com/tos-cn-i-0813/oEe9F1MF1wGeGePIE8LZDIAyEeA2QAQ1eRAQWR"
    )


def test_decrypt_c0rst_fallback() -> None:
    har_path = Path(__file__).resolve().parent.parent / ".examples" / "www.dumanwu.com.har"
    if not har_path.exists():
        pytest.skip(".examples/www.dumanwu.com.har not found")

    with open(har_path, encoding="utf-8") as f:
        har_data = json.load(f)

    chapter_html = har_data["log"]["entries"][49]["response"]["content"]["text"]
    unpacked = _unpack_dean_edwards(chapter_html)
    import re

    m = re.search(r'var __c0rst96\s*=\s*"([^"]+)"', unpacked)
    assert m is not None
    raw_payload = m.group(1)

    # Fallback with None
    images_none = _decrypt_c0rst(raw_payload, key_idx=None)
    assert len(images_none) == 108

    # Fallback with incorrect key index
    images_wrong_idx = _decrypt_c0rst(raw_payload, key_idx=0)
    assert len(images_wrong_idx) == 108


async def test_list_chapters_skips_start_reading_button() -> None:
    """Regression: the 开始阅读 button points at chapter 1 and must not be
    treated as a chapter, nor block chapter 1's real entry from /morechapter.
    Mirrors https://www.dumanwu.com/DjRvRRq/ (我在魔界当俘虏)."""
    cfg = AppConfig()
    mock_fetcher = AsyncMock()
    mock_fetcher.post_json.return_value = {
        "code": "200",
        "data": [
            {"chapterid": "CH003", "chaptername": "03 筹码"},
            {"chapterid": "CH002", "chaptername": "02 特殊待遇?"},
            {"chapterid": "CH001", "chaptername": "01 起点"},
        ],
    }

    plugin = DumanwuPlugin(cfg, fetcher=mock_fetcher)
    html = """
    <html><body>
        <a href="/DjRvRRq/CH001.html">开始阅读</a>
        <div class="chapterlistload"><ul>
            <a href="/DjRvRRq/CH005.html"><li>05 我答应你！</li></a>
            <a href="/DjRvRRq/CH004.html"><li>04 特殊的酸液！</li></a>
            <a href="/DjRvRRq/CH003.html"><li>03 筹码</li></a>
        </ul></div>
        <a href="/DjRvRRq/more.html">更多话点这里</a>
    </body></html>
    """
    detail = MangaDetail(
        source_id="dumanwu",
        title="我在魔界当俘虏",
        ref="DjRvRRq",
        extra={"detail_html": html},
    )

    chapters = await plugin.list_chapters(detail)

    assert [ch.title for ch in chapters] == [
        "01 起点",
        "02 特殊待遇?",
        "03 筹码",
        "04 特殊的酸液！",
        "05 我答应你！",
    ]
    assert [ch.index for ch in chapters] == [1, 2, 3, 4, 5]
    assert all(ch.title != "开始阅读" for ch in chapters)
    assert chapters[0].ref == "CH001"


async def test_list_chapters_falls_back_to_reversal_without_numbers() -> None:
    cfg = AppConfig()
    mock_fetcher = AsyncMock()
    mock_fetcher.post_json.return_value = {"code": "404"}

    plugin = DumanwuPlugin(cfg, fetcher=mock_fetcher)
    html = """
    <html><body><div class="chapterlistload"><ul>
        <a href="/AbCdEf/ZZZ.html"><li>结局</li></a>
        <a href="/AbCdEf/AAA.html"><li>开端</li></a>
    </ul></div></body></html>
    """
    detail = MangaDetail(
        source_id="dumanwu",
        title="无编号漫画",
        ref="AbCdEf",
        extra={"detail_html": html},
    )

    chapters = await plugin.list_chapters(detail)

    assert [ch.title for ch in chapters] == ["开端", "结局"]
    assert [ch.index for ch in chapters] == [1, 2]

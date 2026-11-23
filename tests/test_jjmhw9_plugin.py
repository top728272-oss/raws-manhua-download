"""Tests for the 漫小肆韓漫 (jjmhw9.top) source plugin.

Fixtures mirror the markup captured in `scratch/www.jjmhw9.top *.har` and the
live pages so a template change shows up as a test failure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.config import AppConfig
from app.core.fetcher import HttpFetcher
from app.core.models import Chapter, SearchResult
from app.plugins.jjmhw9 import Jjmhw9Plugin, _clean_ref, _query_relevant

COVER_1068 = "https://www.jjmhw9.top/static/upload/book/1068/cover.jpg"

SEARCH_HTML = f"""
<ul class="mh-list col7">
  <li>
    <div class="mh-item">
      <a href="/book/1068" title="民宿精營中">
        <p class="mh-cover" style="background-image: url({COVER_1068})"></p>
      </a>
      <div class="mh-item-detali">
        <h2 class="title"><a href="/book/1068" title="民宿精營中">民宿精營中</a></h2>
        <p class="chapter" style="width: 181px;">
          <span>更新时间:</span><a href="/book/1068">2026-10-03</a>
        </p>
      </div>
    </div>
  </li>
  <li>
    <div class="mh-item">
      <a href="/book/145" title="甜蜜假期">
        <p class="mh-cover" style="background-image: url(/static/upload/book/145/cover.jpg)"></p>
      </a>
      <div class="mh-item-detali">
        <h2 class="title"><a href="/book/145" title="甜蜜假期">甜蜜假期</a></h2>
      </div>
    </div>
  </li>
  <li>
    <div class="mh-item">
      <a href="/book/1143" title="迷霧深處的誘惑">
        <p class="mh-cover" style="background-image: url(/static/upload/book/1143/cover.jpg)"></p>
      </a>
      <div class="mh-item-detali">
        <h2 class="title"><a href="/book/1143" title="迷霧深處的誘惑">迷霧深處的誘惑</a></h2>
      </div>
    </div>
  </li>
</ul>
"""

DETAIL_HTML = f"""
<div class="banner_detail_form">
  <div class="cover">
    <img src="{COVER_1068}" alt="民宿精營中">
    <div class="sr-qrcode"><img src="/static/images/wxqrcode.jpg" alt="民宿精營中"></div>
  </div>
  <div class="info">
    <h1>民宿精營中</h1>
    <p class="subtitle">别名：民宿精营中</p>
    <p class="subtitle">作者：SUN&amp;林巨星</p>
    <p class="tip">
      <span class="block">状态：<span>连载中</span></span>
      <span class="block ticai">地区：<a href="/booklist/?area=1">韩国</a></span>
      <span class="block">更新时间：2026-10-03</span>
    </p>
    <p class="tip">
      <span class="block">标签：<a href="/booklist/?tag=都市">都市</a></span>
    </p>
    <p class="content">剛退伍的林博齊遇到很頭大的問題&hearts;</p>
    <div class="bottom">
      <a href="/login" class="btn-1 btn_collection">收藏</a>
      <a href="/chapter/48313" class="btn-2">开始阅读</a>
    </div>
  </div>
</div>
<div class="detail-list-title" id="detail-list-title">
  <a href="javascript:void(0);" onclick="sortBtnClick();">倒序</a>
</div>
<div id="chapterlistload">
  <ul class="view-win-list detail-list-select" id="detail-list-select">
    <li><a href="/chapter/48313" rel="nofollow">第1話-醫生，幫我看看這邊好嗎?</a></li>
    <li><a href="/chapter/48314" rel="nofollow">第2話-性感的繼母和繼妹</a></li>
    <li><a href="/chapter/58359" rel="nofollow">第78話-我怎麼硬不起來了?</a></li>
  </ul>
</div>
<div class="index-title"><h2>相关推荐</h2></div>
<div class="index-manga">
  <ul class="mh-list col7">
    <li>
      <div class="mh-item">
        <a href="/book/2" title="純友誼">
          <p class="mh-cover" style="background-image: url({COVER_1068})"></p>
        </a>
      </div>
    </li>
  </ul>
</div>
"""

READER_HTML = """
<div class="chapter-content">
  <div class="img-content">
    <img class="lazy" data-original="/static/upload/book/1068/55738/4566524.jpg">
    <img class="lazy" data-original="/static/upload/book/1068/55738/4566525.jpg">
    <img class="lazy" data-original="/static/upload/book/1068/55738/4566526.jpg">
  </div>
</div>
<div class="qrcode"><img src="/static/images/wxqrcode.jpg"></div>
"""


async def test_jjmhw9_plugin_registration(tmp_path) -> None:
    cfg = AppConfig(output_dir=str(tmp_path))
    fetcher = HttpFetcher(cfg)
    plugin = Jjmhw9Plugin(cfg, fetcher)

    assert plugin.id == "jjmhw9"
    assert plugin.name == "漫小肆韓漫 (18+)"
    assert plugin.category == "18+"
    assert plugin.base_url == "https://www.jjmhw9.top"
    await fetcher.aclose()


def test_clean_ref() -> None:
    assert _clean_ref("1068") == "1068"
    assert _clean_ref("/book/1068") == "1068"
    assert _clean_ref("https://www.jjmhw9.top/book/1068") == "1068"
    assert _clean_ref("民宿精營中") == "民宿精營中"


def test_query_relevant_filters_fuzzy_junk() -> None:
    # Exact and traditional/simplified variants pass.
    assert _query_relevant("民宿精營中", "民宿精營中")
    assert _query_relevant("秘密教学", "秘密教學")
    # The live garbage query `zzqqwwxx1122` returned these unrelated titles.
    for junk in ("償不盡的債", "樓鳳", "無人島之主", "戀愛大富翁"):
        assert not _query_relevant("zzqqwwxx1122", junk)
    # Sub-two-character queries are accepted as-is.
    assert _query_relevant("我", "樓鳳")


def test_parse_search_dedupes_and_filters() -> None:
    plugin = Jjmhw9Plugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]

    results = plugin._parse_search(SEARCH_HTML, "民宿")
    assert [r.ref for r in results] == ["1068"]
    assert results[0].title == "民宿精營中"
    assert (
        results[0].cover_url
        == "https://www.jjmhw9.top/static/upload/book/1068/cover.jpg"
    )

    # No query -> everything, covers absolutised, still one entry per book.
    all_results = plugin._parse_search(SEARCH_HTML)
    assert [r.ref for r in all_results] == ["1068", "145", "1143"]
    assert all_results[1].cover_url == "https://www.jjmhw9.top/static/upload/book/145/cover.jpg"


def test_parse_detail() -> None:
    plugin = Jjmhw9Plugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]
    result = SearchResult(source_id="jjmhw9", title="fallback", ref="1068")

    detail = plugin._parse_detail(DETAIL_HTML, result)
    assert detail.title == "民宿精營中"
    assert detail.author == "SUN&林巨星"
    assert detail.cover_url == "https://www.jjmhw9.top/static/upload/book/1068/cover.jpg"
    assert detail.status == "连载中"
    assert detail.tags == ["都市"]
    assert detail.description == "剛退伍的林博齊遇到很頭大的問題♥"
    assert detail.chapter_count == 3


def test_parse_chapters_is_id_ordered_and_skips_start_button() -> None:
    plugin = Jjmhw9Plugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]

    chapters = plugin._parse_chapters(DETAIL_HTML, "1068")
    assert [(c.ref, c.title) for c in chapters] == [
        ("48313", "第1話-醫生，幫我看看這邊好嗎?"),
        ("48314", "第2話-性感的繼母和繼妹"),
        ("58359", "第78話-我怎麼硬不起來了?"),
    ]
    # 1-based reading order per the SourcePlugin contract, contiguous, and the
    # 开始阅读 button did not duplicate chapter 1.
    assert [c.index for c in chapters] == [1, 2, 3]


def test_parse_chapters_normalises_newest_first_markup() -> None:
    plugin = Jjmhw9Plugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]
    html = """
    <ul id="detail-list-select">
        <li><a href="/chapter/58359">最終話</a></li>
        <li><a href="/chapter/48313">第1話</a></li>
        <li><a href="/chapter/48314">第2話</a></li>
    </ul>
    """
    chapters = plugin._parse_chapters(html, "1068")
    assert [c.ref for c in chapters] == ["48313", "48314", "58359"]


def test_parse_pages_ordered_and_absolute() -> None:
    plugin = Jjmhw9Plugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]

    pages = plugin._parse_pages(READER_HTML)
    assert [p.index for p in pages] == [0, 1, 2]
    assert pages[0].image_url.endswith("/book/1068/55738/4566524.jpg")
    assert pages[2].image_url == "https://www.jjmhw9.top/static/upload/book/1068/55738/4566526.jpg"
    assert pages[0].referer == "https://www.jjmhw9.top/"
    # The reader's QR-code <img src> must not be mistaken for a page.
    assert all("wxqrcode" not in p.image_url for p in pages)


async def test_get_pages_uses_chapter_ref() -> None:
    fetcher = AsyncMock()
    fetcher.get_text.return_value = READER_HTML
    plugin = Jjmhw9Plugin(AppConfig(), fetcher=fetcher)
    chapter = Chapter(source_id="jjmhw9", manga_ref="1068", title="第61話", ref="55738")

    pages = await plugin.get_pages(chapter)
    assert len(pages) == 3
    fetcher.get_text.assert_awaited_once_with("https://www.jjmhw9.top/chapter/55738")


@pytest.mark.asyncio
async def test_search_uses_keyword_endpoint() -> None:
    fetcher = AsyncMock()
    fetcher.get_text.return_value = SEARCH_HTML
    plugin = Jjmhw9Plugin(AppConfig(), fetcher=fetcher)

    results = await plugin.search("民宿")
    assert [r.ref for r in results] == ["1068"]
    fetcher.get_text.assert_awaited_once_with(
        "https://www.jjmhw9.top/search?keyword=%E6%B0%91%E5%AE%BF"
    )

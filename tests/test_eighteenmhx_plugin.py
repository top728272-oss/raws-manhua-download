"""Tests for 18MHX plugin parsing & registration.

Fixtures mirror the live Astro markup (captured 2026-10-04) so a template change
shows up as a failure. The three regressions covered here:

* `/chapter/getcontent` needs `c=<data-cs>`, not the chapter href (href -> 404).
* Chapter order comes from the href's trailing index, **not** `data-cs`
  (an upload counter that misorders chapters).
* The real title is in `<title>`, not the first `<h1>` — that one is the
  age-gate modal's "警告！".
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from app.config import AppConfig
from app.core.fetcher import HttpFetcher
from app.core.models import Chapter, SearchResult
from app.plugins.eighteenmhx import EighteenMHXPlugin

SITE = "https://m.18mhx.com"
COVER = "https://host-cover.mangabuddy.in/mhc"
S3 = "https://s3-nl-01.mangabuddy.in/mxs/mishudeqianguize/0"
PLACEHOLDER = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=="

SEARCH_HTML = f"""
<div class="md:grid-cols-6 grid-cols-3 cardlist">
  <div class="pb-2">
    <a href="{SITE}/manga/mishudeqianguize">
      <div class="text-center">
        <img src="{COVER}/mishudeqianguize/620AC94A42E1w300h420.webp" alt="mishudeqianguize">
        <h3 class="cardtitle">秘書的潛規則</h3>
      </div>
    </a>
  </div>
  <div class="pb-2">
    <a href="{SITE}/manga/gegedemishu">
      <div class="text-center">
        <img src="{COVER}/gegedemishu/BC9766D8D5A5w300h420.webp" alt="gegedemishu">
        <h3 class="cardtitle">哥哥的秘書</h3>
      </div>
    </a>
  </div>
</div>
"""

DETAIL_HTML = f"""
<title>秘書的潛規則-18漫畫</title>
<meta property="og:title" content="秘書的潛規則-18漫畫 - 18漫畫">
<meta name="description" content="最終話 勝哲是一間世界最大歡趣用品店的新進員工。">
<meta property="og:image" content="{COVER}/mishudeqianguize/620AC94A42E1w300h420.webp">
<div class="flex justify-center p-3"><h1 class="text-sm">警告！</h1></div>
<h1 class="mb-2 text-xl font-medium"> 秘書的潛規則
  <span class="text-xs px-2.5 bg-yellow-100"> 完結 </span> </h1>
<div id="allchapterlist" class="chapterlists" data-mid="34">
  <div class="chapteritem" data-index="0">
    <a href="/manga/mishudeqianguize/34-5979-0" data-ms="34" data-cs="35781" data-ct="第1話">
  </div>
  <div class="chapteritem" data-index="1">
    <a href="/manga/mishudeqianguize/34-5999-1" data-ms="34" data-cs="35783" data-ct="第2話">
  </div>
  <div class="chapteritem" data-index="2">
    <a href="/manga/mishudeqianguize/34-6019-2" data-ms="34" data-cs="35782" data-ct="第3話">
  </div>
</div>
"""

CONTENT_HTML = f"""
<div class="container">
  <img src="{S3}/0_145972.webp" width="768" height="768" alt="0 第1話">
  <img data-sizes="auto" data-src="{S3}/1_145988.webp" alt="1 第1話"
       width="768" height="768" src="{PLACEHOLDER}" class="lazyload">
  <img alt="1 第1話" width="768" height="768" src="{S3}/1_145988.webp" loading="lazy">
  <img data-sizes="auto" data-src="{S3}/2_146021.webp" alt="2 第1話"
       width="768" height="768" src="{PLACEHOLDER}" class="lazyload">
  <img alt="2 第1話" width="768" height="768" src="{S3}/2_146021.webp" loading="lazy">
</div>
"""


async def test_eighteenmhx_plugin_registration(tmp_path):
    cfg = AppConfig(output_dir=str(tmp_path))
    fetcher = HttpFetcher(cfg)
    plugin = EighteenMHXPlugin(cfg, fetcher)

    assert plugin.id == "eighteenmhx"
    assert plugin.name == "18MHX (18+)"
    assert plugin.category == "18+"
    assert plugin.base_url == "https://m.18mhx.com"
    await fetcher.aclose()


def test_parse_search():
    plugin = EighteenMHXPlugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]

    results = plugin._parse_search(SEARCH_HTML)
    assert [r.ref for r in results] == ["mishudeqianguize", "gegedemishu"]
    assert results[0].source_id == "eighteenmhx"
    assert results[0].title == "秘書的潛規則"
    assert (
        results[0].cover_url
        == "https://host-cover.mangabuddy.in/mhc/mishudeqianguize/620AC94A42E1w300h420.webp"
    )


def test_parse_detail_ignores_age_gate_h1():
    plugin = EighteenMHXPlugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]
    result = SearchResult(source_id="eighteenmhx", title="fallback", ref="mishudeqianguize")

    detail = plugin._parse_detail(DETAIL_HTML, result, mid="34")

    # The page's first <h1> is the age-gate modal's "警告！" — never the title.
    assert detail.title == "秘書的潛規則"
    assert detail.status == "完結"
    assert detail.ref == "mishudeqianguize"
    assert detail.extra["mid"] == "34"
    assert detail.description.startswith("最終話 勝哲")


def test_parse_chapters_orders_by_href_index_not_data_cs():
    """`data-cs` is an upload counter; the href tail is the reading position."""
    plugin = EighteenMHXPlugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]

    chapters = plugin._parse_chapters(DETAIL_HTML, "mishudeqianguize", mid="34")

    # data-cs order would be 第1話, 第3話, 第2話 (35781, 35782, 35783) — wrong.
    assert [c.title for c in chapters] == ["第1話", "第2話", "第3話"]
    assert [c.index for c in chapters] == [1, 2, 3]
    assert [c.extra["c"] for c in chapters] == ["35781", "35783", "35782"]
    assert [c.extra["mid"] for c in chapters] == ["34", "34", "34"]
    assert chapters[0].ref == "/manga/mishudeqianguize/34-5979-0"


def test_parse_chapters_falls_back_to_document_order():
    """A href without a trailing index must not scramble the whole list."""
    plugin = EighteenMHXPlugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]
    html = """
    <a href="/manga/x/9-1-0" data-cs="500" data-ct="A">
    <a href="/manga/x/9-2" data-cs="100" data-ct="B">
    <a href="/manga/x/9-3-2" data-cs="300" data-ct="C">
    """

    chapters = plugin._parse_chapters(html, "x", mid="9")
    assert [c.title for c in chapters] == ["A", "B", "C"]


async def test_get_pages_uses_data_cs_not_href() -> None:
    """`c` must be the chapter's `data-cs`; the href 404s."""
    fetcher = AsyncMock()
    fetcher.get_text.return_value = CONTENT_HTML
    plugin = EighteenMHXPlugin(AppConfig(), fetcher=fetcher)
    chapter = Chapter(
        source_id="eighteenmhx",
        manga_ref="mishudeqianguize",
        title="第97話",
        ref="/manga/mishudeqianguize/34-18306-96",
        extra={"mid": "34", "c": "35880"},
    )

    pages = await plugin.get_pages(chapter)

    fetcher.get_text.assert_awaited_once_with(
        "https://m.18mhx.com/chapter/getcontent?m=34&c=35880"
    )
    # 0-based, deduped, and ordered — each page appears as a lazyload wrapper
    # plus a real <img>, so a naive scan would emit every page twice.
    assert [p.index for p in pages] == [0, 1, 2]
    assert pages[0].image_url.endswith("/0/0_145972.webp")
    assert pages[-1].image_url.endswith("/0/2_146021.webp")
    assert all(not p.image_url.startswith("data:") for p in pages)
    assert pages[0].referer == "https://m.18mhx.com/"


def test_parse_pages_prefers_data_src_over_placeholder() -> None:
    plugin = EighteenMHXPlugin(AppConfig(), fetcher=None)  # type: ignore[arg-type]

    pages = plugin._parse_pages(CONTENT_HTML)
    assert [p.image_url for p in pages] == [
        "https://s3-nl-01.mangabuddy.in/mxs/mishudeqianguize/0/0_145972.webp",
        "https://s3-nl-01.mangabuddy.in/mxs/mishudeqianguize/0/1_145988.webp",
        "https://s3-nl-01.mangabuddy.in/mxs/mishudeqianguize/0/2_146021.webp",
    ]

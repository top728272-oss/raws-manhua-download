"""No-hit search regression tests.

The old plugins scanned the WHOLE no-results page and turned nav/sidebar/
recommendation links into bogus SearchResults. After the scoping fix, a
no-hits page (which still contains those links) must parse to [].
"""

from __future__ import annotations

from app.config import AppConfig
from app.core.htmlutil import (
    cut_recommendations,
    is_placeholder_cover,
    split_on_container,
    strip_chrome,
)

# ---------------------------------------------------------------- htmlutil --


def test_strip_chrome_removes_chrome_blocks() -> None:
    html = (
        "<html><nav><a href='/manga/x'>nav</a></nav>"
        "<div class='content'><a href='/manga/y'>keep</a></div>"
        "<aside><a href='/manga/z'>aside</a></aside>"
        "<script>var x=1;</script><!-- comment -->"
        "<footer><a href='/manga/f'>footer</a></footer></html>"
    )
    cleaned = strip_chrome(html)
    assert "/manga/x" not in cleaned
    assert "/manga/z" not in cleaned
    assert "/manga/f" not in cleaned
    assert "/manga/y" in cleaned
    assert "comment" not in cleaned


def test_split_on_container_nesting_safe() -> None:
    html = (
        "<nav>chrome</nav>"
        "<div class='card outer'><div class='inner'>a</div></div>"
        "<div class='card other'><div class='inner'>b</div></div>"
    )
    chunks = split_on_container(html, "card")
    assert len(chunks) == 2
    assert "chrome" not in chunks[0]
    assert "a" in chunks[0] and "b" in chunks[1]


def test_is_placeholder_cover() -> None:
    assert is_placeholder_cover("/static/images/load.gif")
    assert is_placeholder_cover("https://site.com/static/logo.png")
    assert is_placeholder_cover("/images/placeholder.png")
    assert not is_placeholder_cover("https://cdn.site.com/cover/abc.jpg")
    assert not is_placeholder_cover(None)


# ------------------------------------------------------------ plugin parsers --

# A realistic no-results page: chrome + recommendation links that the old
# fallback regexes would happily match. Must yield ZERO results everywhere.


def _no_hits_page() -> str:
    return """
<html><head><title>搜索结果 - 没有找到</title></head><body>
<nav><a href="/manga/navcomic"><img src="/img/nav.jpg" alt="NavComic"></a></nav>
<header><a href="/book/12345"><h3>推荐漫画</h3></a></header>
<aside><a href="/comic/999.html">热门推荐</a></aside>
<div class="recommend"><a href="/comics/randomcomic">随机漫画</a></div>
<footer><a href="/comic.php?id=777">友情链接</a></footer>
<div class="empty">没有找到相关漫画</div>
</body></html>
"""


def _plugin(module_name: str, class_name: str):
    import importlib

    module = importlib.import_module(f"app.plugins.{module_name}")
    cls = getattr(module, class_name)
    return cls(AppConfig(), fetcher=None)  # type: ignore[arg-type]


def test_guazimanhua_no_hits() -> None:
    plugin = _plugin("guazimanhua", "GuaziManhuaPlugin")
    html = _no_hits_page().replace("/comic.php?id=777", "/comic.php?id=777")
    html += "<a href='/comic.php?id=888'>More links</a>"
    assert plugin._parse_search(html) == []


def test_manhuafree_no_hits() -> None:
    plugin = _plugin("manhuafree", "ManhuaFreePlugin")
    assert plugin._parse_search(_no_hits_page()) == []


def test_comicbox_no_hits() -> None:
    plugin = _plugin("comicbox", "ComicBoxPlugin")
    assert plugin._parse_search(_no_hits_page()) == []


def test_eighteenmhx_no_hits() -> None:
    plugin = _plugin("eighteenmhx", "EighteenMHXPlugin")
    assert plugin._parse_search(_no_hits_page()) == []


def test_hanman18_no_hits() -> None:
    plugin = _plugin("hanman18", "HanMan18Plugin")
    html = _no_hits_page()
    html += "<aside><a href='/manhwa/secret'><h3>Secret Comic</h3></a></aside>"
    assert plugin._parse_search(html) == []


def test_wmanhua_no_hits() -> None:
    plugin = _plugin("wmanhua", "WManhuaPlugin")
    html = _no_hits_page()
    html += "<aside><a href='/comic/4321.html'>Sidebar Comic</a></aside>"
    assert plugin._parse_search(html) == []


def test_wmh1234_no_hits() -> None:
    plugin = _plugin("wmh1234", "Wmh1234Plugin")
    html = _no_hits_page()
    html += "<aside><a href='/comic/4321.html'>Sidebar Comic</a></aside>"
    assert plugin._parse_search(html) == []


def test_one99mh_no_hits() -> None:
    plugin = _plugin("one99mh", "One99MhPlugin")
    html = _no_hits_page()
    html += "<a href='/comics/randomslug'>Random Comic</a>"
    assert plugin._parse_search(html) == []


def test_manwang_no_hits() -> None:
    plugin = _plugin("manwang", "ManWangPlugin")
    html = _no_hits_page()
    html += "<a href='/book/98765'>Sidebar Book</a>"
    assert plugin._parse_search(html) == []


def test_manhuagui_no_hits() -> None:
    plugin = _plugin("manhuagui", "ManhuaGuiPlugin")
    html = _no_hits_page()
    html += "<a href='/comic/555/'>Sidebar Comic</a>"
    assert plugin._parse_search(html) == []


def test_tibiu_html_fallback_no_hits() -> None:
    plugin = _plugin("tibiu", "TibiuPlugin")
    html = _no_hits_page()
    html += "<aside><a href='/comic/555'>Sidebar Comic</a></aside>"
    assert plugin._parse_search_html(html) == []


async def test_tibiu_empty_json_does_not_fall_back_to_html() -> None:
    from app.plugins.tibiu import TibiuPlugin

    class StubFetcher:
        def __init__(self) -> None:
            self.text_calls: list[str] = []

        async def get_json(self, url: str):
            return {"data": []}

        async def get_text(self, url: str):
            self.text_calls.append(url)
            return "<html><body><a href='/comic/1'>leak</a></body></html>"

    stub = StubFetcher()
    plugin = TibiuPlugin(AppConfig(), stub)  # type: ignore[arg-type]
    results = await plugin.search("zzqqwwxx")
    assert results == []
    # An empty-but-successful API response must NOT trigger the HTML fallback.
    assert stub.text_calls == []


# ------------------------------------------------------------------ dumanwu --

_SOFT200_HTML = """
<html><head>
<meta property="og:title" content="Error - 读漫屋">
<meta property="og:image" content="https://p6.ecombdimg.com/fake.jpg">
</head><body><div>页面不存在</div></body></html>
"""

_REAL_DETAIL_HTML = """
<html><head>
<meta property="og:novel:book_name" content="开局有剑域，我能苟成剑神">
<meta property="og:image" content="https://p6.ecombdimg.com/real.jpg">
</head><body><h1 class="name_mh">开局有剑域，我能苟成剑神</h1></body></html>
"""


class _DumanwuStubFetcher:
    def __init__(self, direct_html: str, keyword_html: str = "") -> None:
        self._direct_html = direct_html
        self._keyword_html = keyword_html
        self.keyword_calls: list[str] = []

    async def get_text(self, url: str, **kwargs) -> str:
        if url.rstrip("/").endswith("/s") or "k=" in url:
            self.keyword_calls.append(url)
            return self._keyword_html
        return self._direct_html

    async def post_text(self, url: str, data=None, headers=None) -> str:
        self.keyword_calls.append(url)
        return self._keyword_html


async def test_dumanwu_garbage_word_falls_through_to_keyword() -> None:
    from app.plugins.dumanwu import DumanwuPlugin

    stub = _DumanwuStubFetcher(direct_html=_SOFT200_HTML, keyword_html="<html></html>")
    plugin = DumanwuPlugin(AppConfig(), stub)  # type: ignore[arg-type]
    results = await plugin.search("zzqqwwxx")
    assert results == []
    assert stub.keyword_calls, "garbage word must fall through to keyword search"


async def test_dumanwu_bad_url_returns_empty_without_keyword() -> None:
    from app.plugins.dumanwu import DumanwuPlugin

    stub = _DumanwuStubFetcher(direct_html=_SOFT200_HTML, keyword_html="<html></html>")
    plugin = DumanwuPlugin(AppConfig(), stub)  # type: ignore[arg-type]
    results = await plugin.search("https://www.dumanwu.com/badslug/")
    assert results == []
    assert not stub.keyword_calls, "pasted URL must not trigger keyword search"


async def test_dumanwu_direct_url_with_identity_still_resolves() -> None:
    from app.plugins.dumanwu import DumanwuPlugin

    stub = _DumanwuStubFetcher(direct_html=_REAL_DETAIL_HTML)
    plugin = DumanwuPlugin(AppConfig(), stub)  # type: ignore[arg-type]
    results = await plugin.search("https://www.dumanwu.com/trKrwGl/")
    assert len(results) == 1
    assert results[0].title == "开局有剑域，我能苟成剑神"
    assert results[0].ref == "trKrwGl"
    assert results[0].cover_url == "https://p6.ecombdimg.com/real.jpg"


# ------------------------------------------- recommendation-section cutting --


def test_cut_recommendations() -> None:
    html = "<div>real results</div><h2>您可能喜歡</h2><div>junk</div>"
    cut = cut_recommendations(html)
    assert "real results" in cut
    assert "junk" not in cut
    assert cut_recommendations("<div>no marker here</div>") == "<div>no marker here</div>"


def test_eighteenmhx_plain_div_recommendations_are_cut() -> None:
    plugin = _plugin("eighteenmhx", "EighteenMHXPlugin")
    html = (
        "<div class='results'></div>"
        "<h2>暫無漫畫，請試試搜尋其他關鍵字</h2>"
        "<h2>您可能喜歡</h2>"
        "<div class='cardlist'>"
        "<a href='https://m.18mhx.com/manga/leak'><h3>Leak Comic</h3></a>"
        "</div>"
    )
    assert plugin._parse_search(html) == []


def test_manhuafree_plain_div_recommendations_are_cut() -> None:
    plugin = _plugin("manhuafree", "ManhuaFreePlugin")
    html = (
        "<div class='main'></div><h2>您可能喜歡</h2>"
        "<div><a href='/manga/leak'><img src='/c.jpg' alt='Leak Comic'></a></div>"
    )
    assert plugin._parse_search(html) == []


def test_wmh1234_recommendation_section_is_cut() -> None:
    plugin = _plugin("wmh1234", "Wmh1234Plugin")
    html = (
        "<div class='search-results'></div><h3>为您推荐</h3>"
        "<div class='comic-grid'><a href='/comic/12366.html' title='X'>"
        "<h3 class='comic-title'>少帅你老婆又跑了</h3></a></div>"
    )
    assert plugin._parse_search(html) == []


# ---------------------------------------------------- comicbox fuzzy engine --


def test_comicbox_fuzzy_junk_is_filtered() -> None:
    from app.plugins.comicbox import _query_relevant

    assert not _query_relevant("zzqqwwxx", "miko酱ww - NIKKE：胜利女神 海伦礼服")
    assert not _query_relevant("zzqqwwxx", "魔法少女的XXX課")
    assert not _query_relevant("zzqqwwxx", "[zero戦(xxzero)] 绝对!山羊还没做")  # only z,x
    assert _query_relevant("秘密教學", "秘密教學")
    assert _query_relevant("秘密教学", "秘密教學")  # simplified/traditional variant
    assert not _query_relevant("秘密教學", "御姐的實戰教學")  # 2/4 coverage only
    assert _query_relevant("a", "anything")  # short query accepted as-is

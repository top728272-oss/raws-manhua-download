"""Tests for PufeimhPlugin (扑飞漫画, www.pufeimh.com).

Fixtures are trimmed captures of the live site (see
``scratch/build_new_site_fixtures.py``); ``pufeimh_chapter.html`` carries a small
payload encrypted by the site's own CryptoJS build so the AES path is pinned
against genuine output rather than our own round-trip.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.config import AppConfig
from app.core.models import Chapter, MangaDetail, SearchResult
from app.core.registry import PluginRegistry
from app.plugins.pufeimh import (
    PufeimhPlugin,
    _comic_slug,
    decode_params_blob,
    unwrap_image_url,
)

FIXTURES = Path(__file__).parent / "fixtures"

REAL_BLOB_URL = "https://c-nd3-1.6wm.top/coco/8522/example-22815/0_2.webp"
_PROXY_SEGMENT = (
    "aHR0cHM6Ly9jLW5kMy0xLjZ3bS50b3AvY29jby84NTIyL2V4YW1wbGUtMjI4MTUvMF8yLndlYnA="
)
_SLUG = "shuohuangdexiaogouhuibeichidiaode"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _plugin(fetcher=None) -> PufeimhPlugin:
    return PufeimhPlugin(AppConfig(), fetcher or AsyncMock())


# ---------------------------------------------------------------------------
# params decryption
# ---------------------------------------------------------------------------


def _params_blob(name: str = "pufeimh_chapter.html") -> str:
    return _fixture(name).split("params = '")[1].split("'")[0]


def test_decode_params_blob_decrypts_cryptojs_payload() -> None:
    payload = decode_params_blob(_params_blob())

    assert payload["host"] == "www.pufeimh.com"
    assert payload["comic_id"] == "563672"
    assert payload["chapter_id"] == "192739"
    assert payload["images"] == [
        "https://c-nd3-1.6wm.top/coco/8522/example-22815/0_1.webp",
        "https://s2.325784.xyz/"
        "aHR0cHM6Ly9jLW5kMy0xLjZ3bS50b3AvY29jby84NTIyL2V4YW1wbGUtMjI4MTUvMF8yLndlYnA=",
    ]


@pytest.mark.parametrize("blob", ["", "not base64!!", "QUJD", "YWJj"])
def test_decode_params_blob_rejects_malformed_input(blob: str) -> None:
    with pytest.raises(ValueError):
        decode_params_blob(blob)


def test_decode_params_blob_rejects_wrong_key() -> None:
    # A payload that is valid base64 with a plausible length but not our scheme.
    blob = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="
    with pytest.raises(ValueError):
        decode_params_blob(blob)


# ---------------------------------------------------------------------------
# image URL unwrapping
# ---------------------------------------------------------------------------


def test_unwrap_image_url_decodes_wrapper_and_drops_referer() -> None:
    wrapped = "https://s2.325784.xyz/" + _PROXY_SEGMENT
    url, needs_referer = unwrap_image_url(wrapped, ("s2.325784.xyz",))

    assert url == REAL_BLOB_URL
    assert needs_referer is False


def test_unwrap_image_url_leaves_plain_cdn_url_referer_free() -> None:
    # Regression: the origin CDN answers a Referer-carrying request with a 301 to
    # a placeholder image, so a plain CDN URL must never be given one.
    url, needs_referer = unwrap_image_url(REAL_BLOB_URL, ("s2.325784.xyz",))

    assert url == REAL_BLOB_URL
    assert needs_referer is False


def test_unwrap_image_url_flags_unresolved_wrapper_host() -> None:
    # Wrapper host, but the path segment is not decodable base64 -> keep the URL
    # and ask for the site Referer, which is what the wrapper needs.
    url, needs_referer = unwrap_image_url("https://s2.325784.xyz/%2Fbroken", ("s2.325784.xyz",))

    assert url == "https://s2.325784.xyz/%2Fbroken"
    assert needs_referer is True


def test_unwrap_image_url_ignores_non_http_entries() -> None:
    assert unwrap_image_url("data:image/png;base64,AAAA", ("s2.325784.xyz",)) == (
        "data:image/png;base64,AAAA",
        False,
    )


# ---------------------------------------------------------------------------
# ref normalisation
# ---------------------------------------------------------------------------


def test_comic_slug() -> None:
    slug = "shuohuangdexiaogouhuibeichidiaode"
    assert _comic_slug(slug) == slug
    assert _comic_slug(f"https://www.pufeimh.com/comic/{slug}") == slug
    assert _comic_slug(f"/comic/{slug}") == slug
    assert _comic_slug(f"/comic/{slug}?from=search") == slug
    assert _comic_slug("") == ""


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_parse_search_returns_single_hit() -> None:
    results = _plugin()._parse_search(_fixture("pufeimh_search.html"))

    assert [r.ref for r in results] == ["shuohuangdexiaogouhuibeichidiaode"]
    assert results[0].title == "说谎的小狗会被吃掉的"
    assert results[0].cover_url == (
        "https://www.pufeimh.com/attachment/comic/2025/10/05/202510050926467021.webp"
    )


def test_parse_search_returns_every_hit() -> None:
    results = _plugin()._parse_search(_fixture("pufeimh_search_multi.html"))

    assert len(results) == 20
    assert len({r.ref for r in results}) == 20
    assert all(r.title and r.cover_url for r in results)


def test_parse_search_ignores_embedded_js_template() -> None:
    """A no-hit page embeds a JS string that looks like a result card."""
    assert _plugin()._parse_search(_fixture("pufeimh_search_miss.html")) == []


def test_parse_search_without_container_returns_empty() -> None:
    assert _plugin()._parse_search("<html><body>nope</body></html>") == []


async def test_search_queries_mobile_host_with_mobile_user_agent() -> None:
    fetcher = AsyncMock()
    fetcher.get_text.return_value = _fixture("pufeimh_search.html")
    plugin = _plugin(fetcher)

    results = await plugin.search("说谎的小狗")

    assert len(results) == 1
    url = fetcher.get_text.await_args.args[0]
    assert url.startswith("https://m.pufeimh.com/search?key=")
    ua = fetcher.get_text.await_args.kwargs["headers"]["User-Agent"]
    assert "iPhone" in ua


async def test_search_retries_while_the_site_reports_busy() -> None:
    """The endpoint throttles with HTTP 200 + {"msg":"搜索繁忙","code":-1}."""
    fetcher = AsyncMock()
    fetcher.get_text.side_effect = [
        '{"msg":"搜索繁忙","code":-1}',
        '{"msg":"搜索繁忙","code":-1}',
        _fixture("pufeimh_search.html"),
    ]
    plugin = _plugin(fetcher)
    plugin._search_retry_delay = 0  # keep the test fast

    results = await plugin.search("说谎的小狗")

    assert len(results) == 1
    assert fetcher.get_text.await_count == 3


async def test_search_gives_up_after_repeated_throttling() -> None:
    fetcher = AsyncMock()
    fetcher.get_text.return_value = '{"msg":"搜索繁忙","code":-1}'
    plugin = _plugin(fetcher)
    plugin._search_retry_delay = 0

    assert await plugin.search("说谎的小狗") == []
    assert fetcher.get_text.await_count == 3


async def test_search_returns_empty_when_request_fails() -> None:
    fetcher = AsyncMock()
    fetcher.get_text.side_effect = RuntimeError("boom")

    assert await _plugin(fetcher).search("说谎的小狗") == []


async def test_search_with_comic_url_resolves_directly() -> None:
    fetcher = AsyncMock()
    fetcher.get_text.return_value = _fixture("pufeimh_comic.html")
    plugin = _plugin(fetcher)

    results = await plugin.search("https://www.pufeimh.com/comic/shuohuangdexiaogouhuibeichidiaode")

    assert [r.title for r in results] == ["说谎的小狗会被吃掉的"]
    assert results[0].ref == "shuohuangdexiaogouhuibeichidiaode"


async def test_search_with_chapter_url_resolves_owning_comic() -> None:
    fetcher = AsyncMock()
    # The chapter page exposes the comic via its 目录 link.
    fetcher.get_text.side_effect = [
        '<a href="/comic/shuohuangdexiaogouhuibeichidiaode" class="J_catalog_button">目录</a>',
        _fixture("pufeimh_comic.html"),
    ]
    plugin = _plugin(fetcher)

    results = await plugin.search("https://www.pufeimh.com/chapter/563672-192739.html")

    assert [r.ref for r in results] == ["shuohuangdexiaogouhuibeichidiaode"]


async def test_search_with_blank_query_does_not_hit_the_network() -> None:
    fetcher = AsyncMock()

    assert await _plugin(fetcher).search("   ") == []
    fetcher.get_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------


def test_parse_detail() -> None:
    detail = _plugin()._parse_detail(
        _fixture("pufeimh_comic.html"),
        SearchResult(source_id="pufeimh", title="ignored", ref="shuohuangdexiaogouhuibeichidiaode"),
        "shuohuangdexiaogouhuibeichidiaode",
    )

    assert detail.title == "说谎的小狗会被吃掉的"
    assert detail.author == "_Px_"
    assert detail.status == "连载中"
    assert detail.tags == ["纯爱", "原创"]
    assert detail.cover_url == (
        "https://www.pufeimh.com/attachment/comic/2025/10/05/202510050926467021.webp"
    )
    assert detail.description and detail.description.startswith("明明是杂食动物")


def test_parse_detail_falls_back_to_search_cover() -> None:
    detail = _plugin()._parse_detail(
        "<html><body><h1>nothing here</h1></body></html>",
        SearchResult(source_id="pufeimh", title="fallback title", ref="x", cover_url="https://c/x.webp"),
        "x",
    )

    assert detail.title == "fallback title"
    assert detail.cover_url == "https://c/x.webp"


# ---------------------------------------------------------------------------
# chapters
# ---------------------------------------------------------------------------


def test_parse_chapters_uses_the_complete_list_in_reading_order() -> None:
    chapters = _plugin()._parse_chapters(_fixture("pufeimh_comic.html"), _SLUG)

    # The page also carries `chapterlistload1` (the newest 8, newest first). The
    # parser must read `chapterlistload` instead, or it would return 8 items.
    assert len(chapters) == 90
    assert chapters[0].title == "第1话 我是狼哦！"
    assert chapters[0].ref == "/chapter/563672-192739.html"
    assert chapters[-1].title == "第72话 心里装不下那老些人"
    assert chapters[-1].ref == "/chapter/563672-214621.html"

    assert [c.index for c in chapters] == list(range(1, 91))
    assert len({c.ref for c in chapters}) == 90
    assert chapters[0].extra == {"mid": "563672", "cid": "192739"}


def test_parse_chapters_without_list_returns_empty() -> None:
    assert _plugin()._parse_chapters("<html></html>", "x") == []


async def test_list_chapters_reuses_stored_detail_html() -> None:
    fetcher = AsyncMock()
    plugin = _plugin(fetcher)
    detail = MangaDetail(
        source_id="pufeimh",
        title="t",
        ref="r",
        extra={"detail_html": _fixture("pufeimh_comic.html")},
    )

    chapters = await plugin.list_chapters(detail)

    assert len(chapters) == 90
    fetcher.get_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


def test_parse_pages_unwraps_and_orders_images() -> None:
    pages = _plugin()._parse_pages(_fixture("pufeimh_chapter.html"))

    assert [p.index for p in pages] == [0, 1]
    assert [p.image_url for p in pages] == [
        "https://c-nd3-1.6wm.top/coco/8522/example-22815/0_1.webp",
        REAL_BLOB_URL,
    ]
    # Never send a Referer to the origin CDN: it redirects to a placeholder.
    assert all(p.referer is None for p in pages)


def test_parse_pages_without_blob_returns_empty() -> None:
    assert _plugin()._parse_pages("<html><body>no blob</body></html>") == []


def test_parse_pages_with_undecryptable_blob_returns_empty() -> None:
    blob = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="
    assert _plugin()._parse_pages(f"<script>var params = '{blob}';</script>") == []


async def test_get_pages_fetches_the_chapter_url() -> None:
    fetcher = AsyncMock()
    fetcher.get_text.return_value = _fixture("pufeimh_chapter.html")
    plugin = _plugin(fetcher)
    chapter = Chapter(
        source_id="pufeimh", manga_ref="m", title="t", ref="/chapter/563672-192739.html"
    )

    pages = await plugin.get_pages(chapter)

    assert len(pages) == 2
    assert fetcher.get_text.await_args.args[0] == (
        "https://www.pufeimh.com/chapter/563672-192739.html"
    )


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_plugin_is_discovered_and_configured() -> None:
    registry = PluginRegistry(AppConfig())
    registry.discover()

    assert "pufeimh" in registry.ids()
    assert registry.get("pufeimh").base_url == "https://www.pufeimh.com"

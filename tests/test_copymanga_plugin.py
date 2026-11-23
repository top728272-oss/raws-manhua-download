"""Tests for CopyMangaPlugin (拷貝漫畫).

The load-bearing behaviours under test:
  * app-only hosts are never contacted (they answer code:210 without the
    Android app's signature) and `code:210` is walked past, not surfaced;
  * `words[]` reorders `contents[]` exactly like the shipped client
    (fumiama/copymanga `Downloader.kt::getMangaUrls`), and is a no-op when absent;
  * bootstrap keeps only web hosts even though network2 lists app hosts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.config import AppConfig
from app.core.fetcher import HttpFetcher
from app.core.models import Chapter, MangaDetail, SearchResult
from app.core.registry import PluginRegistry
from app.plugins.copymanga import (
    BOOTSTRAP_HOSTS,
    WEB_UA,
    AppHostBlocked,
    CopyMangaError,
    CopyMangaPlugin,
    _clean_ref,
    _httpify,
    _is_app_host,
    _join_authors,
    _normalise_host,
    _parse_network,
    _reorder,
    _set_width,
    _unwrap,
)

FIXTURES = Path(__file__).parent / "fixtures"
HAR = Path(__file__).parent.parent / "HTTPToolkit_2026-09-27_12-29.har"
HOST = "https://mapi.hotmangasg.com"
PW = "yongzhechuxingchengfayongzheduifuxingjilu"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _plugin(cfg: AppConfig | None = None, fetcher=None, booted: bool = True) -> CopyMangaPlugin:
    plugin = CopyMangaPlugin(cfg or AppConfig(), fetcher)  # type: ignore[arg-type]
    if booted:
        plugin._booted = True  # noqa: SLF001 - skip the network bootstrap in unit tests
        plugin._hosts = [HOST]  # noqa: SLF001
    return plugin


def _num(url: str) -> int:
    return int(re.search(r"/(\d+)\.(?:png|jpg)", url).group(1))


# ---------------------------------------------------------------------------
# ref cleaning / small helpers
# ---------------------------------------------------------------------------


def test_clean_ref() -> None:
    assert _clean_ref(f"https://www.mangacopy.com/comic/{PW}") == PW
    assert _clean_ref(f"https://www.copy4000.com/comic/{PW}/") == PW
    assert _clean_ref(f"pw:{PW}") == PW
    assert _clean_ref(f"id:{PW}") == PW
    assert _clean_ref("勇者處刑") == "勇者處刑"  # a bare query is NOT a ref
    assert _clean_ref("  ") == ""
    # a non-comic URL must NOT be mistaken for a ref (it degrades to a search)
    assert (
        _clean_ref("https://www.mangacopy.com/search?q=x") == "https://www.mangacopy.com/search?q=x"
    )
    assert _clean_ref("https://www.mangacopy.com/") == "https://www.mangacopy.com/"


def test_is_app_host_and_normalise() -> None:
    assert _is_app_host("https://api.copy202601.com")
    assert _is_app_host("https://api.copy202602.com")
    assert _is_app_host("https://api.copy3000.com")
    assert not _is_app_host("https://mapi.hotmangasg.com")
    assert not _is_app_host("https://api.2024manga.com")

    assert _normalise_host("mapi.hotmangasg.com") == "https://mapi.hotmangasg.com"
    assert _normalise_host("https://x.com/") == "https://x.com"
    assert _normalise_host("") == ""


def test_httpify() -> None:
    assert _httpify("http://sy.mangafunb.fun/a.jpg") == "https://sy.mangafunb.fun/a.jpg"
    assert _httpify("https://sy.mangafunb.fun/a.jpg") == "https://sy.mangafunb.fun/a.jpg"
    assert _httpify(None) is None
    assert _httpify("  ") is None


def test_join_authors() -> None:
    assert _join_authors([{"name": "A"}, {"name": "B"}]) == "A, B"
    assert _join_authors([{"alias": "C"}]) == "C"
    assert _join_authors("solo") == "solo"
    assert _join_authors([]) is None
    assert _join_authors(None) is None
    assert _join_authors([{"nope": 1}, "D"]) == "D"


def test_set_width() -> None:
    assert _set_width("https://x/y/1.jpg.h1500x.webp", 2000) == "https://x/y/1.jpg.h2000x.webp"
    assert _set_width("https://x/y/1.png.c1500x.webp", 800) == "https://x/y/1.png.c800x.webp"
    # no size hint -> unchanged
    assert _set_width("https://x/y/1.jpg", 2000) == "https://x/y/1.jpg"


# ---------------------------------------------------------------------------
# words[] ordering
# ---------------------------------------------------------------------------


def test_reorder_no_words_keeps_contents_order() -> None:
    contents = ["a", "b", "c"]
    assert _reorder(contents, None) == contents
    assert _reorder(contents, []) == contents


def test_reorder_applies_words_permutation() -> None:
    # by_pos[words[i]] = contents[i]; page[i] = by_pos[i]
    # words [2,0,4,1,3] -> output indices [1,3,0,4,2]
    contents = ["a", "b", "c", "d", "e"]
    assert _reorder(contents, [2, 0, 4, 1, 3]) == ["b", "d", "a", "e", "c"]


def test_reorder_identity_words_is_noop() -> None:
    contents = ["a", "b", "c"]
    assert _reorder(contents, [0, 1, 2]) == contents


def test_reorder_pads_short_words() -> None:
    # words covering only part of the range: missing indices are appended (client behaviour)
    assert _reorder(["a", "b", "c"], [2]) == ["b", "c", "a"]


def test_reorder_rejects_malformed_words() -> None:
    contents = ["a", "b", "c"]
    assert _reorder(contents, [0, 0, 2]) == contents  # duplicate
    assert _reorder(contents, [0, 1, 9]) == contents  # out of range
    assert _reorder(contents, [0, 1, "x"]) == contents  # non-numeric
    assert _reorder([], [0]) == []


# ---------------------------------------------------------------------------
# envelope handling
# ---------------------------------------------------------------------------


def test_unwrap_ok() -> None:
    assert _unwrap({"code": 200, "results": {"a": 1}}, "u") == {"a": 1}
    assert _unwrap({"code": "200", "results": [1]}, "u") == [1]


def test_unwrap_code_210_is_app_host() -> None:
    with pytest.raises(AppHostBlocked):
        _unwrap({"code": 210, "message": "请更新APP"}, "u")


def test_unwrap_error_codes_raise_value_error() -> None:
    with pytest.raises(ValueError, match="API code"):
        _unwrap({"code": 500, "message": "boom"}, "u")


def test_unwrap_null_results_and_non_dict() -> None:
    with pytest.raises(CopyMangaError, match="results is null"):
        _unwrap({"code": 200, "results": None}, "u")
    with pytest.raises(CopyMangaError, match="non-object"):
        _unwrap(["nope"], "u")


def test_parse_network_real_fixture() -> None:
    # the live web-host bootstrap: every returned host is a web host
    hosts, image = _parse_network(_fixture("copymanga_network2.json"))
    assert hosts == [
        "https://mapi.hotmangasg.com",
        "https://mapi.hotmangasd.com",
        "https://mapi.hotmangasf.com",
        "https://mapi.elfgjfghkk.club",
        "https://mapi.fgjfghkkcenter.club",
        "https://mapi.fgjfghkk.club",
        "https://www.manga2026.xyz",
    ]
    assert all(not _is_app_host(h) for h in hosts)
    assert image == "https://s3.mangafunb.fun"


def test_parse_network_web_payload_keeps_web_hosts() -> None:
    hosts, image = _parse_network(
        {
            "code": 200,
            "results": {
                "static": "s3.mangafunb.fun",
                "api": [["api.copy202601.com"], ["mapi.hotmangasg.com"]],
                "share": ["www.copy5000.com"],
            },
        }
    )
    assert "https://mapi.hotmangasg.com" in hosts
    assert "https://api.copy202601.com" not in hosts
    assert hosts[0] == "https://mapi.hotmangasg.com"
    assert image == "https://s3.mangafunb.fun"


# ---------------------------------------------------------------------------
# registration / config
# ---------------------------------------------------------------------------


async def test_registration(tmp_path) -> None:
    cfg = AppConfig(output_dir=str(tmp_path))
    fetcher = HttpFetcher(cfg)
    plugin = CopyMangaPlugin(cfg, fetcher)
    assert plugin.id == "copymanga"
    assert plugin.name == "拷貝漫畫 (CopyManga)"
    assert plugin.category == "General"
    assert plugin.base_url == "https://www.mangacopy.com"
    assert plugin.page_order == "words"
    assert plugin.image_width is None
    await fetcher.aclose()


def test_discovered_by_registry() -> None:
    registry = PluginRegistry(AppConfig())
    registry.discover()
    assert "copymanga" in registry._plugins
    assert isinstance(registry._plugins["copymanga"], CopyMangaPlugin)


def test_api_hosts_exclude_app_hosts_and_dedupe() -> None:
    cfg = AppConfig(sources={"copymanga": {"hosts": ["https://api.copy202601.com", "mapi.x.com"]}})
    hosts = _plugin(cfg).api_hosts
    assert all(not _is_app_host(h) for h in hosts)
    assert hosts[0] == "https://mapi.x.com"
    assert len(hosts) == len(set(hosts))
    assert "https://api.2024manga.com" in hosts


def test_website_is_never_used_as_an_api_host() -> None:
    # The site serves HTML and 404s every /api/v3 path, so it must not be
    # contacted as an API host — it only exists for the cover Referer.
    plugin = _plugin()
    assert plugin.base_url == "https://www.mangacopy.com"
    assert plugin.base_url not in plugin.api_hosts
    assert all("mangacopy" not in h and "copy4000" not in h for h in plugin.api_hosts)


def test_config_overrides() -> None:
    cfg = AppConfig(
        sources={"copymanga": {"page_order": "contents", "image_width": 900, "group": "tankobon"}}
    )
    plugin = _plugin(cfg)
    assert plugin.page_order == "contents"
    assert plugin.image_width == 900


def test_image_width_invalid_values_ignored() -> None:
    for value in ("nope", 10, 99999, None, ""):
        cfg = AppConfig(sources={"copymanga": {"image_width": value}})
        assert _plugin(cfg).image_width is None


def test_headers_carry_web_identity_and_no_signature() -> None:
    headers = _plugin()._headers()
    assert headers["user-agent"] == WEB_UA
    assert headers["x-requested-with"] == "com.manga2020.app"
    assert headers["platform"] == "3"
    assert headers["webp"] == "1"
    assert "x-auth-signature" not in headers
    assert "authorization" not in headers


def test_headers_include_token_when_configured() -> None:
    cfg = AppConfig(sources={"copymanga": {"token": "abc123"}})
    assert _plugin(cfg)._headers()["authorization"] == "Token abc123"


# ---------------------------------------------------------------------------
# bootstrap / host failover
# ---------------------------------------------------------------------------


async def test_ensure_bootstrap_uses_web_hosts_from_network2() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.return_value = {
        "code": 200,
        "results": {
            "static": "s3.mangafunb.fun",
            "api": [["api.copy202601.com"], ["mapi.hotmangasg.com", "mapi.hotmangasd.com"]],
            "share": [],
        },
    }
    plugin = _plugin(fetcher=fetcher, booted=False)
    await plugin._ensure_bootstrap()  # noqa: SLF001
    assert plugin._hosts == ["https://mapi.hotmangasg.com", "https://mapi.hotmangasd.com"]  # noqa: SLF001
    assert plugin._image_host == "https://s3.mangafunb.fun"  # noqa: SLF001


async def test_ensure_bootstrap_falls_back_when_network2_fails() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.side_effect = RuntimeError("offline")
    plugin = _plugin(fetcher=fetcher, booted=False)
    await plugin._ensure_bootstrap()  # noqa: SLF001
    assert plugin._hosts  # noqa: SLF001
    assert all(not _is_app_host(h) for h in plugin._hosts)  # noqa: SLF001


async def test_api_get_never_contacts_app_hosts() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.return_value = {"code": 200, "results": {"list": []}}
    plugin = _plugin(fetcher=fetcher)
    plugin._hosts = ["https://api.copy202601.com", HOST]  # noqa: SLF001
    await plugin._api_get("/api/v3/comics")
    requested = [c.args[0] for c in fetcher.get_json.call_args_list]
    assert all("copy202601" not in u for u in requested)
    assert requested == [f"{HOST}/api/v3/comics"]


async def test_api_get_walks_past_code_210_host() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.side_effect = [
        {"code": 210, "message": "请更新APP"},  # unmarked host that still refuses
        {"code": 200, "results": {"ok": True}},
    ]
    plugin = _plugin(fetcher=fetcher)
    plugin._hosts = ["https://api.newapp.example.com", HOST]  # noqa: SLF001
    assert await plugin._api_get("/api/v3/comics") == {"ok": True}
    assert fetcher.get_json.call_count == 2


async def test_api_get_raises_when_all_hosts_fail() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.side_effect = RuntimeError("down")
    plugin = _plugin(fetcher=fetcher)
    with pytest.raises(Exception):
        await plugin._api_get("/api/v3/comics")


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


async def test_search_keyword() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.return_value = _fixture("copymanga_search.json")
    plugin = _plugin(fetcher=fetcher)

    results = await plugin.search("勇者處刑")

    url = fetcher.get_json.call_args.args[0]
    params = fetcher.get_json.call_args.kwargs["params"]
    assert url == f"{HOST}/api/v3/search/comic"
    assert params["q"] == "勇者處刑"
    assert params["limit"] == 20
    assert params["offset"] == 0
    assert params["platform"] == "3"
    assert len(results) == 2
    assert results[0].source_id == "copymanga"
    assert results[0].ref == PW
    assert results[0].title == "勇者處刑 懲罰勇者9004隊服刑記錄"
    assert results[0].author == "ロケツト商会, 井上菜摘"
    assert results[0].cover_url.startswith("https://sy.mangafunb.fun/")


async def test_search_miss_returns_empty() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.return_value = _fixture("copymanga_search_miss.json")
    plugin = _plugin(fetcher=fetcher)
    assert await plugin.search("zzqqwwxx") == []


async def test_search_failure_returns_empty_list() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.side_effect = RuntimeError("network down")
    plugin = _plugin(fetcher=fetcher)
    assert await plugin.search("anything") == []


async def test_search_direct_url_uses_comic_detail() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.return_value = _fixture("copymanga_comic.json")
    plugin = _plugin(fetcher=fetcher)

    results = await plugin.search(f"https://www.mangacopy.com/comic/{PW}")

    assert fetcher.get_json.call_args.args[0] == f"{HOST}/api/v3/comic2/{PW}"
    assert len(results) == 1
    assert results[0].ref == PW
    assert results[0].title == "勇者處刑 懲罰勇者9004隊服刑記錄"


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------


async def test_get_detail_mapping() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.return_value = _fixture("copymanga_comic.json")
    plugin = _plugin(fetcher=fetcher)

    detail = await plugin.get_detail(SearchResult(source_id="copymanga", title="x", ref=PW))

    assert fetcher.get_json.call_args.args[0] == f"{HOST}/api/v3/comic2/{PW}"
    assert detail.title == "勇者處刑 懲罰勇者9004隊服刑記錄"
    assert detail.ref == PW
    assert detail.status == "連載中"
    assert detail.author == "ロケツト商会, 井上菜摘"
    assert detail.cover_url.startswith("https://sy.mangafunb.fun/")
    assert detail.description == "世界上最惡最強的罪人勇者的故事"
    assert detail.chapter_count == 17  # default group count, not the sum of groups
    assert detail.extra["group"] == "default"
    assert "tankobon" in detail.extra["groups"]
    assert detail.extra["free_type"] == "免費"


async def test_get_detail_respects_configured_group() -> None:
    cfg = AppConfig(sources={"copymanga": {"group": "tankobon"}})
    fetcher = AsyncMock()
    fetcher.get_json.return_value = _fixture("copymanga_comic.json")
    detail = await CopyMangaPlugin(cfg, fetcher).get_detail(  # type: ignore[arg-type]
        SearchResult(source_id="copymanga", title="x", ref=PW)
    )
    assert detail.extra["group"] == "tankobon"
    assert detail.chapter_count == 2


# ---------------------------------------------------------------------------
# chapters
# ---------------------------------------------------------------------------


def _detail(ref: str = PW, group: str = "default") -> MangaDetail:
    return MangaDetail(
        source_id="copymanga",
        title="t",
        ref=ref,
        extra={"path_word": ref, "group": group, "groups": {"default": {"count": 3}}},
    )


async def test_list_chapters_preserves_order_and_indexes() -> None:
    fixture = _fixture("copymanga_chapters.json")
    rows = fixture["results"]["list"]
    fetcher = AsyncMock()
    fetcher.get_json.return_value = fixture
    plugin = _plugin(fetcher=fetcher)

    chapters = await plugin.list_chapters(_detail())

    assert fetcher.get_json.call_args.args[0] == f"{HOST}/api/v3/comic/{PW}/group/default/chapters"
    assert len(chapters) == len(rows)
    assert [c.ref for c in chapters] == [r["uuid"] for r in rows]
    assert [c.index for c in chapters] == list(range(1, len(rows) + 1))
    assert [c.title for c in chapters] == [r["name"] for r in rows]
    assert all(c.manga_ref == PW for c in chapters)
    assert all(c.extra["path_word"] == PW for c in chapters)
    assert chapters[0].extra["next"] == rows[0]["next"]


async def test_list_chapters_paginates() -> None:
    def page(rows: list[dict], offset: int) -> dict:
        return {
            "code": 200,
            "results": {"list": rows, "total": 101, "limit": 100, "offset": offset},
        }

    page1 = page([{"uuid": f"u{i}", "name": f"第{i:02d}話"} for i in range(100)], 0)
    page2 = page([{"uuid": "u100", "name": "第101話"}], 100)
    fetcher = AsyncMock()
    fetcher.get_json.side_effect = [page1, page2]
    plugin = _plugin(fetcher=fetcher)

    chapters = await plugin.list_chapters(_detail())

    assert len(chapters) == 101
    assert chapters[-1].ref == "u100"
    assert fetcher.get_json.call_args_list[1].kwargs["params"]["offset"] == 100


async def test_list_chapters_uses_group_from_detail() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.return_value = _fixture("copymanga_chapters.json")
    plugin = _plugin(fetcher=fetcher)
    await plugin.list_chapters(_detail(group="tankobon"))
    assert "/group/tankobon/chapters" in fetcher.get_json.call_args.args[0]


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


def _chapter(uuid: str = "c0e7caae-b17a-11ec-8aa9-024352452ce0") -> Chapter:
    return Chapter(
        source_id="copymanga",
        manga_ref=PW,
        title="第01話",
        ref=uuid,
        index=1,
        extra={"path_word": PW, "uuid": uuid, "group": "default"},
    )


async def test_get_pages_contents_order_when_no_words() -> None:
    fixture = _fixture("copymanga_chapter_pages.json")
    contents = fixture["results"]["chapter"]["contents"]
    assert "words" not in fixture["results"]["chapter"]  # fixture sanity: the live shape
    fetcher = AsyncMock()
    fetcher.get_json.return_value = fixture
    plugin = _plugin(fetcher=fetcher)

    pages = await plugin.get_pages(_chapter())

    assert fetcher.get_json.call_args.args[0] == (
        f"{HOST}/api/v3/comic/{PW}/chapter/c0e7caae-b17a-11ec-8aa9-024352452ce0"
    )
    assert [p.image_url for p in pages] == [c["url"] for c in contents]
    assert [p.index for p in pages] == list(range(len(contents)))
    assert all(p.referer is None for p in pages)  # the CDN needs no Referer


async def test_get_pages_applies_words_order() -> None:
    fixture = _fixture("copymanga_chapter_pages_words.json")
    contents = fixture["results"]["chapter"]["contents"]
    fetcher = AsyncMock()
    fetcher.get_json.return_value = fixture
    plugin = _plugin(fetcher=fetcher)

    pages = await plugin.get_pages(_chapter())

    # words [2,0,4,1,3] -> contents indices [1,3,0,4,2]
    assert [_num(p.image_url) for p in pages] == [_num(contents[i]["url"]) for i in (1, 3, 0, 4, 2)]
    assert [p.image_url for p in pages] != [c["url"] for c in contents]


async def test_get_pages_page_order_contents_disables_words() -> None:
    cfg = AppConfig(sources={"copymanga": {"page_order": "contents"}})
    fixture = _fixture("copymanga_chapter_pages_words.json")
    fetcher = AsyncMock()
    fetcher.get_json.return_value = fixture
    pages = await CopyMangaPlugin(cfg, fetcher).get_pages(_chapter())  # type: ignore[arg-type]
    assert [p.image_url for p in pages] == [
        c["url"] for c in fixture["results"]["chapter"]["contents"]
    ]


async def test_get_pages_rewrites_image_width() -> None:
    cfg = AppConfig(sources={"copymanga": {"image_width": 2000}})
    fetcher = AsyncMock()
    fetcher.get_json.return_value = _fixture("copymanga_chapter_pages.json")
    pages = await CopyMangaPlugin(cfg, fetcher).get_pages(_chapter())  # type: ignore[arg-type]
    assert all(re.search(r"\.[a-z]2000x\.", p.image_url) for p in pages)


async def test_get_pages_locked_chapter_raises() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.return_value = _fixture("copymanga_chapter_locked.json")
    plugin = _plugin(fetcher=fetcher)
    with pytest.raises(ValueError, match="locked/VIP"):
        await plugin.get_pages(_chapter("locked-uuid"))


async def test_get_pages_falls_back_to_chapter2_segment() -> None:
    # `/chapter/` is unusable on every host (the app path 404s here), so the
    # plugin must fall through to `/chapter2/` rather than give up.
    fixture = _fixture("copymanga_chapter_pages.json")

    def fake(url: str, **_kwargs) -> dict:
        if "/chapter/" in url:
            raise RuntimeError("non-JSON response")
        return fixture

    fetcher = AsyncMock()
    fetcher.get_json.side_effect = fake
    plugin = _plugin(fetcher=fetcher)

    pages = await plugin.get_pages(_chapter())

    assert len(pages) == len(fixture["results"]["chapter"]["contents"])
    urls = [c.args[0] for c in fetcher.get_json.call_args_list]
    assert all("/chapter/" in u for u in urls[:-1])
    assert urls[-1].endswith(f"/chapter2/{_chapter().ref}")


# ---------------------------------------------------------------------------
# real-capture cross-check (skipped when the HAR is absent)
# ---------------------------------------------------------------------------


async def test_real_har_words_ordering() -> None:
    if not HAR.exists():
        pytest.skip("HTTPToolkit_2026-09-27_12-29.har not available")
    har = json.loads(HAR.read_text(encoding="utf-8"))
    body = None
    for entry in har["log"]["entries"]:
        if "/comic/yongzhehaoxiangaishanglemowang/chapter2/" in entry["request"]["url"]:
            text = entry["response"]["content"].get("text")
            if text:
                body = json.loads(text)
                break
    assert body is not None

    node = body["results"]["chapter"]
    contents = [c["url"] for c in node["contents"]]
    words = node["words"]
    assert len(contents) == 23 and len(words) == 23

    fetcher = AsyncMock()
    fetcher.get_json.return_value = body
    plugin = _plugin(fetcher=fetcher)
    pages = await plugin.get_pages(_chapter(node["uuid"]))

    expected = [contents[j] for j in sorted(range(len(contents)), key=lambda i: words[i])]
    assert [p.image_url for p in pages] == expected
    assert len(pages) == 23
    # the scramble is real: contents order != reading order
    assert [p.image_url for p in pages] != contents


def test_bootstrap_hosts_are_all_web_hosts() -> None:
    assert all(not _is_app_host(h) for h in BOOTSTRAP_HOSTS)

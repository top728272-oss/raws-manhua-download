"""Tests for MkzhanPlugin (漫客栈, www.mkzhan.com)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.config import AppConfig
from app.core.fetcher import HttpFetcher
from app.core.models import Chapter, MangaDetail, SearchResult
from app.core.registry import PluginRegistry
from app.plugins.mkzhan import MkzhanPlugin, _check_code, _clean_ref

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _plugin(cfg: AppConfig | None = None, fetcher=None) -> MkzhanPlugin:
    cfg = cfg or AppConfig()
    return MkzhanPlugin(cfg, fetcher)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ref cleaning / envelope guard
# ---------------------------------------------------------------------------


def test_clean_ref() -> None:
    assert _clean_ref("215185") == "215185"
    assert _clean_ref("https://www.mkzhan.com/215185/") == "215185"
    assert _clean_ref("https://www.mkzhan.com/215185/872395.html") == "215185"
    assert _clean_ref("/215185/872395.html") == "215185"
    assert _clean_ref("https://comic.mkzcdn.com/comic/info/?comic_id=215185") == "215185"
    assert _clean_ref("恋爱") == "恋爱"
    assert _clean_ref("恋爱.html") == "恋爱"


def test_check_code_raises_on_error_codes() -> None:
    assert _check_code({"code": "200", "data": {}})["code"] == "200"
    with pytest.raises(ValueError, match="login"):
        _check_code({"code": "302", "message": "请登录"})
    with pytest.raises(ValueError, match="paywall"):
        _check_code({"code": "102", "message": "付费"})
    with pytest.raises(ValueError, match="paywall"):
        _check_code({"code": "103", "message": "付费"})
    with pytest.raises(ValueError, match="early-access"):
        _check_code({"code": "199", "message": "抢先"})
    with pytest.raises(ValueError, match="101"):
        _check_code({"code": "101", "message": "质量类型不能为空"})
    with pytest.raises(ValueError, match="unexpected"):
        _check_code({"code": "500", "message": "boom"})
    with pytest.raises(ValueError, match="non-object"):
        _check_code(["not", "a", "dict"])


# ---------------------------------------------------------------------------
# registration / discovery
# ---------------------------------------------------------------------------


async def test_mkzhan_plugin_registration(tmp_path) -> None:
    cfg = AppConfig(output_dir=str(tmp_path))
    fetcher = HttpFetcher(cfg)
    plugin = MkzhanPlugin(cfg, fetcher)
    assert plugin.id == "mkzhan"
    assert plugin.name == "漫客栈 (mkzhan)"
    assert plugin.category == "General"
    assert plugin.base_url == "https://www.mkzhan.com"
    assert plugin.api_base == "https://comic.mkzcdn.com"
    assert plugin.quality == 3
    assert plugin.include_vip is False
    await fetcher.aclose()


def test_mkzhan_plugin_discovered_by_registry() -> None:
    registry = PluginRegistry(AppConfig())
    registry.discover()
    assert "mkzhan" in registry._plugins
    assert isinstance(registry._plugins["mkzhan"], MkzhanPlugin)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


async def test_search_uses_search_endpoint() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.return_value = _load_fixture("mkzhan_search.json")
    plugin = _plugin(fetcher=fetcher)

    results = await plugin.search("星际帝国第一宠婚")

    requested = fetcher.get_json.call_args[0][0]
    assert "/search/keyword/" in requested
    assert "page_num=1" in requested
    assert "page_size=20" in requested
    assert len(results) == 1
    assert results[0].source_id == "mkzhan"
    assert results[0].ref == "215185"
    assert results[0].title == "星际帝国第一宠婚"
    assert results[0].author == "极漫文化"
    assert results[0].cover_url and results[0].cover_url.startswith("https://")


async def test_search_direct_ref_uses_comic_info() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.return_value = _load_fixture("mkzhan_comic_info.json")
    plugin = _plugin(fetcher=fetcher)

    results = await plugin.search("https://www.mkzhan.com/215185/")

    requested = fetcher.get_json.call_args[0][0]
    assert "/comic/info/?comic_id=215185" in requested
    assert len(results) == 1
    assert results[0].ref == "215185"
    assert results[0].title == "星际帝国第一宠婚"


async def test_search_failure_returns_empty_list() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.side_effect = RuntimeError("network down")
    plugin = _plugin(fetcher=fetcher)
    assert await plugin.search("anything") == []


async def test_search_no_results_code_199_returns_empty() -> None:
    # mkzhan returns code 199 ("搜索结果为空") when a keyword has no hits;
    # that must surface as an empty list, not an error.
    fetcher = AsyncMock()
    fetcher.get_json.return_value = {"code": "199", "message": "搜索结果为空", "data": None}
    plugin = _plugin(fetcher=fetcher)
    assert await plugin.search("不存在的作品") == []


def test_parse_search_defensive_shapes() -> None:
    plugin = _plugin()

    # canonical shape: data.list
    assert len(plugin._parse_search({"list": [{"comic_id": "1", "title": "A"}]})) == 1
    # alternate: data is a bare list
    assert len(plugin._parse_search([{"comic_id": "2", "title": "B"}])) == 1
    # alternate: data.data.list
    nested = {"data": {"list": [{"id": "3", "name": "C", "pic": "http://x/c.jpg"}]}}
    results = plugin._parse_search(nested)
    assert len(results) == 1
    assert results[0].ref == "3"
    assert results[0].title == "C"
    assert results[0].cover_url == "https://x/c.jpg"
    # garbage shapes -> empty
    assert plugin._parse_search(None) == []
    assert plugin._parse_search("nope") == []
    assert plugin._parse_search({"list": ["junk", {"comic_id": ""}, {"id": "4"}]})[0].ref == "4"


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------


async def test_get_detail_mapping() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.return_value = _load_fixture("mkzhan_comic_info.json")
    plugin = _plugin(fetcher=fetcher)

    detail = await plugin.get_detail(SearchResult(source_id="mkzhan", title="x", ref="215185"))

    requested = fetcher.get_json.call_args[0][0]
    assert "/comic/info/?comic_id=215185" in requested
    assert detail.title == "星际帝国第一宠婚"
    assert detail.status == "完结"
    assert detail.chapter_count == 76
    assert detail.cover_url and detail.cover_url.startswith("https://oss.mkzcdn.com/")
    assert detail.description
    assert detail.extra["comic_id"] == "215185"


async def test_get_detail_ongoing_status() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.return_value = {
        "code": "200",
        "message": "ok",
        "data": {"title": "连载中的作品", "cover": "http://oss.mkzcdn.com/c.jpg", "finish": "1"},
    }
    plugin = _plugin(fetcher=fetcher)
    detail = await plugin.get_detail(SearchResult(source_id="mkzhan", title="x", ref="216466"))
    assert detail.status == "连载中"
    assert detail.cover_url == "https://oss.mkzcdn.com/c.jpg"


# ---------------------------------------------------------------------------
# chapters
# ---------------------------------------------------------------------------


def _make_detail(ref: str = "215185") -> MangaDetail:
    return MangaDetail(
        source_id="mkzhan",
        title="星际帝国第一宠婚",
        ref=ref,
    )


async def test_list_chapters_skips_vip_and_preserves_order() -> None:
    fixture = _load_fixture("mkzhan_chapter_list.json")
    rows = fixture["data"]
    vip_rows = [r for r in rows if r.get("is_vip") == "1"]
    assert len(vip_rows) == 2  # fixture sanity: synthetic VIP rows present

    fetcher = AsyncMock()
    fetcher.get_json.return_value = fixture
    plugin = _plugin(fetcher=fetcher)

    chapters = await plugin.list_chapters(_make_detail())

    requested = fetcher.get_json.call_args[0][0]
    assert "/chapter/v1/?comic_id=215185" in requested
    # VIP rows filtered, non-VIP order preserved, 1-based sequential index
    assert len(chapters) == len(rows) - 2
    refs = [c.ref for c in chapters]
    assert "999901" not in refs and "999902" not in refs
    assert [c.index for c in chapters] == list(range(1, len(chapters) + 1))
    assert [c.ref for c in chapters] == [r["chapter_id"] for r in rows if r.get("is_vip") != "1"]
    assert all(c.manga_ref == "215185" for c in chapters)
    assert all(c.extra["comic_id"] == "215185" for c in chapters)


async def test_list_chapters_include_vip_config() -> None:
    cfg = AppConfig(sources={"mkzhan": {"include_vip": True}})
    fetcher = AsyncMock()
    fetcher.get_json.return_value = _load_fixture("mkzhan_chapter_list.json")
    plugin = MkzhanPlugin(cfg, fetcher)  # type: ignore[arg-type]

    chapters = await plugin.list_chapters(_make_detail())
    assert len(chapters) == 8
    assert "999901" in [c.ref for c in chapters]


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


def _make_chapter(comic_id: str = "215185", chapter_id: str = "872395") -> Chapter:
    return Chapter(
        source_id="mkzhan",
        manga_ref=comic_id,
        title="第1话",
        ref=chapter_id,
        index=1,
        extra={"comic_id": comic_id, "chapter_id": chapter_id},
    )


async def test_get_pages_mapping() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.return_value = _load_fixture("mkzhan_chapter_content.json")
    plugin = _plugin(fetcher=fetcher)

    pages = await plugin.get_pages(_make_chapter())

    requested = fetcher.get_json.call_args[0][0]
    assert "/chapter/content/v1/" in requested
    assert "chapter_id=872395" in requested
    assert "comic_id=215185" in requested
    assert "format=1" in requested
    assert "quality=3" in requested
    assert "type=1" in requested

    assert len(pages) == 4
    assert [p.index for p in pages] == [0, 1, 2, 3]
    for page in pages:
        assert page.image_url.startswith("https://content.mkzcdn.com/")
        assert "!page-800" in page.image_url  # format=1 (webp) + quality=3? see fixture note
        assert page.referer == "https://www.mkzhan.com/"


async def test_get_pages_quality_from_config() -> None:
    cfg = AppConfig(sources={"mkzhan": {"quality": 1}})
    fetcher = AsyncMock()
    fetcher.get_json.return_value = _load_fixture("mkzhan_chapter_content.json")
    plugin = MkzhanPlugin(cfg, fetcher)  # type: ignore[arg-type]

    await plugin.get_pages(_make_chapter())
    requested = fetcher.get_json.call_args[0][0]
    assert "quality=1" in requested


async def test_get_pages_invalid_quality_falls_back() -> None:
    cfg = AppConfig(sources={"mkzhan": {"quality": 9}})
    plugin = MkzhanPlugin(cfg, fetcher=None)  # type: ignore[arg-type]
    assert plugin.quality == 3  # 9 is invalid -> fallback to 3


async def test_get_pages_paywall_raises() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.return_value = {"code": "102", "message": "付费章节"}
    plugin = _plugin(fetcher=fetcher)
    with pytest.raises(ValueError, match="paywall"):
        await plugin.get_pages(_make_chapter())


async def test_get_pages_login_required_raises() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.return_value = {"code": "302", "message": "请登录"}
    plugin = _plugin(fetcher=fetcher)
    with pytest.raises(ValueError, match="login"):
        await plugin.get_pages(_make_chapter())


async def test_get_pages_bad_quality_code_raises() -> None:
    fetcher = AsyncMock()
    fetcher.get_json.return_value = {"code": "101", "message": "质量类型不能为空"}
    plugin = _plugin(fetcher=fetcher)
    with pytest.raises(ValueError, match="101"):
        await plugin.get_pages(_make_chapter())


# ---------------------------------------------------------------------------
# full HAR capture cross-check (skipped when .examples/ is absent)
# ---------------------------------------------------------------------------


async def test_get_pages_from_har_capture() -> None:
    har_path = Path(__file__).parent.parent / ".examples" / "www.mkzhan.com.har"
    if not har_path.exists():
        pytest.skip(".examples/www.mkzhan.com.har not available")

    har = json.loads(har_path.read_text(encoding="utf-8"))
    body = None
    for entry in har["log"]["entries"]:
        if "/chapter/content/v1/" in entry["request"]["url"]:
            text = entry["response"]["content"].get("text")
            if text:
                body = json.loads(text)
                break
    assert body is not None

    fetcher = AsyncMock()
    fetcher.get_json.return_value = body
    plugin = _plugin(fetcher=fetcher)
    pages = await plugin.get_pages(_make_chapter())
    assert len(pages) == 28
    assert pages[0].image_url.startswith("https://content.mkzcdn.com/image/")
    assert "auth_key=" in pages[0].image_url

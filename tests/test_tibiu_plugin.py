"""Tests for TibiuPlugin."""

from __future__ import annotations

from unittest.mock import AsyncMock

from app.config import AppConfig
from app.core.fetcher import HttpFetcher
from app.core.models import Chapter, MangaDetail, SearchResult
from app.plugins.tibiu import TibiuPlugin, _clean_ref


def test_clean_ref() -> None:
    assert _clean_ref("66091") == "66091"
    assert _clean_ref("https://comic.tibiu.net/comic/66091") == "66091"
    assert _clean_ref("/comic/66091") == "66091"
    assert _clean_ref("https://comic.tibiu.net/comic/detail?mid=66091") == "66091"
    assert _clean_ref("女鬼小姐，禁止贴贴！") == "女鬼小姐，禁止贴贴！"


async def test_tibiu_plugin_registration(tmp_path) -> None:
    cfg = AppConfig(output_dir=str(tmp_path))
    fetcher = HttpFetcher(cfg)
    plugin = TibiuPlugin(cfg, fetcher)

    assert plugin.id == "tibiu"
    assert plugin.name == "TIBIU漫画 (tibiu)"
    assert plugin.base_url == "https://comic.tibiu.net"
    await fetcher.aclose()


def test_parse_search_json() -> None:
    cfg = AppConfig()
    plugin = TibiuPlugin(cfg, fetcher=None)  # type: ignore[arg-type]

    sample_data = {
        "code": 1,
        "msg": "漫画列表",
        "data": [
            {
                "id": "66091",
                "name": "女鬼小姐，禁止贴贴！",
                "pic": "https://s3-aws.tibiu.xyz/dax/manwa/comic/cover/438201.jpg",
                "author": "二三玖陆工作室",
                "content": "作为道士的兰亭捡到了一个千年女鬼...",
            }
        ],
    }

    results = plugin._parse_search_json(sample_data)
    assert len(results) == 1
    assert results[0].source_id == "tibiu"
    assert results[0].title == "女鬼小姐，禁止贴贴！"
    assert results[0].ref == "66091"
    assert results[0].cover_url == "https://s3-aws.tibiu.xyz/dax/manwa/comic/cover/438201.jpg"
    assert results[0].author == "二三玖陆工作室"


def test_parse_detail() -> None:
    cfg = AppConfig()
    plugin = TibiuPlugin(cfg, fetcher=None)  # type: ignore[arg-type]

    sample_html = """
    <!DOCTYPE html>
    <html>
    <head>
      <title>女鬼小姐，禁止贴贴！ - TIBIU漫画在线</title>
      <meta property="og:title" content="女鬼小姐，禁止贴贴！ - TIBIU漫画在线">
      <meta property="og:description" content="作为道士的兰亭捡到了一个千年女鬼...">
      <meta property="og:image" content="https://s3-aws.tibiu.xyz/dax/manwa/comic/cover/438201.jpg">
    </head>
    <body>
      <h1>女鬼小姐，禁止贴贴！</h1>
    </body>
    </html>
    """

    res = SearchResult(source_id="tibiu", title="女鬼小姐，禁止贴贴！", ref="66091")
    detail = plugin._parse_detail(sample_html, res)
    assert detail.title == "女鬼小姐，禁止贴贴！"
    assert detail.cover_url == "https://s3-aws.tibiu.xyz/dax/manwa/comic/cover/438201.jpg"
    assert detail.description == "作为道士的兰亭捡到了一个千年女鬼..."


async def test_list_chapters_json_api() -> None:
    cfg = AppConfig()
    mock_fetcher = AsyncMock()
    mock_fetcher.get_json.return_value = {
        "code": 1,
        "msg": "章节列表",
        "data": [
            {"id": "793232", "name": "1 怎么会有这样的小女鬼？！"},
            {"id": "1012301", "name": "33 这样你就受不了了？"},
        ],
    }

    plugin = TibiuPlugin(cfg, fetcher=mock_fetcher)
    detail = MangaDetail(source_id="tibiu", title="Test Manga", ref="66091")
    chapters = await plugin.list_chapters(detail)

    assert len(chapters) == 2
    assert chapters[0].ref == "66091/793232"
    assert chapters[0].title == "1 怎么会有这样的小女鬼？！"
    assert chapters[1].ref == "66091/1012301"
    assert chapters[1].title == "33 这样你就受不了了？"


async def test_get_pages() -> None:
    cfg = AppConfig()
    mock_fetcher = AsyncMock()
    mock_fetcher.get_json.return_value = {
        "code": 1,
        "msg": "图片列表",
        "data": [
            {
                "id": "2",
                "img": "https://s3-aws.tibiu.xyz/dax/manwa/comic/chapter/438201/31377435/0002.webp",
            },
            {
                "id": "1",
                "img": "https://s3-aws.tibiu.xyz/dax/manwa/comic/chapter/438201/31377435/0001.webp",
            },
        ],
    }

    plugin = TibiuPlugin(cfg, fetcher=mock_fetcher)
    chapter = Chapter(source_id="tibiu", manga_ref="66091", title="Ch 33", ref="66091/1012301")
    pages = await plugin.get_pages(chapter)

    assert len(pages) == 2
    assert (
        pages[0].image_url
        == "https://s3-aws.tibiu.xyz/dax/manwa/comic/chapter/438201/31377435/0001.webp"
    )
    assert (
        pages[1].image_url
        == "https://s3-aws.tibiu.xyz/dax/manwa/comic/chapter/438201/31377435/0002.webp"
    )
    assert pages[0].referer == "https://comic.tibiu.net/"

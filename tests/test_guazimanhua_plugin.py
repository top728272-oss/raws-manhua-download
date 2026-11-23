"""Tests for GuaziManhuaPlugin."""

from __future__ import annotations

from unittest.mock import AsyncMock

from app.config import AppConfig
from app.core.fetcher import HttpFetcher
from app.core.models import Chapter, SearchResult
from app.plugins.guazimanhua import GuaziManhuaPlugin, _clean_ref


def test_clean_ref() -> None:
    assert _clean_ref("19158") == "19158"
    assert _clean_ref("https://www.guazimanhua.com/comic.php?id=19158") == "19158"
    assert _clean_ref("/comic.php?id=19158") == "19158"
    assert _clean_ref("https://www.guazimanhua.com/chapter.php?id=985454") == "985454"
    assert _clean_ref("暗神榜") == "暗神榜"


async def test_guazimanhua_plugin_registration(tmp_path) -> None:
    cfg = AppConfig(output_dir=str(tmp_path))
    fetcher = HttpFetcher(cfg)
    plugin = GuaziManhuaPlugin(cfg, fetcher)

    assert plugin.id == "guazimanhua"
    assert plugin.name == "瓜子漫画 (guazimanhua)"
    assert plugin.base_url == "https://www.guazimanhua.com"
    await fetcher.aclose()


def test_parse_search() -> None:
    cfg = AppConfig()
    plugin = GuaziManhuaPlugin(cfg, fetcher=None)  # type: ignore[arg-type]

    sample_html = """
    <section class="grid">
        <article class="card">
            <a class="cover-wrap" href="/comic.php?id=19158">
                <img class="cover" src="https://img.guazicdn.com/th/comics/cover/11630/260421/cover.webp" alt="暗神榜封面" />
            </a>
            <h3><a href="/comic.php?id=19158">暗神榜</a></h3>
            <div class="meta">白辰广 · 连载</div>
        </article>
    </section>
    """

    results = plugin._parse_search(sample_html)
    assert len(results) == 1
    assert results[0].source_id == "guazimanhua"
    assert results[0].title == "暗神榜"
    assert results[0].ref == "19158"
    assert (
        results[0].cover_url == "https://img.guazicdn.com/th/comics/cover/11630/260421/cover.webp"
    )
    assert results[0].author == "白辰广"


def test_parse_detail() -> None:
    cfg = AppConfig()
    plugin = GuaziManhuaPlugin(cfg, fetcher=None)  # type: ignore[arg-type]

    sample_html = """
    <html>
        <head>
            <meta property="og:image" content="https://img.guazicdn.com/th/comics/cover/11630/260421/cover.webp" />
            <meta name="description" content="暗神榜漫画简介" />
        </head>
        <body>
            <h1>暗神榜</h1>
            <div class="meta">白辰广 · 连载</div>
        </body>
    </html>
    """

    detail = plugin._parse_detail(
        sample_html, SearchResult(source_id="guazimanhua", title="暗神榜", ref="19158")
    )
    assert detail.title == "暗神榜"
    assert detail.ref == "19158"
    assert detail.cover_url == "https://img.guazicdn.com/th/comics/cover/11630/260421/cover.webp"
    assert detail.author == "白辰广"
    assert detail.status == "连载"
    assert detail.description == "暗神榜漫画简介"


def test_list_chapters() -> None:
    cfg = AppConfig()
    plugin = GuaziManhuaPlugin(cfg, fetcher=None)  # type: ignore[arg-type]

    sample_html = """
    <div class="chapter-list">
        <a href="/chapter.php?id=985454">从第一章开始阅读</a>
        <a href="/chapter.php?id=2417027">截教陰謀 阅读</a>
        <a href="/chapter.php?id=985454">人体炼丹</a>
    </div>
    """

    chapters = plugin._parse_chapters(sample_html, "19158")
    assert len(chapters) == 2
    assert chapters[0].ref == "985454"
    assert chapters[0].title == "001 - 人体炼丹"
    assert chapters[0].index == 1

    assert chapters[1].ref == "2417027"
    assert chapters[1].title == "002 - 截教陰謀"
    assert chapters[1].index == 2


async def test_get_pages() -> None:
    cfg = AppConfig()
    mock_fetcher = AsyncMock()
    mock_fetcher.get_text.return_value = """
    <main>
        <img class="reading-image" src="https://img.guazicdn.com/th/comics/chapters/11630/260421/1_1.webp" />
        <img class="reading-image" src="https://img.guazicdn.com/th/comics/chapters/11630/260421/1_2.webp" />
    </main>
    """

    plugin = GuaziManhuaPlugin(cfg, fetcher=mock_fetcher)
    chapter = Chapter(source_id="guazimanhua", manga_ref="19158", title="Ch 1", ref="985454")
    pages = await plugin.get_pages(chapter)

    assert len(pages) == 2
    assert pages[0].image_url == "https://img.guazicdn.com/th/comics/chapters/11630/260421/1_1.webp"
    assert pages[1].image_url == "https://img.guazicdn.com/th/comics/chapters/11630/260421/1_2.webp"
    assert pages[0].referer == "https://www.guazimanhua.com/"

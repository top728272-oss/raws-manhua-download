"""Tests for ManWangPlugin."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from app.config import AppConfig
from app.core.fetcher import HttpFetcher
from app.core.models import Chapter, SearchResult
from app.plugins.manwang import AES_KEY, ManWangPlugin, _clean_ref, _decrypt_params


def _encrypt_params_for_test(data: dict) -> str:
    iv = b"1234567890abcdef"
    json_bytes = json.dumps(data).encode("utf-8")
    cipher = AES.new(AES_KEY, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(pad(json_bytes, AES.block_size))
    return base64.b64encode(iv + ciphertext).decode("utf-8")


def test_clean_ref() -> None:
    assert _clean_ref("527142") == "527142"
    assert _clean_ref("https://www.manwang.net/book/527142") == "527142"
    assert _clean_ref("https://www.manwang.net/book/527142.html") == "527142"
    assert _clean_ref("/book/527142") == "527142"
    assert _clean_ref("527142-178033") == "527142-178033"


async def test_manwang_plugin_registration(tmp_path) -> None:
    cfg = AppConfig(output_dir=str(tmp_path))
    fetcher = HttpFetcher(cfg)
    plugin = ManWangPlugin(cfg, fetcher)

    assert plugin.id == "manwang"
    assert plugin.name == "漫王 (manwang)"
    assert plugin.base_url == "https://www.manwang.net"
    await fetcher.aclose()


def test_decrypt_params() -> None:
    test_data = {
        "comic_id": "527142",
        "chapter_id": "178033",
        "images": [
            "https://dmw.546457.xyz/page1.webp",
            "https://dmw.546457.xyz/page2.webp",
        ],
    }
    encrypted = _encrypt_params_for_test(test_data)
    decrypted = _decrypt_params(encrypted)

    assert decrypted["comic_id"] == "527142"
    assert decrypted["chapter_id"] == "178033"
    assert len(decrypted["images"]) == 2
    assert decrypted["images"][0] == "https://dmw.546457.xyz/page1.webp"


def test_parse_search() -> None:
    cfg = AppConfig()
    plugin = ManWangPlugin(cfg, fetcher=None)  # type: ignore[arg-type]

    sample_html = """
    <ul class="list-comic-book ui-clfx">
        <li>
            <section class="mod-hitem-comic">
                <a href="/book/507050" class="comic-item">
                    <div class="comic-book">
                        <img data-src="https://p6.ecombdimg.com/cover.jpeg" alt="纯情丫头火辣辣">
                    </div>
                    <div class="comic-info">
                        <h2>纯情丫头火辣辣</h2>
                        <div class="data">
                            <p class="desc">洛瑶瑶，一夜醉酒她竟把那...</p>
                        </div>
                    </div>
                </a>
            </section>
        </li>
    </ul>
    """

    results = plugin._parse_search(sample_html)
    assert len(results) == 1
    assert results[0].source_id == "manwang"
    assert results[0].title == "纯情丫头火辣辣"
    assert results[0].ref == "507050"
    assert results[0].cover_url == "https://p6.ecombdimg.com/cover.jpeg"


def test_parse_detail() -> None:
    cfg = AppConfig()
    plugin = ManWangPlugin(cfg, fetcher=None)  # type: ignore[arg-type]

    sample_html = """
    <!DOCTYPE html>
    <html>
    <head>
      <title>爷和女鬼硬碰硬_爷和女鬼硬碰硬漫画-漫网</title>
      <meta property="og:title" content="爷和女鬼硬碰硬">
      <meta property="og:description" content="遇到女鬼怎么办？">
      <meta property="og:image" content="https://p6.ecombdimg.com/cover.jpeg">
      <meta property="og:novel:author" content="作者: 二三玖陆工作室">
    </head>
    <body>
      <h1>爷和女鬼硬碰硬</h1>
    </body>
    </html>
    """

    res = SearchResult(source_id="manwang", title="爷和女鬼硬碰硬", ref="527142")
    detail = plugin._parse_detail(sample_html, res)
    assert detail.title == "爷和女鬼硬碰硬"
    assert detail.cover_url == "https://p6.ecombdimg.com/cover.jpeg"
    assert detail.author == "二三玖陆工作室"
    assert detail.description == "遇到女鬼怎么办？"


def test_parse_chapters() -> None:
    cfg = AppConfig()
    plugin = ManWangPlugin(cfg, fetcher=None)  # type: ignore[arg-type]

    sample_html = """
    <div class="chapters">
        <a href="/chapter/527142-178034">开始阅读</a>
        <a href="/chapter/527142-178034">001 女鬼？一拳给她干翻！</a>
        <a href="/chapter/527142-178033">002 开局就有美少女上门服务</a>
    </div>
    """

    chapters = plugin._parse_chapters(sample_html, "527142")
    assert len(chapters) == 2
    assert chapters[0].ref == "527142-178034"
    assert chapters[0].title == "001 女鬼？一拳给她干翻！"
    assert chapters[1].ref == "527142-178033"
    assert chapters[1].title == "002 开局就有美少女上门服务"


async def test_get_pages() -> None:
    cfg = AppConfig()
    mock_fetcher = AsyncMock()

    test_payload = {
        "comic_id": "527142",
        "chapter_id": "178033",
        "images": [
            "https://dmw.546457.xyz/1.webp",
            "https://dmw.546457.xyz/2.webp",
        ],
    }
    encrypted = _encrypt_params_for_test(test_payload)

    mock_fetcher.get_text.return_value = f"""
    <html>
    <script>
    var params = '{encrypted}';
    </script>
    </html>
    """

    plugin = ManWangPlugin(cfg, fetcher=mock_fetcher)
    chapter = Chapter(source_id="manwang", manga_ref="527142", title="Ch 2", ref="527142-178033")
    pages = await plugin.get_pages(chapter)

    assert len(pages) == 2
    assert pages[0].image_url == "https://dmw.546457.xyz/1.webp"
    assert pages[1].image_url == "https://dmw.546457.xyz/2.webp"
    assert pages[0].referer == "https://www.manwang.net/"

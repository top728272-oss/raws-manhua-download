"""Tests for ComicBox / WuWu source plugin."""

from __future__ import annotations

import hashlib
import io
from unittest.mock import AsyncMock

import pytest
from Crypto.Cipher import AES
from PIL import Image

from app.config import AppConfig
from app.core.fetcher import BaseFetcher, FetchError
from app.core.models import SearchResult
from app.plugins.comicbox import (
    AES_IV,
    AES_KEY,
    CORP_MAP,
    ComicBoxFetcher,
    ComicBoxPlugin,
    decrypt_and_unscramble_image,
    decrypt_chunks,
    unscramble_image,
)


class MockFetcher(BaseFetcher):
    def __init__(self) -> None:
        self.get_text_mock = AsyncMock()
        self.get_bytes_mock = AsyncMock()

    async def get_text(self, url: str, **kwargs) -> str:
        return await self.get_text_mock(url, **kwargs)

    async def get_bytes(self, url: str, **kwargs) -> bytes:
        return await self.get_bytes_mock(url, **kwargs)


def _encrypt_stream(payload: bytes) -> bytes:
    """PKCS#7-pad + AES-CBC encrypt one payload (one CDN chunk)."""
    pad_len = 16 - (len(payload) % 16)
    payload += bytes([pad_len]) * pad_len
    return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(payload)


def _create_mock_scrambled_image(
    width: int = 720, height: int = 800, book_id: int = 3751, page_number: int = 2965447
) -> tuple[bytes, bytes]:
    """Build the two `.b_0` / `.b_1` chunks the CDN serves for one page.

    Each chunk is its OWN AES-CBC stream with its own PKCS#7 padding — this is
    what the live CDN does (verified against bmigmi-global-wuwu.ccavbox.com),
    and modelling it as one stream cut in half is what hid the decrypt bug.
    """
    # Calculate slice count
    md5_hash = hashlib.md5(f"{book_id}{page_number}".encode()).hexdigest()
    s = CORP_MAP[ord(md5_hash[-1]) % 10]
    mod = height % s

    # A non-uniform image: a solid-colour JPEG is small and tolerant enough that
    # a garbled middle can still decode to the same pixels, which would hide the
    # corruption this fixture exists to reproduce.
    raw = bytes((i * 137 + (i // 7) * 31) % 256 for i in range(width * height * 3))
    orig = Image.frombytes("RGB", (width, height), raw)

    # Invert slices to produce scrambled image
    scrambled = Image.new("RGB", (width, height))
    for block_index in range(s):
        d = height // s
        dest_y = d * block_index
        src_y = height - d * (block_index + 1) - mod
        if block_index == 0:
            d += mod
        else:
            dest_y += mod
        slice_img = orig.crop((0, dest_y, width, dest_y + d))
        scrambled.paste(slice_img, (0, src_y))

    buf = io.BytesIO()
    scrambled.save(buf, format="JPEG")
    raw_img = buf.getvalue()

    # Prepend header: [file_type=0, scramble=0, b2, b3, b4, b5, b6, b7]
    header = bytearray(8)
    header[0] = 0  # JPEG
    header[1] = 0  # scrambled
    header[2] = (book_id >> 8) & 0xFF
    header[3] = book_id & 0xFF
    header[4] = (page_number >> 24) & 0xFF
    header[5] = (page_number >> 16) & 0xFF
    header[6] = (page_number >> 8) & 0xFF
    header[7] = page_number & 0xFF

    payload = bytes(header) + raw_img
    half = len(payload) // 2
    return _encrypt_stream(payload[:half]), _encrypt_stream(payload[half:])


def test_decrypt_chunks_round_trips():
    c0, c1 = _create_mock_scrambled_image(720, 800, 3751, 2965447)
    unscrambled_bytes = unscramble_image(decrypt_chunks([c0, c1]))
    img = Image.open(io.BytesIO(unscrambled_bytes))
    assert img.size == (720, 800)


def test_single_stream_decrypt_corrupts_the_page():
    """Regression: chaining CBC across the chunk boundary garbles chunk 2.

    This is what produced the "broken data stream when reading image file"
    warnings — and the silently corrupted pages the user reported.
    """
    c0, c1 = _create_mock_scrambled_image(720, 800, 3751, 2965447)
    good = unscramble_image(decrypt_chunks([c0, c1]))
    try:
        bad = unscramble_image(decrypt_and_unscramble_image(c0 + c1))
    except OSError:
        return  # PIL refused the garbled stream outright
    assert bad != good, "single-stream CBC must not silently equal the correct page"


@pytest.mark.asyncio
async def test_comicbox_fetcher():
    mock_base = MockFetcher()
    c0, c1 = _create_mock_scrambled_image(720, 800, 3751, 2965447)
    seen: list[tuple[str, dict]] = []

    async def mock_get_bytes(url: str, **kwargs):
        seen.append((url, kwargs))
        if url.endswith(".b_0"):
            return c0
        if url.endswith(".b_1"):
            return c1
        return b""

    mock_base.get_bytes_mock.side_effect = mock_get_bytes
    fetcher = ComicBoxFetcher(mock_base)

    data = await fetcher.get_bytes(
        "https://cdn.example.com/break_2/static/upload/book/3751/42031/2965447.jpg"
    )
    img = Image.open(io.BytesIO(data))
    assert img.size == (720, 800)
    # Ciphertext chunks must skip the image sniff: random bytes starting with
    # "[" or "{" were otherwise rejected as a JSON error page.
    assert seen and all(kwargs.get("sniff") is False for _, kwargs in seen)


@pytest.mark.asyncio
async def test_comicbox_fetcher_chunk_starting_with_bracket():
    """A chunk whose first byte is `{` must still be decrypted, not rejected."""
    mock_base = MockFetcher()
    c0, c1 = _create_mock_scrambled_image(720, 800, 3751, 2965447)

    async def mock_get_bytes(url: str, *, sniff: bool = True, **kwargs):
        if sniff:
            # Mimic HttpFetcher: ciphertext is not an image, so a body that
            # starts like JSON is rejected unless sniffing is off.
            raise FetchError("non-image response (content-type='application/octet-stream')")
        return c0 if url.endswith(".b_0") else c1

    mock_base.get_bytes_mock.side_effect = mock_get_bytes
    fetcher = ComicBoxFetcher(mock_base)

    data = await fetcher.get_bytes(
        "https://cdn.example.com/break_2/static/upload/book/3751/42031/2965447.jpg"
    )
    assert Image.open(io.BytesIO(data)).size == (720, 800)


@pytest.mark.asyncio
async def test_comicbox_plugin_search():
    cfg = AppConfig()
    mock_fetcher = MockFetcher()
    plugin = ComicBoxPlugin(cfg, mock_fetcher)

    html = """
    <html>
    <body>
        <a href="/book/3751">
            <img src="/cover.jpg" alt="秘密教學">
            <h3>秘密教學</h3>
        </a>
        <a href="/book/4484">
            <img data-src="/cover2.jpg" alt="御姐的實戰教學">
            <h3>御姐的實戰教學</h3>
        </a>
    </body>
    </html>
    """
    mock_fetcher.get_text_mock.return_value = html

    results = await plugin.search("秘密教學")
    # Fuzzy junk is filtered: only the exact-title hit survives (see _query_relevant).
    assert len(results) == 1
    assert results[0].ref == "3751"
    assert results[0].title == "秘密教學"


@pytest.mark.asyncio
async def test_comicbox_plugin_detail_and_chapters():
    cfg = AppConfig()
    mock_fetcher = MockFetcher()
    plugin = ComicBoxPlugin(cfg, mock_fetcher)

    book_html = """
    <html>
    <head><meta property="og:image" content="https://cdn.example.com/cover.jpg"></head>
    <body>
        <h1>秘密教學</h1>
        <div class="author">子豪</div>
        <div class="content">13歲那年成為孤兒...</div>
        <div class="chapter-list">
            <a href="/free-chapter/13411?t=20260415">▶ 第1話</a>
            <a href="/free-chapter/13412?t=20260415">▶ 第2話</a>
        </div>
    </body>
    </html>
    """
    chapter_html = """
    <html>
    <body>
        <div id="2965447" class="cropped" data-src="https://cdn.example.com/break_2/static/upload/book/3751/42031/2965447.jpg"></div>
        <div id="2965448" class="cropped" data-src="https://cdn.example.com/break_2/static/upload/book/3751/42031/2965448.jpg"></div>
    </body>
    </html>
    """

    async def mock_get_text(url: str, **kwargs):
        if "/book/" in url:
            return book_html
        return chapter_html

    mock_fetcher.get_text_mock.side_effect = mock_get_text

    detail = await plugin.get_detail(
        SearchResult(source_id="comicbox", title="秘密教學", ref="3751")
    )
    assert detail.title == "秘密教學"
    assert detail.author == "子豪"
    assert detail.cover_url == "https://cdn.example.com/cover_pc.jpg"

    chapters = await plugin.list_chapters(detail)
    assert len(chapters) == 2
    assert chapters[0].ref == "13411"
    assert chapters[0].title == "第1話"

    pages = await plugin.get_pages(chapters[0])
    assert len(pages) == 2
    # 0-based: the output writer names page 0 "001.webp". A 1-based index here
    # shifted every saved filename by one (first page became 002.webp).
    assert [p.index for p in pages] == [0, 1]
    assert "2965447.jpg" in pages[0].image_url

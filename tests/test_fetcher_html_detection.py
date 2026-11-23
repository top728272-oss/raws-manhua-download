"""Tests for the fetcher's HTML-detection + failover diagnostics.

Reproduces host failover diagnostics when a domain returns HTTP 200 with an
HTML page instead of JSON. We must (a) NOT crash with a cryptic JSON error, and
(b) fail over to the next host with a readable message.

Also covers `get_bytes` rejecting a body that is not really an image: an error
page served with HTTP 200 must never be saved as a page, and must never be
cached either.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import load_config
from app.core.cache import cache_file
from app.core.fetcher import FetchError, HttpFetcher, looks_like_text


@pytest.mark.asyncio
async def test_html_response_raises_readable_error() -> None:
    cfg = load_config()
    f = HttpFetcher(cfg)
    try:
        # First host: HTML page (dead domain). Second host: valid JSON.
        calls: list[str] = []

        async def fake_get_json(url, **kw):
            calls.append(url)
            if "dead" in url:
                # Simulate what get_json does on an HTML body: raise FetchError.
                raise FetchError(
                    "non-JSON response (content-type='text/html'); looks like an "
                    "HTML page. Body starts: '<!DOCTYPE html> <html> 服務器升級中'"
                )
            return {"code": 200, "results": {"list": []}}

        f.get_json = fake_get_json  # type: ignore[assignment]
        data = await f.get_json_failover(
            ["https://api.dead-domain.com", "https://api.live-domain.com"],
            "/api/v3/search/comic",
        )
        assert data["code"] == 200
        assert len(calls) == 2  # tried dead first, then live
        assert "live-domain" in calls[1]
    finally:
        await f.aclose()


@pytest.mark.asyncio
async def test_all_hosts_dead_raises_fetcherror() -> None:
    cfg = load_config()
    f = HttpFetcher(cfg)
    try:

        async def always_html(url, **kw):
            raise FetchError("non-JSON response; HTML page")

        f.get_json = always_html  # type: ignore[assignment]
        with pytest.raises(FetchError) as ei:
            await f.get_json_failover(["https://a.com", "https://b.com"], "/api/v3/search/comic")
        assert "all hosts failed" in str(ei.value)
    finally:
        await f.aclose()


# --------------------------------------------------------------------------- #
# get_bytes: a body that is not an image
# --------------------------------------------------------------------------- #


def test_looks_like_text_classifies_bodies() -> None:
    # Markup / JSON / plain text -> rejected.
    assert looks_like_text(b"<!DOCTYPE html><html><body>404</body></html>") is True
    assert looks_like_text(b"  <html><head></head></html>") is True
    assert looks_like_text(b'{"code":403,"message":"forbidden"}') is True
    assert looks_like_text(b'["error"]') is True
    assert looks_like_text(b"   ") is False  # whitespace only is not "text"
    assert looks_like_text(b"") is False

    # Real image magic -> accepted, including formats we do not explicitly know.
    assert looks_like_text(b"\xff\xd8\xff\xe0\x00\x10JFIF") is False
    assert looks_like_text(b"\x89PNG\r\n\x1a\n") is False
    assert looks_like_text(b"RIFF\x00\x00\x00\x00WEBPVP8 ") is False
    assert looks_like_text(b"GIF89a") is False
    assert looks_like_text(b"BM\x00\x00") is False
    assert looks_like_text(b"\x00\x00\x00\x18ftypavif") is False


@pytest.mark.asyncio
async def test_get_bytes_rejects_an_error_page_and_does_not_cache_it() -> None:
    cfg = load_config()
    f = HttpFetcher(cfg)
    url = "https://cdn.example.test/not-an-image-1.webp"
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<!DOCTYPE html><html><body>404 Not Found</body></html>",
        )

    original = f._client
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(FetchError, match="non-image"):
            await f.get_bytes(url)
        # The bad body must not have entered the cache, or every later call would
        # keep "downloading" the error page.
        assert len(f._bytes_cache) == 0
        with pytest.raises(FetchError, match="non-image"):
            await f.get_bytes(url)
        assert calls == 2, "a rejected body must be re-fetched, never served from cache"
    finally:
        await f._client.aclose()
        f._client = original
        await f.aclose()


@pytest.mark.asyncio
async def test_get_bytes_accepts_and_caches_a_real_image() -> None:
    cfg = load_config()
    f = HttpFetcher(cfg)
    url = "https://cdn.example.test/real-image-1.webp"
    body = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "image/webp"}, content=body)

    original = f._client
    f._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await f.get_bytes(url) == body
        assert await f.get_bytes(url) == body
        assert calls == 1, "the second call must be served from the in-memory cache"
        assert f._bytes_cache.size_bytes == len(body)
        # Page bytes are held in memory only — never written to the cover cache.
        assert not cache_file(url).exists()
    finally:
        await f._client.aclose()
        f._client = original
        await f.aclose()

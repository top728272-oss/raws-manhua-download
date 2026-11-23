"""Fetch abstraction layer.

Two backends behind one interface:
  - HttpFetcher       -> httpx
  - PlaywrightFetcher  -> headless chromium (reserved for future sources)

A plugin declares its `fetch_mode`; the registry/download engine hands it the
matching fetcher. Polite pacing (request_delay_ms) and proxy config live here so
individual plugins don't each reinvent throttling.

The `*_failover` helpers try a list of hosts in order, returning the first success
when a source rotates its API domains.

Fetched image bytes are cached **in memory only**, for the lifetime of the fetcher
(one per source plugin). Page images are never written to
disk: the downloaded library is the real store, and a disk cache keyed on
``md5(url)`` cannot work for the sources that serve signed, expiring page URLs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.config import AppConfig
from app.core.cache import MemoryBytesCache
from app.core.models import FetchMode

logger = logging.getLogger(__name__)


class FetchError(Exception):
    """Raised when all hosts/attempts for a request fail."""


_SNIFF_BYTES = 512


_BYTES_CACHE_MAX_ENTRIES = 256
_BYTES_CACHE_MAX_BYTES = 64 * 1024 * 1024


def looks_like_text(data: bytes) -> bool:
    """True when a body is HTML/XML/JSON/text rather than image bytes.

    Image CDNs answer with an error page or a JSON error - sometimes with HTTP
    200 - when a URL is wrong, expired or hotlink-blocked. Without this check
    such a body is happily saved as if it were a page, so the chapter ends up
    containing an error page that looks like a successful download.

    Only markup/text shapes are rejected; every image format starts with binary
    magic (``\\xff\\xd8\\xff`` JPEG, ``RIFF….WEBP``, ``\\x89PNG``, ``GIF8``, …)
    so an unrecognised *binary* body is still accepted, which keeps exotic
    formats working.
    """
    head = data[:_SNIFF_BYTES].lstrip()
    if not head:
        return False
    if head[:1] in (b"<", b"{", b"["):
        return True
    lowered = head.lower()
    return b"<!doctype" in lowered or b"<html" in lowered


def status_code_of(exc: BaseException | None) -> int | None:
    """The HTTP status carried by ``exc`` (or its cause chain), when any.

    ``httpx`` surfaces a bad status as ``httpx.HTTPStatusError`` raised by
    ``raise_for_status()``; the status itself sits on ``.response``. Plugins
    sometimes wrap that error, so the ``__cause__``/``__context__`` chain is
    walked and the status is duck-typed off ``status_code`` attributes.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for candidate in (current, getattr(current, "response", None)):
            status = getattr(candidate, "status_code", None)
            if isinstance(status, int):
                return status
        current = current.__cause__ or current.__context__
    return None


_RETRYABLE_STATUSES = frozenset({408, 429})


def is_permanent_client_error(exc: BaseException | None) -> bool:
    """True for 4xx responses that will never succeed by retrying the same url."""
    status = status_code_of(exc)
    return status is not None and 400 <= status < 500 and status not in _RETRYABLE_STATUSES


def is_server_error(exc: BaseException | None) -> bool:
    """True when ``exc`` - or anything in its cause/context chain - is an HTTP 5xx.

    ``httpx`` surfaces a bad status as ``httpx.HTTPStatusError`` (raised by
    ``raise_for_status()``); plugins sometimes wrap that in their own error, so
    the whole ``__cause__``/``__context__`` chain is walked rather than only the
    outermost exception. The check is duck-typed on ``status_code`` so it also
    catches non-httpx errors that carry the status directly.

    A 5xx means "the server broke", not "the request was wrong" - which is what
    makes it the trigger for the cross-source fallback in ``downloader``.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for candidate in (current, getattr(current, "response", None)):
            status = getattr(candidate, "status_code", None)
            if isinstance(status, int) and 500 <= status < 600:
                return True
        current = current.__cause__ or current.__context__
    return False


class BaseFetcher:
    async def get_json(self, url: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def post_json(self, url: str, json: Any = None, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def post_text(self, url: str, data: Any = None, **kwargs: Any) -> str:
        raise NotImplementedError

    async def get_text(self, url: str, **kwargs: Any) -> str:
        raise NotImplementedError

    async def get_text_with_headers(self, url: str, **kwargs: Any) -> tuple[str, dict[str, str]]:
        """GET a body as text together with the response headers (lowercased keys).

        For sources whose response decryption keys off a response header
        (mimei's per-response IV seed lives in ``x-response-length``).
        """
        raise NotImplementedError

    async def post_text_with_headers(
        self, url: str, json: Any = None, data: Any = None, **kwargs: Any
    ) -> tuple[str, dict[str, str]]:
        raise NotImplementedError

    async def get_bytes(self, url: str, *, sniff: bool = True, **kwargs: Any) -> bytes:
        raise NotImplementedError

    async def get_json_failover(self, hosts: list[str], path: str, **kwargs: Any) -> Any:
        """Try `{host}{path}` for each host in order; return first JSON success."""
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


class HttpFetcher(BaseFetcher):
    """httpx-backed fetcher with proxy support and polite pacing."""

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._delay = cfg.network.request_delay_ms / 1000.0
        self._last_request = 0.0
        self._lock = asyncio.Lock()

        self._bytes_cache = MemoryBytesCache(_BYTES_CACHE_MAX_ENTRIES, _BYTES_CACHE_MAX_BYTES)

        headers = {"User-Agent": cfg.network.user_agent}
        proxy = cfg.network.proxy or None
        self._client = httpx.AsyncClient(
            headers=headers,
            proxy=proxy,
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )
        logger.debug("HttpFetcher ready (proxy=%s, delay=%.2fs)", proxy, self._delay)

    async def _pace(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._delay - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        await self._pace()
        logger.debug("GET(json) %s", url)
        resp = await self._client.get(url, **kwargs)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        body = resp.text
        looks_like_html = body.lstrip()[:1] == "<"
        if "json" not in ctype.lower() and looks_like_html:
            snippet = " ".join(body.split())[:180]
            raise FetchError(
                f"non-JSON response (content-type={ctype!r}); "
                f"looks like an HTML page. Body starts: {snippet!r}"
            )
        try:
            return resp.json()
        except Exception as exc:
            snippet = " ".join(body.split())[:180]
            raise FetchError(f"JSON parse failed ({exc}); body starts: {snippet!r}")

    async def post_json(self, url: str, json: Any = None, **kwargs: Any) -> Any:
        await self._pace()
        logger.debug("POST(json) %s", url)
        resp = await self._client.post(url, json=json, **kwargs)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception as exc:
            body = resp.text
            snippet = " ".join(body.split())[:180]
            raise FetchError(f"JSON parse failed ({exc}); body starts: {snippet!r}")

    async def post_text(self, url: str, data: Any = None, **kwargs: Any) -> str:
        await self._pace()
        logger.debug("POST(text) %s", url)
        resp = await self._client.post(url, data=data, **kwargs)
        resp.raise_for_status()
        return resp.text

    async def get_text(self, url: str, **kwargs: Any) -> str:
        await self._pace()
        logger.debug("GET(text) %s", url)
        resp = await self._client.get(url, **kwargs)
        resp.raise_for_status()
        return resp.text

    async def get_text_with_headers(self, url: str, **kwargs: Any) -> tuple[str, dict[str, str]]:
        await self._pace()
        logger.debug("GET(text+headers) %s", url)
        resp = await self._client.get(url, **kwargs)
        resp.raise_for_status()
        return resp.text, {k.lower(): v for k, v in resp.headers.items()}

    async def post_text_with_headers(
        self, url: str, json: Any = None, data: Any = None, **kwargs: Any
    ) -> tuple[str, dict[str, str]]:
        await self._pace()
        logger.debug("POST(text+headers) %s", url)
        resp = await self._client.post(url, json=json, data=data, **kwargs)
        resp.raise_for_status()
        return resp.text, {k.lower(): v for k, v in resp.headers.items()}

    async def get_bytes(self, url: str, *, sniff: bool = True, **kwargs: Any) -> bytes:
        """Fetch a body as bytes.

        ``sniff=False`` skips the "is this really an image?" check for URLs that
        are known to hold raw binary rather than an image - ComicBox's ``.b_N``
        ciphertext chunks are random bytes, so a chunk starting with ``[`` or
        ``{`` was being rejected as a JSON error page. An unvalidated body is
        never cached, so a transient error page cannot poison later retries.
        """
        cached = self._bytes_cache.get(url)
        if cached is not None:
            logger.debug("MEMCACHE HIT(bytes) %s", url)
            return cached

        await self._pace()
        logger.debug("GET(bytes) %s", url)
        resp = await self._client.get(url, **kwargs)
        resp.raise_for_status()
        data = resp.content

        if sniff and data and looks_like_text(data):
            ctype = resp.headers.get("content-type", "")
            snippet = " ".join(data[:180].decode("utf-8", "replace").split())
            raise FetchError(
                f"non-image response (content-type={ctype!r}) from {url}; body starts: {snippet!r}"
            )

        if sniff:
            self._bytes_cache.put(url, data)

        return data

    async def get_json_failover(self, hosts: list[str], path: str, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for host in hosts:
            url = f"{host.rstrip('/')}{path}"
            try:
                data = await self.get_json(url, **kwargs)

                if data is None:
                    raise FetchError("empty JSON body")
                return data
            except Exception as exc:
                last_exc = exc
                logger.warning("host failed (%s): %s", url, exc)
        raise FetchError(f"all hosts failed for {path}: {last_exc}")

    async def aclose(self) -> None:
        await self._client.aclose()


class PlaywrightFetcher(BaseFetcher):
    """Headless-browser fetcher. STUB - implemented when JS sources are added."""

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        logger.debug("PlaywrightFetcher constructed (not yet implemented)")

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        raise NotImplementedError("PlaywrightFetcher.get_json - add with JS sources")

    async def post_json(self, url: str, json: Any = None, **kwargs: Any) -> Any:
        raise NotImplementedError("PlaywrightFetcher.post_json - add with JS sources")

    async def post_text(self, url: str, data: Any = None, **kwargs: Any) -> str:
        raise NotImplementedError("PlaywrightFetcher.post_text - add with JS sources")

    async def get_text(self, url: str, **kwargs: Any) -> str:
        raise NotImplementedError("PlaywrightFetcher.get_text - add with JS sources")

    async def get_text_with_headers(self, url: str, **kwargs: Any) -> tuple[str, dict[str, str]]:
        raise NotImplementedError("PlaywrightFetcher.get_text_with_headers - add with JS sources")

    async def post_text_with_headers(
        self, url: str, json: Any = None, data: Any = None, **kwargs: Any
    ) -> tuple[str, dict[str, str]]:
        raise NotImplementedError("PlaywrightFetcher.post_text_with_headers - add with JS sources")

    async def get_bytes(self, url: str, *, sniff: bool = True, **kwargs: Any) -> bytes:
        raise NotImplementedError("PlaywrightFetcher.get_bytes - add with JS sources")


def make_fetcher(mode: FetchMode, cfg: AppConfig) -> BaseFetcher:
    if mode == FetchMode.HTTP:
        return HttpFetcher(cfg)
    if mode == FetchMode.PLAYWRIGHT:
        return PlaywrightFetcher(cfg)
    raise ValueError(f"Unknown fetch mode: {mode}")

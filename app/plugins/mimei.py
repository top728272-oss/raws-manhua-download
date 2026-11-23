"""谜妹漫画 / Mimei (com.mimei17) source plugin - the Android app's own API.

Speaks the protocol of the 谜妹漫画 Android client (v1.9.6), reconstructed from
the app with the app's `libsecrets.so` key material. Everything is envelope
encrypted: the API answers base64 AES-CBC JSON, and the image CDN serves
AES-CBC ciphertext where a plain JPEG should be. Bootstrap hosts, keys and
key/iv pairs were verified live on 2026-10-18.

Protocol::

    bootstrap  GET  {public host}/host_app.txt
                    base64( AES-256-CBC( json, HOST_KEY, HOST_IV ) )
                    -> api/comic server pools (the comic pool carries the image
                       host `tsurl` and the `chain` id every call needs)
    register   POST /v1/register  {universal_id, platform:2, model, key, ...}
                    key = sha256("hcg" + universal_id + "hcg")
                    -> success.token (Authorization: Bearer <token>)
    response        base64( AES-256-CBC( json,
                             key = md5(API_KEY) as 32 ascii bytes,
                             iv  = md5(X-Response-Length header)[8:24] ) )
    images     read() returns ABSOLUTE urls on the API host, but the bytes only
               exist on that pool's `tsurl` host - the url's host is swapped,
               the body decrypted with one of the fixed IMG_KEYS (per chain;
               CV2 on the current one), PKCS#7 stripped -> JPEG.

The catalogue is one-shot doujinshi: a comic row IS a book, so search results
are per-book and most series answer `directory` with a single entry. The
plugin still walks the directory generically - a multi-entry answer becomes a
real chapter list, ordered with None-last sorting on volume/chapter/number.

Cover urls carry a `#mimei` fragment (never sent to the server) that tells the
GUI cover loader the payload needs the same image decryption - see
``app.gui.cover_loader``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
from typing import Any
from urllib.parse import urlparse

from Crypto.Cipher import AES
from PIL import Image

from app.config import AppConfig
from app.core.fetcher import BaseFetcher, FetchError
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)


API_KEY = "6e561ccd4aade2fed458d4da61e76770"
HOST_KEY = base64.b64decode("h7lkUI0kb25Usb3qTFLefTf8b+8Hs2Y63sANNraUm/Q=")
HOST_IV = base64.b64decode("k54Vcjbp6ByR6D83eIoxgw==")


IMG_KEYS: dict[str, tuple[str, str]] = {
    "CV2": (
        "D94E86BC48615C397146E9A1CA0870E244F2C7920D7535D2F20039E9D54AD810",
        "CA87D9018CC39C86FA25E7876AC079CB",
    ),
    "C": ("DD217E1B3F959CA64998445205D7C64C", "971ED8AACAC2B85FDA491FCD6276FE61"),
    "CV3": (
        "E85FC2DB6DC3C0F6FC1A9CB3D5921B85DED22788BAE7EE50F8FD1B9E68BE4C13",
        "F5917556BA2DCBF1EFB6C16F514E5696",
    ),
    "A": ("204149A15B092E24E89CEB31040148DB", "AC14AECE2E0681EE730A4D773F3B2EE6"),
    "AD": ("B2F3842866F9583D1ECE61C4E055C255", "E01EDE6331D37AFCC7BE05597D654D22"),
}


BOOTSTRAP_HOSTS = [
    "https://weifhiu.meme06.live",
    "https://public.mino02.live",
    "https://public.micd04.live",
    "https://public2t.muqhcmn.cn",
    "https://public2t.hpmleip.cn",
]


READ_PAGE_SIZE = 100


MAX_READ_PAGES = 40


MAX_CHAPTERS = 500


def _unpad_pkcs7(data: bytes) -> bytes:
    if data and 1 <= data[-1] <= 16:
        return data[: len(data) - data[-1]]
    return data


def decrypt_api_body(text: str, headers: dict[str, str]) -> Any:
    """Open one API envelope: base64 -> AES-256-CBC -> JSON (or JSON-ish text).

    The key is md5(API_KEY) as its 32 hex-ascii bytes; the IV is the middle 16
    hex chars of md5 of the ``x-response-length`` header. The plaintext starts
    with a 16-byte IV block before the JSON, which :func:`_json_from` skips.
    """
    ct = base64.b64decode(text.strip())
    key = hashlib.md5(API_KEY.encode()).hexdigest().encode()
    seed = headers.get("x-response-length", "")
    iv = hashlib.md5(seed.encode()).hexdigest()[8:24].encode()
    plain = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
    return _json_from(plain)


def _json_from(plain: bytes) -> Any:
    i = plain.find(b"{")
    if i < 0:
        i = plain.find(b"[")
    if i < 0:
        raise ValueError("no json in plaintext")
    obj, _ = json.JSONDecoder().raw_decode(plain[i:].decode("utf-8", "replace"))
    return obj


def decrypt_image(data: bytes, key_order: list[str] | None = None) -> bytes:
    """Decrypt one image payload, trying key candidates until PIL validates.

    Used for both chapter pages and covers; the ciphertext is random-looking
    bytes, hence the strict PIL acceptance instead of magic-byte sniffing.
    """
    order = key_order or list(IMG_KEYS)
    ct = data[: len(data) // 16 * 16]
    for name in order:
        spec = IMG_KEYS.get(name)
        if spec is None:
            continue
        key, iv = bytes.fromhex(spec[0]), bytes.fromhex(spec[1])
        plain = _unpad_pkcs7(AES.new(key, AES.MODE_CBC, iv).decrypt(ct))
        try:
            img = Image.open(io.BytesIO(plain))
            img.verify()
        except Exception:
            continue
        return plain
    raise FetchError(f"mimei: no image key decrypted this payload ({len(data)} bytes)")


def cover_url_from_path(tsurl: str, path: str | None) -> str | None:
    """Absolute cover URL with the ``#mimei`` decrypt marker the GUI looks for."""
    if not path:
        return None
    if path.startswith("http"):
        parsed = urlparse(path)
        pool = urlparse(tsurl) if tsurl else None
        if pool and pool.netloc and parsed.netloc and parsed.netloc != pool.netloc:
            kept = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            path = f"{pool.scheme or 'https'}://{pool.netloc}{kept}"
        return path + "#mimei"
    return tsurl.rstrip("/") + "/" + path.lstrip("/") + "#mimei"


class MimeiFetcher(BaseFetcher):
    """Decrypts every image body before it reaches the download engine.

    The engine calls ``get_bytes(page.image_url)``; the ciphertext must become
    a real image here, with the sniff disabled (random-looking bytes, and the
    acceptance test is the PIL round-trip in :func:`decrypt_image`). The key
    that worked last is tried first - one PIL validation per page thereafter.
    """

    def __init__(self, raw: BaseFetcher, key_order: list[str]) -> None:
        self._raw = raw
        self._key_order = list(key_order)
        self._working_key: str | None = None

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        return await self._raw.get_json(url, **kwargs)

    async def post_json(self, url: str, json: Any = None, **kwargs: Any) -> Any:
        return await self._raw.post_json(url, json=json, **kwargs)

    async def post_text(self, url: str, data: Any = None, **kwargs: Any) -> str:
        return await self._raw.post_text(url, data=data, **kwargs)

    async def get_text(self, url: str, **kwargs: Any) -> str:
        return await self._raw.get_text(url, **kwargs)

    async def get_text_with_headers(self, url: str, **kwargs: Any) -> tuple[str, dict[str, str]]:
        return await self._raw.get_text_with_headers(url, **kwargs)

    async def post_text_with_headers(
        self, url: str, json: Any = None, data: Any = None, **kwargs: Any
    ) -> tuple[str, dict[str, str]]:
        return await self._raw.post_text_with_headers(url, json=json, data=data, **kwargs)

    async def get_bytes(self, url: str, *, sniff: bool = True, **kwargs: Any) -> bytes:
        ct = await self._raw.get_bytes(url, sniff=False, **kwargs)
        order = self._key_order
        if self._working_key and self._working_key in order:
            order = [self._working_key] + [k for k in order if k != self._working_key]
        plain = decrypt_image(ct, order)
        head = url.rsplit("/", 1)[-1]
        if order and order[0] != self._working_key:
            logger.debug("mimei image key %s accepted for %s", order[0], head)
        self._working_key = order[0]
        return plain

    async def aclose(self) -> None:
        await self._raw.aclose()


def _sort_key(entry: dict[str, Any]) -> tuple[int, Any, ...]:
    """Reading order for directory entries; None sorts last within a field."""

    def part(value: Any) -> tuple[int, Any]:
        if value in (None, ""):
            return (1, 0)
        try:
            return (0, float(value))
        except (TypeError, ValueError):
            return (0, str(value))

    return (
        part(entry.get("volume")),
        part(entry.get("chapter")),
        part(entry.get("season")),
        part(entry.get("series_number")),
        part(entry.get("id")),
    )


class MimeiPlugin(SourcePlugin):
    id = "mimei"
    name = "谜妹漫画 / Mimei (18+)"
    category = "18+"
    fetch_mode = FetchMode.HTTP

    def __init__(self, cfg: AppConfig, fetcher: BaseFetcher) -> None:
        super().__init__(cfg, fetcher)
        self._raw = fetcher
        self.fetcher = MimeiFetcher(fetcher, self.key_order)

        self._api_host: str | None = None
        self._chain: str = ""
        self._image_host: str | None = None
        self._token: str | None = None
        self._ready_lock = asyncio.Lock()

    @property
    def key_order(self) -> list[str]:
        keys = self.settings.get("image_keys")
        if isinstance(keys, list) and keys:
            return [str(k) for k in keys if k in IMG_KEYS]
        return list(IMG_KEYS)

    @property
    def server_index(self) -> int:
        try:
            return max(0, int(self.settings.get("server_index", 0)))
        except (TypeError, ValueError):
            return 0

    @property
    def search_counts(self) -> int:
        try:
            return max(1, int(self.settings.get("search_counts", 40)))
        except (TypeError, ValueError):
            return 40

    @property
    def search_type(self) -> int:
        """Search shelf: 1 = Japanese doujinshi books, 4 = Chinese-translated
        Korean webtoons (one comic row per chapter). Any row of a serial still
        expands to the whole series via directory()."""
        try:
            return max(1, int(self.settings.get("search_type", 1)))
        except (TypeError, ValueError):
            return 1

    async def _ensure_ready(self) -> None:
        """Bootstrap the server pool and register an anonymous device (once)."""
        async with self._ready_lock:
            if self._token and self._api_host:
                return
            pool = self.settings.get("bootstrap_hosts")
            hosts = (
                [str(h).rstrip("/") for h in pool]
                if isinstance(pool, list) and pool
                else BOOTSTRAP_HOSTS
            )
            last_exc: Exception | None = None
            for host in hosts:
                try:
                    text = await self._raw.get_text(f"{host}/host_app.txt")
                    cfg = _json_from(
                        AES.new(HOST_KEY, AES.MODE_CBC, HOST_IV).decrypt(
                            base64.b64decode(text.strip())
                        )
                    )
                    servers = [s for s in cfg.get("comic", []) if s.get("url") and s.get("tsurl")]
                    if not servers:
                        raise ValueError("bootstrap config has no comic servers")
                except Exception as exc:
                    last_exc = exc
                    logger.warning("mimei bootstrap via %s failed: %s", host, exc)
                    continue
                start = min(self.server_index, len(servers) - 1)
                ordered = servers[start:] + servers[:start]
                for server in ordered:
                    api_host = str(server["url"]).rstrip("/")
                    image_host = str(server["tsurl"]).rstrip("/")
                    chain = str(server.get("chain") or "")
                    self._api_host = api_host
                    self._image_host = image_host
                    self._chain = chain
                    self._token = None
                    try:
                        await self._register()
                    except Exception as exc:
                        last_exc = exc
                        logger.warning("mimei server %s refused registration: %s", api_host, exc)
                        continue
                    logger.info("mimei using server %s", api_host)
                    return
                self._api_host = None
                self._image_host = None
                self._chain = ""
            raise FetchError(f"mimei bootstrap failed on all hosts and servers: {last_exc}")

    async def _register(self) -> None:
        universal_id = hashlib.md5(os.urandom(16)).hexdigest()
        key = hashlib.sha256(f"hcg{universal_id}hcg".encode()).hexdigest()
        body = {
            "universal_id": universal_id,
            "platform": 2,
            "model": "samsung_SM-A546E",
            "key": key,
            "install": "tgmimei",
            "source": "",
        }
        text, headers = await self._raw.post_text_with_headers(
            f"{self._api_host}/v1/register", json=body
        )
        payload = decrypt_api_body(text, headers)
        token = (payload.get("success") or {}).get("token") if isinstance(payload, dict) else None
        if not token:
            raise FetchError(f"mimei register refused: {str(payload)[:120]}")
        self._token = token
        logger.info("mimei registered (universal_id %s…)", universal_id[:8])

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _api_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_ready()
        url = f"{self._api_host}/{path.lstrip('/')}"
        text, headers = await self._raw.get_text_with_headers(
            url, params=params, headers=self._auth_headers()
        )
        return self._payload(decrypt_api_body(text, headers), path)

    def _payload(self, decoded: Any, what: str) -> dict[str, Any]:
        if not isinstance(decoded, dict):
            raise FetchError(f"mimei {what}: unexpected payload {type(decoded).__name__}")
        if "error" in decoded:
            raise FetchError(f"mimei {what} refused: {str(decoded['error'])[:160]}")
        success = decoded.get("success")
        if not isinstance(success, dict):
            raise FetchError(f"mimei {what}: no success object in answer")
        return success

    async def search(self, query: str) -> list[SearchResult]:
        query = (query or "").strip()
        if not query:
            return []

        await self._ensure_ready()

        if query.isdigit():
            return await self._search_by_id(query)

        try:
            success = await self._api_get(
                "/v2/search/comic",
                {
                    "chain": self._chain,
                    "keywords": query,
                    "field": "name",
                    "type": self.search_type,
                    "order": "new",
                    "counts": self.search_counts,
                    "pages": 1,
                },
            )
        except Exception as err:
            logger.warning("mimei search failed for '%s': %s", query, err)
            return []

        results = [
            self._search_result(item)
            for item in success.get("data") or []
            if isinstance(item, dict) and item.get("id") is not None
        ]
        logger.info("mimei: search '%s' parsed %d results", query, len(results))
        return results

    async def _search_by_id(self, comic_id: str) -> list[SearchResult]:
        """A pasted bare comic id resolves straight to its book."""
        try:
            detail = await self.get_detail(
                SearchResult(source_id=self.id, title=comic_id, ref=comic_id)
            )
        except Exception as err:
            logger.warning("mimei id lookup %s failed: %s", comic_id, err)
            return []
        return [
            SearchResult(
                source_id=self.id,
                title=detail.title,
                ref=detail.ref,
                cover_url=detail.cover_url,
                author=detail.author,
            )
        ]

    def _search_result(self, item: dict[str, Any]) -> SearchResult:
        comic_id = str(item["id"])
        return SearchResult(
            source_id=self.id,
            title=str(item.get("name") or comic_id).strip(),
            ref=comic_id,
            cover_url=cover_url_from_path(self._image_host or "", item.get("cover_path")),
            author=self._authors_of(item),
            extra={"pages": item.get("pages")},
        )

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        await self._ensure_ready()
        comic_id = str(result.ref)
        success = await self._api_get(
            f"/v2/comic/summary/{comic_id}",
            {"host": self._api_host, "chain": self._chain},
        )
        data = success.get("data") or []
        comic = next((e for e in data if isinstance(e, dict)), None)
        if comic is None:
            raise FetchError(f"mimei summary for {comic_id} carried no book")

        tags = [str(t).strip() for t in (comic.get("tag") or []) if str(t).strip()]
        end = comic.get("end")
        return MangaDetail(
            source_id=self.id,
            title=str(comic.get("name") or result.title or comic_id).strip(),
            ref=comic_id,
            cover_url=cover_url_from_path(
                self._image_host or "", comic.get("full_cover_path") or comic.get("cover_path")
            )
            or result.cover_url,
            author=self._authors_of(comic) or result.author,
            description=None,
            status=("已完结" if end == "Y" else "连载中") if end in ("Y", "N") else None,
            tags=tags,
            extra={"pages": comic.get("pages"), "series_hash_key": comic.get("series_hash_key")},
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        await self._ensure_ready()
        comic_id = detail.ref
        entries: list[dict[str, Any]] = []
        try:
            success = await self._api_get(
                f"/v2/comic/directory/{comic_id}",
                {"host": self._api_host, "chain": self._chain, "last_stamp": 0},
            )
            entries = [e for e in (success.get("data") or []) if isinstance(e, dict)]
        except Exception as err:
            logger.warning(
                "mimei directory for %s failed (%s); falling back to the book itself", comic_id, err
            )

        entries = sorted(entries, key=_sort_key)[:MAX_CHAPTERS] or [
            {"id": comic_id, "name": detail.title, "pages": (detail.extra or {}).get("pages")}
        ]

        chapters: list[Chapter] = []
        for position, entry in enumerate(entries, start=1):
            entry_id = str(entry.get("id") or comic_id)
            title = str(entry.get("name") or "").strip() or detail.title
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=detail.ref,
                    title=title,
                    ref=entry_id,
                    index=position,
                    extra={"pages": entry.get("pages")},
                )
            )
        logger.info("mimei: %d chapter(s) for '%s'", len(chapters), detail.title)
        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        await self._ensure_ready()
        expected = int(chapter.extra.get("pages") or 0)

        urls: list[str] = []
        for page_no in range(1, MAX_READ_PAGES + 1):
            success = await self._api_get(
                "/v2/comic/read",
                {
                    "id": chapter.ref,
                    "counts": READ_PAGE_SIZE,
                    "pages": page_no,
                    "host": self._api_host,
                    "chain": self._chain,
                },
            )
            batch = [str(u) for u in (success.get("data") or []) if u]
            urls.extend(batch)
            if (expected and len(urls) >= expected) or len(batch) < READ_PAGE_SIZE:
                break
        if not urls:
            logger.warning("mimei: chapter %s listed no pages", chapter.ref)
            return []

        pages = [
            Page(index=idx, image_url=self._swap_to_image_host(u)) for idx, u in enumerate(urls)
        ]
        logger.info("mimei: resolved %d pages for chapter %s", len(pages), chapter.ref)
        return pages

    def _swap_to_image_host(self, url: str) -> str:
        """read() urls point at the API host; the bytes only exist on `tsurl`."""
        if not self._api_host or not url.startswith(self._api_host):
            return url
        return self._image_host + url[len(self._api_host) :]

    def _authors_of(self, item: dict[str, Any]) -> str | None:
        raw = item.get("author")
        if isinstance(raw, list) and raw:
            return "、".join(str(a).strip() for a in raw if str(a).strip()) or None
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None

"""JM Comic (禁漫天堂 / 18comic) source plugin - official mobile-app API.

Speaks the protocol of the REAL JM app (the 2.1.x Android client of the
18comic.vip / 18comic.org site family), not the counterfeit "禁漫天堂 new" apps
circulating under random package names with a different, envelope-encrypted
backend. This platform is the one whose third-party downloaders (jmcomic,
JMComic-qt, ...) can fetch every album - 收费 ones included - because the
paywall lives in the app UI rather than in the data path.

Protocol (from the public reference implementation, behaviour-checked there):

  Request   GET {api_host}{path} with the app's headers:
              user-agent:   Android webview UA
              token:        md5("{ts}" + APP_TOKEN_SECRET)                (hex)
              tokenparam:   "{ts},{app_version}"
            /chapter_view_template alone signs with APP_TOKEN_SECRET_2 - the
            regular secret answers 403 on that one endpoint.

  Response  {"code": 200, "data": "<base64>"} where data is AES-ECB ciphertext
            keyed on md5("{ts}" + APP_DATA_SECRET) as a 32-byte utf-8 string,
            PKCS#7 padded, plaintext = JSON. Errors can also arrive encrypted
            or as empty lists with an errorMsg, so code/shape checks come
            before decryption.

  Domains   The API host pool rotates. When the static pool fails wholesale the
            plugin reads JM's own domain-server (object-storage text: an
            AES-ECB+b64 JSON keyed on a separate secret) for the live pool.

  Images    https://{image_host}/media/photos/{photo_id}/{name}
            Real image files - but albums at/above their scramble_id are
            transported VERTICALLY SHUFFLED: ceil(h/num)-row strips ordered
            bottom-to-top. num = f(scramble_id, photo_id, filename) per page.
            The fetcher wrapper un-shuffles before the bytes reach the download
            engine (same delegation shape as the ComicBox plugin).

  Paywall   Albums marked 收费 are served whole by /chapter; no login, no
            coins. Missing/invalid ids answer an encrypted error and raise.

Every outbound call here carries its own short httpx timeout: JM's API answers
in ~1-2 s, while its domain-server/CDN edges have been observed to hold
ESTABLISHED TCP connections with no bytes for many minutes, which would
otherwise stall a whole download batch.

Direct album ids and 18comic.vip URLs are accepted as searches ("360123",
"jm360123", or a bare album URL all resolve to the album).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import re
import time
from typing import Any
from urllib.parse import quote, urlparse

from Crypto.Cipher import AES
from PIL import Image

from app.config import AppConfig
from app.core.fetcher import BaseFetcher, FetchError
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)


APP_VERSION = "2.1.7"
APP_TOKEN_SECRET = "185Hcomic3PAPP7R"
APP_TOKEN_SECRET_2 = "18comicAPPContent"
APP_DATA_SECRET = "185Hcomic3PAPP7R"
DOMAIN_SERVER_SECRET = "diosfjckwpqpdfjkvnqQjsik"

DEFAULT_API_DOMAINS = [
    "www.cdnhjk.net",
    "www.cdngwc.cc",
    "www.cdngwc.net",
    "www.cdngwc.club",
]
DEFAULT_IMAGE_DOMAINS = [
    "cdn-msp.jmapiproxy1.cc",
    "cdn-msp.jmapiproxy2.cc",
    "cdn-msp2.jmapiproxy2.cc",
    "cdn-msp3.jmapiproxy2.cc",
    "cdn-msp.jmapinodeudzn.net",
    "cdn-msp3.jmapinodeudzn.net",
]

DOMAIN_SERVER_URLS = [
    "https://rup4a04-c01.tos-ap-southeast-1.bytepluses.com/newsvr-2025.txt",
    "https://rup4a04-c02.tos-cn-hongkong.bytepluses.com/newsvr-2025.txt",
    "https://rup4a04-c03.tos-cn-beijing.bytepluses.com.cn/newsvr-2025.txt",
]


_API_TIMEOUT_S = 15.0
_DOMAIN_SERVER_TIMEOUT_S = 8.0

APP_HEADERS = {
    "Accept-Encoding": "gzip, deflate",
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 9; V1938CT Build/PQ3A.190705.11211812; wv) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.114 "
        "Safari/537.36"
    ),
}

APP_IMAGE_HEADERS = {
    **APP_HEADERS,
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "X-Requested-With": "com.JMComic3.app",
}


SCRAMBLE_268850 = 268850
SCRAMBLE_421926 = 421926


SCRAMBLE_FALLBACK = 220980


def _md5hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _sign(ts: str, secret: str = APP_TOKEN_SECRET, version: str = APP_VERSION) -> dict[str, str]:
    return {**APP_HEADERS, "token": _md5hex(f"{ts}{secret}"), "tokenparam": f"{ts},{version}"}


def _unpad_pkcs7(data: bytes) -> bytes:
    if data and 1 <= data[-1] <= 16:
        return data[: len(data) - data[-1]]
    return data


def _aes_ecb_b64(key_ascii: str, cipher_b64: str | bytes) -> bytes:
    if isinstance(cipher_b64, str):
        cipher_b64 = cipher_b64.encode("ascii")
    key = _md5hex(key_ascii).encode("utf-8")
    return _unpad_pkcs7(AES.new(key, AES.MODE_ECB).decrypt(base64.b64decode(cipher_b64)))


def decrypt_server_list(text: str) -> list[str]:
    """Decode JM's domain-server file: b64 + AES-ECB keyed on its own secret,
    then {"Server": [...]} - with the reference client's leading non-ascii trim
    for BOM/preamble bytes."""
    body = text.lstrip()
    while body and not body[0].isascii():
        body = body[1:]
    data = json.loads(_aes_ecb_b64(DOMAIN_SERVER_SECRET, body).decode("utf-8", "replace"))
    servers = data.get("Server") if isinstance(data, dict) else None
    return [str(host).strip() for host in servers if str(host).strip()] if servers else []


def _version_gt(candidate: str, current: str) -> bool:
    def parts(v: str) -> list[int]:
        try:
            return [int(p) for p in str(v).split(".")]
        except ValueError:
            return []

    pc, pn = parts(candidate), parts(current)
    return len(pc) == len(pn) and pc > pn


def parse_jm_id(text: str) -> str | None:
    """Album id out of "360123", "jm360123" or an album URL path."""
    query = (text or "").strip()
    if not query:
        return None
    if query.startswith(("http://", "https://", "/")) or "album" in query.lower():
        target = query if query.startswith("http") else f"https://{query}"
        path = urlparse(target).path
        m = re.search(r"/album/(\d+)", path) or re.search(r"/photo/(\d+)", path)
        if m:
            return m.group(1)
    m = re.fullmatch(r"jm(\d{3,9})", query, re.I)
    if m:
        return m.group(1)
    if query.isdigit() and 3 <= len(query) <= 9:
        return query
    return None


def strip_bucket_num(scramble_id: int, photo_id: int, filename: str) -> int:
    """Vertical-strip count for one page (JmImageTool.get_num)."""
    if photo_id < int(scramble_id):
        return 0
    if photo_id < SCRAMBLE_268850:
        return 10
    field = 10 if photo_id < SCRAMBLE_421926 else 8
    digest = _md5hex(f"{photo_id}{filename}")
    return (ord(digest[-1]) % field) * 2 + 2


def unshuffle_strips(data: bytes, num: int) -> bytes:
    """Re-order the vertical strips of a shuffled page (JmImageTool.decode).

    The server swaps ceil(h/num)-row bands bottom-to-top; walk the same
    permutation in reverse. Re-encoded as JPEG - an animated page collapses to
    its first frame, matching the reference client.
    """
    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    out = Image.new("RGB", (w, h))
    over = h % num
    for i in range(num):
        move = h // num
        y_src = h - move * (i + 1) - over
        y_dst = move * i
        if i == 0:
            move += over
        else:
            y_dst += over
        out.paste(img.crop((0, y_src, w, y_src + move)), (0, y_dst))
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


class JmComicFetcher(BaseFetcher):
    """Adds the app's image headers and un-shuffles scrambled pages.

    The download engine calls ``get_bytes(page.image_url)`` with nothing but
    (maybe) a Referer, so the shuffle context (scramble_id / photo_id /
    filename) is recorded here by the plugin's ``get_pages`` and looked up per
    URL - the same delegation pattern as ComicBoxFetcher.
    """

    def __init__(self, raw: BaseFetcher, image_domains: list[str]) -> None:
        self._raw = raw
        self._scramble_info: dict[str, tuple[int, int, str]] = {}
        self._image_domains = list(image_domains)

    def remember_scramble(self, url: str, scramble_id: int, photo_id: int, filename: str) -> None:
        self._scramble_info[url] = (scramble_id, photo_id, filename)
        if len(self._scramble_info) > 4096:
            for stale in list(self._scramble_info)[:1024]:
                self._scramble_info.pop(stale, None)

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        return await self._raw.get_json(url, **kwargs)

    async def post_json(self, url: str, json: Any = None, **kwargs: Any) -> Any:
        return await self._raw.post_json(url, json=json, **kwargs)

    async def post_text(self, url: str, data: Any = None, **kwargs: Any) -> str:
        return await self._raw.post_text(url, data=data, **kwargs)

    async def get_text(self, url: str, **kwargs: Any) -> str:
        return await self._raw.get_text(url, **kwargs)

    async def get_bytes(self, url: str, *, sniff: bool = True, **kwargs: Any) -> bytes:
        kwargs["headers"] = {**APP_IMAGE_HEADERS, **(kwargs.get("headers") or {})}
        meta = self._scramble_info.get(url)
        path = urlparse(url).path

        candidates = [url]
        if meta is not None:
            for host in self._image_domains:
                alt = f"https://{host}{path}"
                if alt != url:
                    candidates.append(alt)

        last_exc: Exception | None = None
        for candidate in candidates:
            try:
                data = await self._raw.get_bytes(candidate, sniff=False, **kwargs)
            except Exception as exc:
                last_exc = exc
                logger.warning("JM image host failed (%s): %s", candidate.split("/")[2], exc)
                continue
            if meta is None:
                return data
            scramble_id, photo_id, filename = meta
            num = strip_bucket_num(scramble_id, photo_id, filename)
            if num <= 0:
                return data
            return unshuffle_strips(data, num)
        raise FetchError(f"all JM image hosts failed for {url}: {last_exc}")

    async def aclose(self) -> None:
        await self._raw.aclose()


class JmComicPlugin(SourcePlugin):
    id = "jmcomic"
    name = "JM Comic / 禁漫天堂 (18+)"
    category = "18+"
    fetch_mode = FetchMode.HTTP

    def __init__(self, cfg: AppConfig, fetcher: BaseFetcher) -> None:
        super().__init__(cfg, fetcher)
        self._raw = fetcher
        self.fetcher = JmComicFetcher(fetcher, self.image_domains)

        self._app_version = str(self.settings.get("app_version") or APP_VERSION)
        self._api_domains: list[str] = list(self.settings.get("api_domains") or DEFAULT_API_DOMAINS)
        self._domains_refreshed = False
        self._scramble_cache: dict[str, int] = {}
        self._version_checked = False
        self._api_lock = asyncio.Lock()

    @property
    def image_domains(self) -> list[str]:
        return list(self.settings.get("image_domains") or DEFAULT_IMAGE_DOMAINS)

    async def _api(self, path: str) -> dict[str, Any]:
        """One signed call against the rotating API host pool; returns the
        decrypted JSON payload. Static hosts first; JM's pushed pool is only
        consulted once the static pool has failed wholesale."""
        async with self._api_lock:
            last_exc: Exception | None = None
            static_pool = list(self._api_domains)

            for pool in (static_pool, None):
                if pool is None:
                    if self.settings.get("refresh_domains", True) is False:
                        continue
                    if not await self._refresh_domains():
                        continue
                    pool = self._api_domains

                for domain in pool:
                    url = f"https://{domain}{path}"
                    ts = str(int(time.time()))
                    try:
                        body = await self._raw.get_json(
                            url,
                            headers=_sign(ts, version=self._app_version),
                            timeout=_API_TIMEOUT_S,
                        )
                    except Exception as exc:
                        last_exc = exc
                        logger.warning("JM api host failed: %s (%s)", url, exc)
                        continue
                    code = body.get("code")
                    if code != 200:
                        msg = body.get("errorMsg") or body.get("msg") or f"code={code}"
                        logger.warning("JM api refused (%s): %s", url, msg)
                        last_exc = FetchError(f"JM api refused {path}: {msg}")
                        continue
                    cipher = body.get("data")
                    if cipher is None or (isinstance(cipher, list) and not cipher):
                        msg = body.get("errorMsg") or "empty"
                        logger.warning("JM api returned no data (%s): %s", url, msg)
                        last_exc = FetchError(f"JM api returned no data for {path}: {msg}")
                        continue
                    plain = _aes_ecb_b64(f"{ts}{APP_DATA_SECRET}", cipher)
                    await self._maybe_check_version()
                    return json.loads(plain.decode("utf-8"))
            raise FetchError(f"all JM api hosts failed for {path}: {last_exc}")

    async def _refresh_domains(self) -> bool:
        """Replace the static API pool with JM's own pushed list. Bounded:
        3 endpoints at 8 s each, then give up and stay on the static pool.
        Returns True when a pushed pool was obtained."""
        if self._domains_refreshed:
            return False
        self._domains_refreshed = True
        client = getattr(self._raw, "_client", None)
        for url in DOMAIN_SERVER_URLS:
            try:
                if client is not None:
                    resp = await client.get(
                        url, timeout=_DOMAIN_SERVER_TIMEOUT_S, follow_redirects=False
                    )
                    resp.raise_for_status()
                    text = resp.text
                else:
                    text = await asyncio.wait_for(
                        self._raw.get_text(url), timeout=_DOMAIN_SERVER_TIMEOUT_S
                    )
                servers = decrypt_server_list(text)
            except Exception as exc:
                logger.warning("JM domain-server %s failed: %s", url, exc)
                continue
            if servers:
                logger.info("JM api domains refreshed: %s", servers)
                self._api_domains = servers
                return True
        return False

    async def _maybe_check_version(self) -> None:
        """/setting announces jm3_version; an outdated client number can 403 on
        the API side. Fire once per run, best-effort, non-blocking."""
        if self._version_checked or not self.settings.get("auto_version", True):
            return
        self._version_checked = True

        async def work() -> None:
            ts = str(int(time.time()))
            url = f"https://{self._api_domains[0]}/setting"
            body = await self._raw.get_json(
                url, headers=_sign(ts, version=self._app_version), timeout=_API_TIMEOUT_S
            )
            cipher = body.get("data")
            if not isinstance(cipher, str) or not cipher:
                return
            data = json.loads(
                _aes_ecb_b64(f"{ts}{APP_DATA_SECRET}", cipher).decode("utf-8", "replace")
            )
            newer = str(data.get("jm3_version") or "").strip()
            if newer and _version_gt(newer, self._app_version):
                logger.info("JM app_version %s -> %s (server announced)", self._app_version, newer)
                self._app_version = newer

        try:
            await asyncio.wait_for(work(), timeout=_API_TIMEOUT_S)
        except Exception as exc:
            logger.debug("JM /setting version check failed: %s", exc)

    async def _scramble_id(self, photo_id: str, album_id: str) -> int:
        """Chapters of one album share the shuffle seed - cached per album."""
        for key in (album_id, photo_id):
            if key in self._scramble_cache:
                return self._scramble_cache[key]

        ts = str(int(time.time()))
        headers = _sign(ts, secret=APP_TOKEN_SECRET_2, version=self._app_version)
        url = (
            f"/chapter_view_template?id={photo_id}&mode=vertical&page=0"
            f"&app_img_shunt=1&express=off&v={ts}"
        )
        html = ""
        last_exc: Exception | None = None
        for domain in list(self._api_domains):
            try:
                html = await self._raw.get_text(
                    f"https://{domain}{url}", headers=headers, timeout=_API_TIMEOUT_S
                )
                break
            except Exception as exc:
                last_exc = exc
                logger.warning("JM scramble host failed: %s (%s)", domain, exc)
        if not html:
            raise FetchError(f"JM scramble lookup failed for photo {photo_id}: {last_exc}")
        match = re.search(r"var\s+scramble_id\s*=\s*(\d+);", html)
        if match is None:
            logger.info(
                "JM scramble id absent for album %s; pinning %d", album_id, SCRAMBLE_FALLBACK
            )
            sid = SCRAMBLE_FALLBACK
        else:
            sid = int(match.group(1))
        self._scramble_cache[album_id] = sid
        self._scramble_cache[photo_id] = sid
        return sid

    async def search(self, query: str) -> list[SearchResult]:
        ref = parse_jm_id(query)
        if ref is not None:
            try:
                album = await self._album(ref)
                return [
                    SearchResult(
                        source_id=self.id,
                        title=self._title_of(album),
                        ref=str(album.get("id") or ref),
                        author=self._authors_of(album),
                        cover_url=self._cover_url(str(album.get("id") or ref)),
                    )
                ]
            except Exception as exc:
                logger.warning("JM direct id %s failed: %s", ref, exc)
                return []

        data = await self._api(f"/search?main_tag=0&search_query={quote(query)}&page=1&o=mv&t=h")
        content = data.get("content") or []
        out: list[SearchResult] = []
        for item in content:
            aid = str(item.get("id") or "").strip()
            title = str(item.get("name") or "").strip()
            if not aid or not title:
                continue
            authors = item.get("author") or []
            category = (item.get("category") or {}).get("title")
            out.append(
                SearchResult(
                    source_id=self.id,
                    title=title,
                    ref=aid,
                    author="、".join(authors)
                    if isinstance(authors, list)
                    else (str(authors) or None),
                    cover_url=(str(item.get("image") or "").strip() or None),
                    extra={"category": category, "description": item.get("description")},
                )
            )
        return out[:60]

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        album = await self._album(result.ref)
        series = album.get("series") or []
        return MangaDetail(
            source_id=self.id,
            title=self._title_of(album),
            ref=result.ref,
            cover_url=self._cover_url(result.ref),
            author=self._authors_of(album),
            description=album.get("description"),
            status=None,
            tags=[str(t) for t in (album.get("tags") or [])],
            chapter_count=len(series) if series else 1,
            extra={"series": series, "series_id": str(album.get("series_id") or "0")},
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        series = detail.extra.get("series") or []
        if not series:
            return [
                Chapter(
                    source_id=self.id,
                    manga_ref=detail.ref,
                    title=detail.title,
                    ref=detail.ref,
                    index=1,
                )
            ]

        parsed: list[tuple[int, str, str]] = []
        for ep in series:
            try:
                sort = int(str(ep.get("sort") or "0"))
            except (TypeError, ValueError):
                sort = 0
            parsed.append((sort, str(ep.get("id") or ""), str(ep.get("name") or "").strip()))
        parsed.sort(key=lambda t: (t[0] == 0, t[0]))

        chapters: list[Chapter] = []
        for position, (sort, ep_id, ep_name) in enumerate(parsed, start=1):
            if not ep_id:
                continue
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=detail.ref,
                    title=ep_name or f"第{position}話",
                    ref=ep_id,
                    index=position,
                    extra={"sort": sort},
                )
            )
        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        photo = await self._api(f"/chapter?id={chapter.ref}")
        images = photo.get("images") or []
        if not images:
            return []
        photo_id = str(photo.get("id") or chapter.ref)
        series_id = str(photo.get("series_id") or "0")

        album_id = photo_id if series_id == "0" else series_id

        scramble_id = await self._scramble_id(photo_id, album_id)
        img_domain = self.image_domains[0]
        pages: list[Page] = []
        for idx, filename in enumerate(images):
            name = str(filename)
            url = f"https://{img_domain}/media/photos/{photo_id}/{name}"
            self.fetcher.remember_scramble(
                url, int(scramble_id), int(photo_id), name.rsplit(".", 1)[0]
            )
            pages.append(Page(index=idx, image_url=url))
        return pages

    async def _album(self, album_id: str) -> dict[str, Any]:
        data = await self._api(f"/album?id={album_id}")
        if not data.get("name"):
            raise FetchError(f"JM album {album_id} missing/not found")
        return data

    def _title_of(self, album: dict[str, Any]) -> str:
        return str(album.get("name") or "").strip() or str(album.get("id") or "untitled")

    def _authors_of(self, album: dict[str, Any]) -> str | None:
        authors = album.get("author") or []
        if isinstance(authors, list) and authors:
            return "、".join(str(a).strip() for a in authors if str(a).strip())
        return None

    def _cover_url(self, album_id: str) -> str:

        return f"https://{self.image_domains[0]}/media/albums/{album_id}.jpg"

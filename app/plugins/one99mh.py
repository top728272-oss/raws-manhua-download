"""199mh (www.199mh.org) source plugin - Kindle epub volume store.

2026-10 pivot: the site no longer streams page images. A series page
(`/comic/<id>`) lists volume rows and each is delivered as an epub (or mobi)
file: `POST /m/vol/download_add` returns a signed URL per volume id. That API
requires a login (`POST /m/user/login`, form-encoded email + md5 password, no
captcha) - set `sources.one99mh.email`/`password` in config.yaml, or paste a
browser session as `cookie`.

Page images live inside the epub. `get_pages` downloads it once per volume,
unzips it in memory and extracts the images in spine order; pages carry
`one99mh-epub://<vol>/<n>` URLs that `_EpubPageFetcher` answers from that
unpacked pool (same wrapper shape as JmComicFetcher), lazily re-fetching a
volume if its cache entry was evicted.
"""

from __future__ import annotations

import asyncio
import hashlib
import html as _html
import io
import json
import logging
import re
import zipfile
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from posixpath import dirname, join, normpath
from typing import Any
from urllib.parse import quote, urlparse
from xml.etree import ElementTree

from app.config import AppConfig
from app.core.fetcher import BaseFetcher, FetchError, looks_like_text, status_code_of
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]+>")
_PAGE_SCHEME = "one99mh-epub://"
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_KEEP_VOLUMES = 2

_CARD = re.compile(
    r'<a[^>]+href=["\'](?:https?://[^/"\']+)?(/comic/[^"/?#]+)["\'][^>]*>(.*?)</a>', re.I | re.S
)
_CARD_TITLE = re.compile(
    r'<div[^>]*class=["\'][^"\']*title[^"\']*["\'][^>]*>(.*?)</div>', re.I | re.S
)
_CARD_AUTHOR = re.compile(
    r'<div[^>]*class=["\'][^"\']*auth[^"\']*["\'][^>]*>(.*?)</div>', re.I | re.S
)
_IMG_SRC = re.compile(r'<img[^>]+(?:data-src|src)=["\']([^"\']+)["\']', re.I)
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
_COVER_IMG = re.compile(r'<img[^>]+src=["\']([^"\']*/upload/cover/[^"\']+)["\']', re.I)
_AUTHOR_ROW = re.compile(r"作者(.*?)題材", re.S)
_TAGS_ROW = re.compile(r"題材(.*?)地區", re.S)
_STATUS_ROW = re.compile(r"進度(.*?)(?:收藏|評分)", re.S)
_VOL_ROW = re.compile(
    r'<input[^>]+value="(\d+)"[^>]*id="vol\d+"[^>]*>.*?'
    r'<span[^>]*class=["\'][^"\']*vol-item-no[^"\']*["\'][^>]*>(.*?)</span>',
    re.I | re.S,
)
_VOL_PAGES = re.compile(r'<span[^>]*class=["\']vol-item-info["\'][^>]*>\((\d+)頁\)')
_META_LINE = re.compile(r"<(h1|div|span|label|p|a)[^>]*>(.*?)</\1>", re.I | re.S)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG.sub("", fragment))).strip()


def _clean_ref(query: str) -> str:
    query = query.strip()
    if query.startswith("http://") or query.startswith("https://") or "/" in query:
        parsed = urlparse(query)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "comic":
            return parts[1]
        if parts:
            return parts[-1]
    return query


def _natural_key(name: str) -> list[Any]:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name.lower())]


def _absolutize(url: str, base_url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return base_url + (url if url.startswith("/") else "/" + url)


def _images_in_spine_order(zf: zipfile.ZipFile) -> list[str]:
    """Zip member names for the page images, in epub spine order.

    Falls back to a natural-name sort of every image member when the OPF does
    not yield an ordered document list.
    """
    container = zf.read("META-INF/container.xml")
    rootfile = ElementTree.fromstring(container).find(".//{*}rootfile")
    opf_path = rootfile.get("full-path") if rootfile is not None else None
    if not opf_path or opf_path not in zf.namelist():
        return []

    opf = ElementTree.fromstring(zf.read(opf_path))
    href_by_id: dict[str, str] = {}
    for item in opf.findall(".//{*}manifest/{*}item"):
        item_id, href, media = item.get("id"), item.get("href"), item.get("media-type", "")
        if item_id and href:
            href_by_id[item_id] = (href, media)

    ordered: list[str] = []
    seen: set[str] = set()
    opf_dir = dirname(opf_path)
    img_ref = re.compile(
        r"<img[^>]+src=[\"']([^\"']+)[\"']|<image[^>]+(?:xlink:)?href=[\"']([^\"']+)[\"']", re.I
    )
    for itemref in opf.findall(".//{*}spine/{*}itemref"):
        entry = href_by_id.get(itemref.get("idref") or "")
        if not entry:
            continue
        href, media = entry
        if "html" not in media and not href.lower().endswith((".xhtml", ".html", ".htm")):
            continue
        doc = normpath(join(opf_dir, href))
        if doc not in zf.namelist():
            continue
        for m in img_ref.finditer(zf.read(doc).decode("utf-8", "replace")):
            ref = _html.unescape(m.group(1) or m.group(2) or "").strip()
            if not ref or ref.startswith(("data:", "http://", "https://")):
                continue
            member = normpath(join(dirname(doc), ref))
            if member in zf.namelist() and member not in seen:
                seen.add(member)
                ordered.append(member)

    if ordered:
        return ordered
    return sorted(
        (n for n in zf.namelist() if n.lower().endswith(_IMAGE_EXTS)),
        key=_natural_key,
    )


def _unpack_epub(data: bytes) -> list[bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        try:
            names = _images_in_spine_order(zf)
        except (KeyError, ElementTree.ParseError):
            names = []
        if not names:
            names = sorted(
                (n for n in zf.namelist() if n.lower().endswith(_IMAGE_EXTS)),
                key=_natural_key,
            )
        return [zf.read(name) for name in names]


class _EpubPageFetcher(BaseFetcher):
    """Answers `one99mh-epub://<vol>/<n>` URLs from volumes unpacked in memory."""

    def __init__(self, raw: BaseFetcher, loader: Callable[[str], Awaitable[list[bytes]]]) -> None:
        self._raw = raw
        self._loader = loader
        self._volumes: OrderedDict[str, list[bytes]] = OrderedDict()
        self._load_lock = asyncio.Lock()

    async def images_for(self, vol_id: str) -> list[bytes]:
        cached = self._volumes.get(vol_id)
        if cached is not None:
            self._volumes.move_to_end(vol_id)
            return cached
        async with self._load_lock:
            cached = self._volumes.get(vol_id)
            if cached is not None:
                return cached
            images = await self._loader(vol_id)
            self._volumes[vol_id] = images
            while len(self._volumes) > _KEEP_VOLUMES:
                self._volumes.popitem(last=False)
            return images

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
        self, url: str, data: Any = None, **kwargs: Any
    ) -> tuple[str, dict[str, str]]:
        return await self._raw.post_text_with_headers(url, data=data, **kwargs)

    async def get_json_failover(self, hosts: list[str], path: str, **kwargs: Any) -> Any:
        return await self._raw.get_json_failover(hosts, path, **kwargs)

    async def aclose(self) -> None:
        await self._raw.aclose()

    async def get_bytes(self, url: str, *, sniff: bool = True, **kwargs: Any) -> bytes:
        if not url.startswith(_PAGE_SCHEME):
            return await self._raw.get_bytes(url, sniff=sniff, **kwargs)
        vol_id, _, pos = url[len(_PAGE_SCHEME) :].partition("/")
        images = await self.images_for(vol_id)
        index = int(pos)
        if index >= len(images):
            raise FetchError(f"one99mh: page {index} beyond volume {vol_id} ({len(images)} images)")
        data = images[index]
        if sniff and data and looks_like_text(data):
            raise FetchError(f"one99mh: epub page {index} of volume {vol_id} is not an image")
        return data


class One99MhPlugin(SourcePlugin):
    id = "one99mh"
    name = "199漫画 (one99mh)"
    category = "General"
    fetch_mode = FetchMode.HTTP

    def __init__(self, cfg: AppConfig, fetcher: BaseFetcher) -> None:
        super().__init__(cfg, fetcher)
        self._raw = fetcher
        self.fetcher = _EpubPageFetcher(fetcher, self._volume_images)
        self._logged_in = False

    @property
    def base_url(self) -> str:
        return self.settings.get("base_url", "https://www.199mh.org").rstrip("/")

    async def _ensure_login(self) -> None:
        if self._logged_in:
            return
        cookie = str(self.settings.get("cookie") or "").strip()
        if cookie:
            for part in cookie.split(";"):
                name, _, value = part.strip().partition("=")
                if name:
                    self._raw._client.cookies.set(name, value)
            self._logged_in = True
            return
        email = str(self.settings.get("email") or "").strip()
        password = str(self.settings.get("password") or "")
        if not (email and password):
            raise FetchError(
                "one99mh: volume downloads need a (free) 199mh.org account - set "
                "sources.one99mh.email/password, or a logged-in browser session "
                "as sources.one99mh.cookie, in config.yaml"
            )
        body = await self._raw.post_text(
            f"{self.base_url}/m/user/login",
            data={"email": email, "password": hashlib.md5(password.encode()).hexdigest()},
        )
        message = _text(body)
        if "登錄" in message and "success" not in body.lower() and "result" not in body:
            raise FetchError(f"one99mh: login rejected: {message[:120]}")
        self._logged_in = True
        logger.info("one99mh: logged in as %s", email)

    async def search(self, query: str) -> list[SearchResult]:

        if (
            query.startswith(("http://", "https://"))
            or "/" in query
            or re.fullmatch(r"[A-Za-z0-9_-]{10,}", query.strip())
        ):
            ref = _clean_ref(query)
            try:
                html = await self.fetcher.get_text(f"{self.base_url}/comic/{ref}")
                detail = self._parse_detail(
                    html, SearchResult(source_id=self.id, title=ref, ref=ref)
                )
                return [
                    SearchResult(
                        source_id=self.id,
                        title=detail.title,
                        ref=ref,
                        cover_url=detail.cover_url,
                        author=detail.author,
                    )
                ]
            except Exception as err:
                logger.warning("one99mh ref search failed for '%s': %s", ref, err)
                return []

        try:
            url = f"{self.base_url}/m/comic/search?q={quote(query)}"
            html = await self.fetcher.get_text(url)
            return self._parse_search(html)
        except Exception as err:
            logger.warning("one99mh search failed: %s", err)
            return []

    def _parse_search(self, html: str) -> list[SearchResult]:
        seen: set[str] = set()
        out: list[SearchResult] = []
        for link_m, card in _CARD.findall(html):
            title_m = _CARD_TITLE.search(card)
            if not title_m:
                continue
            slug = link_m.strip("/").rsplit("/", 1)[-1]
            if not slug or slug in seen:
                continue
            seen.add(slug)
            author_m = _CARD_AUTHOR.search(card)
            img_m = _IMG_SRC.search(card)
            cover = _html.unescape(img_m.group(1)) if img_m else None
            out.append(
                SearchResult(
                    source_id=self.id,
                    title=_text(title_m.group(1)),
                    ref=slug,
                    cover_url=_absolutize(cover, self.base_url) if cover else None,
                    author=_text(author_m.group(1)) or None if author_m else None,
                )
            )
        logger.info("one99mh: search parsed %d results", len(out))
        return out[:60]

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        html = await self.fetcher.get_text(f"{self.base_url}/comic/{ref}")
        return self._parse_detail(html, result)

    def _parse_detail(self, html: str, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        h1_m = _H1.search(html)
        title = _text(h1_m.group(1)) if h1_m else result.title

        cover_m = _COVER_IMG.search(html)
        cover = _html.unescape(cover_m.group(1)) if cover_m else result.cover_url

        author = result.author
        author_m = _AUTHOR_ROW.search(_TAG.sub("|", html))
        if author_m:
            names = [n for n in (t.strip(" |\n\r\t、") for t in author_m.group(1).split("|")) if n]
            if names:
                author = "、".join(names)

        tags: list[str] = []
        tags_m = _TAGS_ROW.search(_TAG.sub("|", html))
        if tags_m:
            tags = [
                t.strip()
                for t in re.split(r"[、,，]", tags_m.group(1).replace("|", ""))
                if t.strip()
            ]

        status = None
        status_m = _STATUS_ROW.search(_TAG.sub(" ", html))
        if status_m:
            status = _text(status_m.group(1)) or None

        return MangaDetail(
            source_id=self.id,
            title=title or ref,
            ref=ref,
            cover_url=_absolutize(cover, self.base_url) if cover else None,
            author=author,
            status=status,
            tags=tags,
            extra={"detail_html": html},
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        ref = _clean_ref(detail.ref)
        html = detail.extra.get("detail_html")
        if not html:
            html = await self.fetcher.get_text(f"{self.base_url}/comic/{ref}")
        return self._parse_chapters(html, ref)

    def _parse_chapters(self, html: str, manga_ref: str) -> list[Chapter]:
        chapters: list[Chapter] = []
        seen: set[str] = set()
        for vol_id, title in _VOL_ROW.findall(html):
            if vol_id in seen:
                continue
            seen.add(vol_id)
            clean_title = _text(title) or f"第{len(chapters) + 1}话"
            vol_anchor = html.find(f'id="vol{vol_id}"')
            pages_m = (
                _VOL_PAGES.search(html[vol_anchor : vol_anchor + 2000]) if vol_anchor >= 0 else None
            )
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=manga_ref,
                    title=clean_title,
                    ref=vol_id,
                    index=len(chapters) + 1,
                    extra={
                        "vol_id": vol_id,
                        "page_count": int(pages_m.group(1)) if pages_m else None,
                    },
                )
            )
        logger.info("one99mh: parsed %d volumes for ref '%s'", len(chapters), manga_ref)
        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        vol_id = str((chapter.extra or {}).get("vol_id") or chapter.ref)
        images = await self.fetcher.images_for(vol_id)
        return [
            Page(index=i, image_url=f"{_PAGE_SCHEME}{vol_id}/{i}", referer=f"{self.base_url}/")
            for i in range(len(images))
        ]

    async def _volume_images(self, vol_id: str) -> list[bytes]:
        data = await self._download_epub(vol_id)
        images = _unpack_epub(data)
        if not images:
            raise FetchError(f"one99mh: volume {vol_id} epub carries no images")
        logger.info("one99mh: volume %s unpacked to %d page images", vol_id, len(images))
        return images

    async def _download_epub(self, vol_id: str) -> bytes:
        try:
            return await self._request_epub(vol_id)
        except Exception as exc:
            status = status_code_of(exc)
            if status and 400 <= status < 500:
                self._logged_in = False
                await self._ensure_login()
                return await self._request_epub(vol_id)
            raise

    async def _request_epub(self, vol_id: str) -> bytes:
        await self._ensure_login()
        body = await self._raw.post_text(
            f"{self.base_url}/m/vol/download_add",
            data={"ids[]": vol_id, "ext": "epub"},
        )
        try:
            result = (json.loads(body).get("result") or [None])[0]
        except ValueError:
            raise FetchError(f"one99mh: unexpected download_add response: {body[:200]}") from None
        if not result or not result.get("url"):
            raise FetchError(f"one99mh: no download URL granted for volume {vol_id}: {body[:200]}")
        url = str(result["url"])
        filename = str(result.get("file_name") or "")
        if filename:
            url += ("&" if "?" in url else "?") + "filename=" + quote(filename)
        return await self._raw.get_bytes(url, headers={"Referer": self.base_url + "/"})

"""Pufeimh (pufeimh.com) source plugin.

The site runs the MANGABZ desktop template on top of a Mccms backend, and it has
two quirks that shape this plugin:

1. **Desktop search is dead.** ``https://www.pufeimh.com/search?key=...`` answers
   200 for every query but always reports ``搜索结果（0）``. The mobile host,
   ``m.pufeimh.com/search?key=...``, renders real results server-side - but only
   when the request carries a mobile User-Agent. So search goes to the mobile
   host with a phone UA while everything else stays on the desktop host.

2. **Chapter images are AES-encrypted.** A reader page ships
   ``var params = '<base64>'`` plus an obfuscated ``pic-v2.js`` that decrypts it
   with CryptoJS. The scheme is:

       raw        = base64decode(params)
       iv         = raw[0:16]
       ciphertext = raw[16:]
       plaintext  = AES-128-CBC(ciphertext, key="9S8$vJnU2ANeSRoF", iv)
       JSON       = PKCS7-unpad(plaintext) -> {source_id, comic_id, images:[...]}

   (CryptoJS truncates the 64-byte IV it derives down to one block, which is why
   the first 16 bytes are the effective IV.)

   The decrypted ``images`` entries point at ``https://s2.325784.xyz/<base64url>``,
   a re-encoding proxy that demands a Referer and serves a visibly smaller file.
   The base64 segment is the *origin* CDN URL, which is reachable directly at full
   quality with no Referer - so we decode and use that, falling back to the proxy
   URL plus a Referer if the payload ever stops being base64.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html as _html
import json
import logging
import re
from urllib.parse import quote, unquote, urljoin, urlparse

from app.core.aes import aes_cbc_decrypt, strip_pkcs7
from app.core.htmlutil import iter_containers, strip_chrome
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)


_PARAMS_KEY = b"9S8$vJnU2ANeSRoF"


_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
)

_TAG = re.compile(r"<[^>]+>")
_PARAMS_BLOB = re.compile(r"params\s*=\s*'([^']+)'")


_SEARCH_BUSY = re.compile(r'"code"\s*:\s*-\s*1|搜索繁忙')
_CHAPTER_HREF = re.compile(r"^/chapter/(\d+)-(\d+)\.html")
_COMIC_HREF = re.compile(r"/comic/([A-Za-z0-9_\-]+)")
_SEARCH_ITEM_HREF = re.compile(r'<a[^>]+href=["\']([^"\']+)["\']', re.I)
_SEARCH_COVER = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
_SEARCH_TITLE = re.compile(
    r'<p[^>]*class=["\'][^"\']*manga-i-list-title[^"\']*["\'][^>]*>(.*?)</p>', re.I | re.S
)
_SEARCH_SUBTITLE = re.compile(
    r'<p[^>]*class=["\'][^"\']*manga-i-list-subtitle[^"\']*["\'][^>]*>(.*?)</p>', re.I | re.S
)
_DETAIL_TITLE = re.compile(
    r'<p[^>]*class=["\'][^"\']*detail-info-title[^"\']*["\'][^>]*>(.*?)</p>', re.I | re.S
)
_DETAIL_COVER = re.compile(r'<img[^>]+class=["\'][^"\']*detail-info-cover[^"\']*["\'][^>]*>', re.I)
_DETAIL_DESC = re.compile(
    r'<p[^>]*class=["\'][^"\']*detail-info-content[^"\']*["\'][^>]*>(.*?)</p>', re.I | re.S
)
_DETAIL_TIP = re.compile(
    r'<p[^>]*class=["\'][^"\']*detail-info-tip[^"\']*["\'][^>]*>(.*?)</p>', re.I | re.S
)

_SRC_PRIORITY = (
    re.compile(r'data-original=["\']([^"\']+)["\']', re.I),
    re.compile(r'data-src=["\']([^"\']+)["\']', re.I),
    re.compile(r'src=["\']([^"\']+)["\']', re.I),
)
_FULL_CHAPTER_LIST = re.compile(
    r'<div[^>]*id=["\']chapterlistload["\'][^>]*>(.*?)</div>', re.I | re.S
)
_CHAPTER_ITEM = re.compile(
    r'<a[^>]+href=["\'](/chapter/[^"\']+)["\'][^>]*class=["\'][^"\']*detail-list-form-item[^"\']*["\']'
    r"[^>]*>(.*?)</a>",
    re.I | re.S,
)
_AUTHOR = re.compile(r"作者[:：]\s*(.*?)(?:</span>|$)", re.S)
_STATUS = re.compile(r"状态[:：]\s*(?:<span>)?([^<]*)", re.S)
_TAGS = re.compile(r"题材[:：].*", re.S)
_TAG_LINK = re.compile(r"<a[^>]*>([^<]+)</a>", re.S)


def _text(fragment: str) -> str:
    """Strip tags/entities and collapse whitespace."""
    return re.sub(r"\s+", " ", _html.unescape(_TAG.sub("", fragment))).strip()


def _image_src(tag: str) -> str | None:
    """Best image URL from an ``<img>`` tag, preferring lazy-load attributes."""
    for pattern in _SRC_PRIORITY:
        match = pattern.search(tag)
        if match:
            return _html.unescape(match.group(1)).strip()
    return None


def _comic_slug(value: str) -> str:
    """Normalize a comic URL, bare slug or ``/comic/<slug>`` path to its slug."""
    value = value.strip()
    if not value:
        return ""
    if "://" in value or value.startswith("/"):
        parsed = urlparse(value)
        match = _COMIC_HREF.search(parsed.path or value)
        if match:
            return match.group(1)
        return (parsed.path or value).strip("/").split("/")[-1]
    return value.split("?")[0].strip("/")


def decode_params_blob(blob: str) -> dict:
    """Decrypt a reader page's ``params`` blob into its JSON payload.

    Pure helper (no I/O) so it can be unit-tested against a captured page.
    Raises ``ValueError`` when the payload is malformed.
    """
    try:
        raw = base64.b64decode(blob)
    except (binascii.Error, ValueError) as err:
        raise ValueError(f"params is not valid base64: {err}") from err

    if len(raw) <= 16:
        raise ValueError("params payload too short to contain an IV and ciphertext")

    plain = strip_pkcs7(aes_cbc_decrypt(raw[16:], _PARAMS_KEY, raw[:16]))
    try:
        payload = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError(f"params did not decrypt to JSON: {err}") from err

    if not isinstance(payload, dict):
        raise ValueError("params JSON is not an object")
    return payload


_DEFAULT_WRAPPER_HOSTS = ("s2.325784.xyz",)


def unwrap_image_url(url: str, wrapper_hosts: tuple[str, ...] | list[str] = ()) -> tuple[str, bool]:
    """Resolve a reader image URL to ``(url, needs_referer)``.

    ``s2.325784.xyz``-style entries carry the origin CDN URL as a base64 path
    segment; unwrapping them yields the full-quality image and needs no Referer.
    A URL that stays on a known wrapper host does need the site Referer. Anything
    else is returned untouched, with no Referer.
    """
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return url, False

    segment = parsed.path.strip("/")
    if segment and "/" not in segment:
        padded = segment + "=" * (-len(segment) % 4)
        try:
            decoded = base64.b64decode(unquote(padded)).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            decoded = ""
        if decoded.startswith(("http://", "https://")):
            return decoded, False

    host = (parsed.hostname or "").lower()
    return url, any(host == h.lower() for h in wrapper_hosts)


class PufeimhPlugin(SourcePlugin):
    id = "pufeimh"
    name = "扑飞漫画 (pufeimh)"
    category = "General"
    fetch_mode = FetchMode.HTTP

    _search_retry_delay = 0.7

    @property
    def base_url(self) -> str:
        return self.settings.get("base_url", "https://www.pufeimh.com").rstrip("/")

    @property
    def mobile_url(self) -> str:
        return self.settings.get("mobile_url", "https://m.pufeimh.com").rstrip("/")

    @property
    def wrapper_hosts(self) -> tuple[str, ...]:
        hosts = self.settings.get("wrapper_hosts")
        if isinstance(hosts, (list, tuple)) and hosts:
            return tuple(str(h) for h in hosts)
        return _DEFAULT_WRAPPER_HOSTS

    async def search(self, query: str) -> list[SearchResult]:
        query = query.strip()
        if not query:
            return []

        if self._looks_like_ref(query):
            result = await self._result_from_ref(query)
            return [result] if result else []

        try:
            page = await self._fetch_search(query)
        except Exception as err:
            logger.warning("pufeimh search failed for '%s': %s", query, err)
            return []

        return self._parse_search(page)

    async def _fetch_search(self, query: str, attempts: int = 3) -> str:
        """Fetch a mobile search page, retrying while the site reports "busy".

        The mobile search endpoint throttles by answering HTTP 200 with a JSON
        body ``{"msg":"搜索繁忙","code":-1}`` roughly half the time. The throttle
        clears on its own, so a couple of spaced retries make search reliable.
        """
        url = f"{self.mobile_url}/search?key={quote(query)}"
        headers = {"User-Agent": _MOBILE_UA}
        delay = self._search_retry_delay
        for attempt in range(1, attempts + 1):
            page = await self.fetcher.get_text(url, headers=headers)
            if not _SEARCH_BUSY.search(page):
                return page
            logger.info("pufeimh: search throttled (attempt %d/%d)", attempt, attempts)
            if attempt < attempts and delay:
                await asyncio.sleep(delay)
                delay *= 2
        logger.warning("pufeimh: search still throttled after %d attempts", attempts)
        return ""

    @staticmethod
    def _looks_like_ref(query: str) -> bool:
        if "://" in query:
            return True
        return query.startswith("/comic/") or query.startswith("/chapter/")

    def _parse_search(self, page: str) -> list[SearchResult]:

        start = page.find('id="catagory-list"')
        if start < 0:
            logger.info("pufeimh: search page had no results container")
            return []
        scope = strip_chrome(page[start:])

        results: list[SearchResult] = []
        seen: set[str] = set()
        for body in iter_containers(scope, "manga-i-list-item"):
            href_m = _SEARCH_ITEM_HREF.search(body)
            if not href_m:
                continue
            slug = _comic_slug(href_m.group(1))
            if not slug or slug in seen:
                continue
            seen.add(slug)

            title_m = _SEARCH_TITLE.search(body)
            cover_m = _SEARCH_COVER.search(body)
            sub_m = _SEARCH_SUBTITLE.search(body)

            cover = _html.unescape(cover_m.group(1)) if cover_m else None
            if cover and not cover.startswith("http"):
                cover = urljoin(self.base_url + "/", cover)

            results.append(
                SearchResult(
                    source_id=self.id,
                    title=_text(title_m.group(1)) if title_m else slug,
                    ref=slug,
                    cover_url=cover,
                    extra={"summary": _text(sub_m.group(1)) if sub_m else None},
                )
            )

        logger.info("pufeimh: search parsed %d results", len(results))
        return results

    async def _result_from_ref(self, ref: str) -> SearchResult | None:
        """Build a single result from a pasted comic or chapter URL."""
        chapter_m = _CHAPTER_HREF.match(urlparse(ref).path or ref)
        try:
            if chapter_m:
                page = await self.fetcher.get_text(self._chapter_url(ref))
                slug_m = _COMIC_HREF.search(page)
                if not slug_m:
                    logger.warning("pufeimh: chapter %s exposed no comic link", ref)
                    return None
                slug = slug_m.group(1)
            else:
                slug = _comic_slug(ref)
                if not slug:
                    return None
        except Exception as err:
            logger.warning("pufeimh ref lookup failed for '%s': %s", ref, err)
            return None

        try:
            detail = await self.get_detail(SearchResult(source_id=self.id, title=slug, ref=slug))
        except Exception as err:
            logger.warning("pufeimh ref detail failed for '%s': %s", slug, err)
            return None
        return SearchResult(
            source_id=self.id,
            title=detail.title,
            ref=detail.ref,
            cover_url=detail.cover_url,
            author=detail.author,
        )

    def _comic_url(self, slug: str) -> str:
        return f"{self.base_url}/comic/{_comic_slug(slug)}"

    def _chapter_url(self, ref: str) -> str:
        if ref.startswith("http"):
            return ref
        return urljoin(self.base_url + "/", ref.lstrip("/"))

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        slug = _comic_slug(result.ref or result.title)
        page = await self.fetcher.get_text(self._comic_url(slug))
        return self._parse_detail(page, result, slug)

    def _parse_detail(self, page: str, result: SearchResult, slug: str) -> MangaDetail:

        page = strip_chrome(page)
        title_m = _DETAIL_TITLE.search(page)
        cover_tag = _DETAIL_COVER.search(page)
        desc_m = _DETAIL_DESC.search(page)
        tip_m = _DETAIL_TIP.search(page)
        tip = tip_m.group(1) if tip_m else ""

        title = _text(title_m.group(1)) if title_m else ""
        if not title:
            head_m = re.search(r"<title>(.*?)</title>", page, re.I | re.S)
            title = _text(head_m.group(1)).split(" - ")[0] if head_m else ""

        cover = _image_src(cover_tag.group(0)) if cover_tag else None
        if cover and not cover.startswith("http"):
            cover = urljoin(self.base_url + "/", cover)
        if not cover:
            cover = result.cover_url

        author_m = _AUTHOR.search(tip)
        status_m = _STATUS.search(tip)
        tags_m = _TAGS.search(tip)
        tags = [_text(t) for t in _TAG_LINK.findall(tags_m.group(0)) if _text(t)] if tags_m else []

        return MangaDetail(
            source_id=self.id,
            title=title or result.title or slug,
            ref=slug,
            cover_url=cover,
            author=_text(author_m.group(1)) if author_m else None,
            description=_text(desc_m.group(1)) if desc_m else None,
            status=_text(status_m.group(1)) if status_m else None,
            tags=tags,
            extra={"detail_html": page},
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        page = detail.extra.get("detail_html")
        if not page:
            page = await self.fetcher.get_text(self._comic_url(detail.ref))
        return self._parse_chapters(page, detail.ref)

    def _parse_chapters(self, page: str, manga_ref: str) -> list[Chapter]:

        block_m = _FULL_CHAPTER_LIST.search(page)
        if not block_m:
            logger.warning("pufeimh: no chapter list found for ref '%s'", manga_ref)
            return []

        chapters: list[Chapter] = []
        seen: set[str] = set()
        for href, label in _CHAPTER_ITEM.findall(block_m.group(1)):
            href = _html.unescape(href).strip()
            if href in seen:
                continue
            seen.add(href)

            ids_m = _CHAPTER_HREF.match(href)
            title = _text(label)
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=manga_ref,
                    title=title or f"第{len(chapters) + 1}话",
                    ref=href,
                    index=len(chapters) + 1,
                    extra={
                        "mid": ids_m.group(1) if ids_m else None,
                        "cid": ids_m.group(2) if ids_m else None,
                    },
                )
            )

        logger.info("pufeimh: parsed %d chapters for ref '%s'", len(chapters), manga_ref)
        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        page = await self.fetcher.get_text(self._chapter_url(chapter.ref))
        return self._parse_pages(page)

    def _parse_pages(self, page: str) -> list[Page]:
        blob_m = _PARAMS_BLOB.search(page)
        if not blob_m:
            logger.warning("pufeimh: reader page had no params blob")
            return []

        try:
            payload = decode_params_blob(blob_m.group(1))
        except ValueError as err:
            logger.error("pufeimh: could not decode reader payload: %s", err)
            return []

        raw_images = payload.get("images") or []
        if not isinstance(raw_images, list):
            logger.error("pufeimh: reader payload images field is not a list")
            return []

        pages: list[Page] = []
        for entry in raw_images:
            if not isinstance(entry, str) or not entry.strip():
                continue
            url, needs_referer = unwrap_image_url(entry, self.wrapper_hosts)
            pages.append(
                Page(
                    index=len(pages),
                    image_url=url,
                    referer=f"{self.base_url}/" if needs_referer else None,
                )
            )

        logger.info("pufeimh: parsed %d pages", len(pages))
        return pages

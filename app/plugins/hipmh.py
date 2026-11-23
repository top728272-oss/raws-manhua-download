"""Hipmh (嬉皮漫畫) source plugin.

Astro SSR front end over a plain JSON API on ``hipapi1.s3file.top``. Nothing
needs authenticating - no Origin, Referer, cookie, signature or token - so the
whole plugin is unauthenticated HTTP.

Endpoint map (all GET, envelope ``{"code":200,"message":"ok","data":{...}}``)::

    /v1/search?q=<q>&page=<n>&page_size=<n>       keyword search
    /v1/manga/chapters?mid=<mid>&page=<n>&per_page=<n>&order=asc
                                                  chapter list (per_page caps at 50)
    /v2/chapter?hid=<api_hid>                     page image list

There is no manga-detail endpoint (``/v1/manga/<id>`` is 404), so metadata comes
from the server-rendered ``/works/<id>`` page's JSON-LD block.

Two shapes of this site's IDs matter more than anything else here:

1. **The chapter endpoint wants a different hid than the chapter list returns.**
   A chapter ``hid`` is ``b64url("m:28024-c:176917") + "-" +
   b64url("28024:124.00")``, but ``/v2/chapter`` only accepts
   ``b64url("c:176917") + "-" + b64url("28024:124.00")``. Handing it the full
   hid answers ``404 {"code":404,"message":"Chapter not found"}``, which reads
   exactly like a genuinely missing chapter. :func:`chapter_api_hid` does the
   rewrite and the plugin stores the result on the Chapter.

2. **The decoded page list carries a decoy.** ``/v2/chapter`` returns one extra
   entry beyond the real page count; the extra one duplicates a real page's
   number with a different content hash, so it cannot be found by sorting. Only
   the site's ``order_id``/``sid`` arithmetic locates it - see
   ``app.core.hipmh_decoder.drop_decoy``. Skipping that step yields a chapter
   with one wrong image in the middle and one page short.

Images live on ``hip-tx-1.s3imgs.top`` (mirror: ``hip-cf-1``), serve WebP, and
need no Referer. The catalogue has no paywalled chapters.

Verified live (2026-10-12) against the JS the site itself ships: 51 chapters
across 10 series decode byte-identical, and a full search -> detail -> chapters
-> pages -> WebP-bytes run completes unauthenticated.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from dataclasses import replace
from typing import Any
from urllib.parse import quote, urlparse

from app.core.hipmh_decoder import decode_images, drop_decoy
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

API_BASE_DEFAULT = "https://hipapi1.s3file.top"
SITE_BASE_DEFAULT = "https://m.hipmh.com"
READER_BASE_DEFAULT = "https://reader.hipmh.top"
IMG_BASE_DEFAULT = "https://hip-tx-1.s3imgs.top"
COVER_BASE_DEFAULT = "https://cover.s3imgs.top"


CHAPTER_PAGE_SIZE = 50


SEARCH_PAGE_SIZE = 20


MAX_CHAPTER_PAGES = 40

_LD_JSON = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S
)
_CHAPTER_COUNT = re.compile(r'chapters=["\'](\d+)["\']', re.I)
_GENRE_LINK = re.compile(r'href=["\']/genre/[^"\']*["\'][^>]*>([^<]+)<', re.I)

_WORKS_PATH = re.compile(r"/works/([A-Za-z0-9_\-]+)")
_READER_CHAPTER_PATH = re.compile(r"/chapter/([A-Za-z0-9_\-]+)")

_STATUS_LABELS = {
    "ongoing": "连载中",
    "completed": "已完结",
}


def _b64url_encode(value: str) -> str:
    """Base64url without padding - the form the site uses inside hids."""
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> str:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return ""


def _manga_id(ref: str) -> str:
    """Extract the manga id (the ``bToy...`` prefix) from any hipmh ref form.

    Accepts a full works id, a ``/works/<id>`` path, a bare id, or - for a
    pasted reader URL - derives nothing and returns "" so the caller can resolve
    the series properly instead.
    """
    ref = (ref or "").strip()
    if not ref:
        return ""
    if "://" in ref or ref.startswith("/"):
        match = _WORKS_PATH.search(urlparse(ref).path or "")
        if not match:
            return ""
        ref = match.group(1)
    return ref.split("?")[0].strip("/")


def manga_mid(manga_id: str) -> str:
    """The ``mid`` the chapter-list endpoint wants: the id's first segment."""
    return (manga_id or "").split("-")[0]


def chapter_api_hid(hid: str) -> str:
    """Rewrite a chapter-list ``hid`` into the form ``/v2/chapter`` accepts.

    ``b64url("m:28024-c:176917") + "-" + b64url("28024:124.00")`` becomes
    ``b64url("c:176917") + "-" + b64url("28024:124.00")``. An already-narrowed
    hid is returned untouched, so this is safe to call repeatedly.
    """
    hid = (hid or "").strip()
    if "-" not in hid:
        return hid
    head, _, tail = hid.partition("-")
    decoded = _b64url_decode(head)
    if not decoded.startswith("m:") or "-" not in decoded:
        return hid
    return _b64url_encode(decoded.split("-", 1)[1]) + "-" + tail


def _authors(item: dict[str, Any]) -> str | None:
    raw = item.get("authors")
    if not isinstance(raw, list):
        return None
    names = [str(a.get("name")).strip() for a in raw if isinstance(a, dict) and a.get("name")]
    return ", ".join(names) or None


def _genres(item: dict[str, Any]) -> list[str]:
    raw = item.get("genres")
    if not isinstance(raw, list):
        return []
    return [str(g.get("name")).strip() for g in raw if isinstance(g, dict) and g.get("name")]


def _abs_cover(path: str | None, cover_base: str) -> str | None:
    if not path:
        return None
    path = path.strip()
    if path.startswith("http"):
        return path
    return cover_base.rstrip("/") + "/" + path.lstrip("/")


def _envelope(raw: Any) -> dict[str, Any]:
    """Unwrap the ``{"code", "message", "data"}`` envelope, raising on errors."""
    if not isinstance(raw, dict):
        raise ValueError(f"hipmh: unexpected non-object API response: {type(raw).__name__}")
    code = raw.get("code")
    data = raw.get("data")
    if code != 200:
        raise ValueError(f"hipmh: API error {code!r}: {raw.get('message')!r}")
    if not isinstance(data, dict):
        raise ValueError("hipmh: API response carried no data object")
    return data


class HipmhPlugin(SourcePlugin):
    id = "hipmh"
    name = "嬉皮漫畫 (hipmh)"
    category = "General"
    fetch_mode = FetchMode.HTTP

    @property
    def api_base(self) -> str:
        return str(self.settings.get("api_base", API_BASE_DEFAULT)).rstrip("/")

    @property
    def base_url(self) -> str:
        return str(self.settings.get("base_url", SITE_BASE_DEFAULT)).rstrip("/")

    @property
    def reader_base(self) -> str:
        return str(self.settings.get("reader_base", READER_BASE_DEFAULT)).rstrip("/")

    @property
    def img_base(self) -> str:
        return str(self.settings.get("img_base", IMG_BASE_DEFAULT)).rstrip("/")

    @property
    def cover_base(self) -> str:
        return str(self.settings.get("cover_base", COVER_BASE_DEFAULT)).rstrip("/")

    @property
    def chapter_pages(self) -> int:
        """How many chapter-list pages to walk. Config-capped for safety."""
        try:
            configured = int(self.settings.get("chapter_pages", MAX_CHAPTER_PAGES))
        except (TypeError, ValueError):
            configured = MAX_CHAPTER_PAGES
        return max(1, min(configured, MAX_CHAPTER_PAGES))

    async def search(self, query: str) -> list[SearchResult]:
        query = (query or "").strip()
        if not query:
            return []

        if self._looks_like_ref(query):
            result = await self._result_from_ref(query)
            return [result] if result else []

        try:
            raw = await self.fetcher.get_json(
                f"{self.api_base}/v1/search?q={quote(query)}&page=1&page_size={SEARCH_PAGE_SIZE}"
            )
            items = _envelope(raw).get("data") or []
        except Exception as err:
            logger.warning("hipmh search failed for '%s': %s", query, err)
            return []

        results = self._parse_search(items)
        logger.info("hipmh: search '%s' parsed %d results", query, len(results))
        return results

    @staticmethod
    def _looks_like_ref(query: str) -> bool:
        """True when the user pasted a site URL instead of typing a keyword."""
        if "://" in query:
            return True
        return query.startswith("/works/") or query.startswith("/chapter/")

    def _parse_search(self, items: Any) -> list[SearchResult]:
        if not isinstance(items, list):
            return []

        results: list[SearchResult] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            manga_id = str(item.get("id") or "").strip()
            if not manga_id or manga_id in seen:
                continue
            seen.add(manga_id)
            results.append(
                SearchResult(
                    source_id=self.id,
                    title=str(item.get("title") or manga_id).strip(),
                    ref=manga_id,
                    cover_url=_abs_cover(item.get("vertical_image_url"), self.cover_base),
                    author=_authors(item),
                    extra={
                        "summary": item.get("description"),
                        "status": item.get("status"),
                        "genres": _genres(item),
                    },
                )
            )
        return results

    async def _result_from_ref(self, ref: str) -> SearchResult | None:
        """Resolve a pasted works/reader URL into a single search result."""
        manga_id = await self._manga_id_from_ref(ref)
        if not manga_id:
            logger.warning("hipmh: could not resolve a manga id from '%s'", ref)
            return None
        try:
            detail = await self.get_detail(
                SearchResult(source_id=self.id, title=manga_id, ref=manga_id)
            )
        except Exception as err:
            logger.warning("hipmh ref lookup failed for '%s': %s", ref, err)
            return None
        return SearchResult(
            source_id=self.id,
            title=detail.title,
            ref=detail.ref,
            cover_url=detail.cover_url,
            author=detail.author,
            extra=dict(detail.extra),
        )

    async def _manga_id_from_ref(self, ref: str) -> str:
        """Manga id for a works URL, a bare id, or a pasted reader chapter URL.

        A reader URL has to bounce through ``/chapter/go?hid=…``, which redirects
        to the canonical chapter page carrying the series slug.
        """
        path = urlparse(ref).path if "://" in ref else ref

        match = _WORKS_PATH.search(path or "")
        if match:
            return match.group(1)
        if not path.startswith("/"):
            return _manga_id(ref)

        match = _READER_CHAPTER_PATH.search(path or "")
        if not match:
            return _manga_id(ref)

        chapter_hid = match.group(1)
        try:
            page = await self.fetcher.get_text(f"{self.reader_base}/chapter/{chapter_hid}")
        except Exception as err:
            logger.warning("hipmh: reader page for hid '%s' failed: %s", chapter_hid, err)
            return ""

        works = re.search(r"m\.hipmh\.com/works/([A-Za-z0-9_\-]+)", page)
        if works:
            return works.group(1)
        return ""

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        manga_id = _manga_id(result.ref or result.title)
        if not manga_id:
            raise ValueError(f"hipmh: '{result.ref!r}' is not a usable manga id")

        page = await self.fetcher.get_text(f"{self.base_url}/works/{manga_id}")
        return self._parse_detail(page, result, manga_id)

    def _parse_detail(self, page: str, result: SearchResult, manga_id: str) -> MangaDetail:
        node: dict[str, Any] = {}
        match = _LD_JSON.search(page)
        if match:
            try:
                graph = json.loads(match.group(1)).get("@graph", [])
                node = next(
                    (
                        n
                        for n in graph
                        if isinstance(n, dict) and n.get("@type") == "CreativeWorkSeries"
                    ),
                    {},
                )
            except (json.JSONDecodeError, AttributeError) as err:
                logger.warning("hipmh: unparsable JSON-LD on /works/%s: %s", manga_id, err)

        extra = dict(result.extra or {})
        author = node.get("author")
        if isinstance(author, dict):
            author = author.get("name")
        author = (str(author).strip() if author else None) or result.author

        description = node.get("description") or extra.get("summary")

        tags = _genres_from_page(page) or extra.get("genres") or []
        status = _STATUS_LABELS.get(str(extra.get("status") or "").lower()) or extra.get("status")

        count_match = _CHAPTER_COUNT.search(page)
        chapter_count = int(count_match.group(1)) if count_match else None

        return MangaDetail(
            source_id=self.id,
            title=str(node.get("name") or result.title or manga_id).strip(),
            ref=manga_id,
            cover_url=_abs_cover(node.get("image"), self.cover_base) or result.cover_url,
            author=author,
            description=str(description).strip() if description else None,
            status=str(status).strip() if status else None,
            tags=tags,
            chapter_count=chapter_count,
            extra={"mid": manga_mid(manga_id)},
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        manga_id = _manga_id(detail.ref)
        mid = (detail.extra or {}).get("mid") or manga_mid(manga_id)
        if not mid:
            logger.warning("hipmh: no manga id to list chapters for ('%s')", detail.ref)
            return []

        items: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        while page <= min(total_pages, self.chapter_pages):
            try:
                data = _envelope(
                    await self.fetcher.get_json(
                        f"{self.api_base}/v1/manga/chapters"
                        f"?mid={quote(mid)}&page={page}&per_page={CHAPTER_PAGE_SIZE}&order=asc"
                    )
                )
            except Exception as err:
                logger.warning("hipmh: chapter page %d failed for '%s': %s", page, manga_id, err)
                break

            batch = data.get("items")
            if isinstance(batch, list):
                items.extend(item for item in batch if isinstance(item, dict))
            try:
                total_pages = max(1, int(data.get("total_pages") or 1))
            except (TypeError, ValueError):
                total_pages = 1
            page += 1

        chapters = self._parse_chapters(items, manga_id)
        logger.info("hipmh: parsed %d chapters for '%s'", len(chapters), manga_id)
        return chapters

    def _parse_chapters(self, items: list[dict[str, Any]], manga_id: str) -> list[Chapter]:
        seen: set[str] = set()
        parsed: list[tuple[int, Chapter]] = []

        for item in items:
            hid = str(item.get("hid") or "").strip()
            if not hid or hid in seen:
                continue
            seen.add(hid)
            try:
                number = int(item.get("chapter_number"))
            except (TypeError, ValueError):
                number = 0
            parsed.append(
                (
                    number,
                    Chapter(
                        source_id=self.id,
                        manga_ref=manga_id,
                        title=str(item.get("title") or f"第{number}话").strip(),
                        ref=hid,
                        index=0,
                        extra={
                            "chapter_number": number,
                            "api_hid": chapter_api_hid(hid),
                        },
                    ),
                )
            )

        parsed.sort(key=lambda pair: pair[0])
        return [
            replace(chapter, index=position)
            for position, (_, chapter) in enumerate(parsed, start=1)
        ]

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        extra = chapter.extra or {}
        api_hid = extra.get("api_hid") or chapter_api_hid(chapter.ref)
        if not api_hid:
            logger.warning("hipmh: chapter '%s' carried no usable hid", chapter.ref)
            return []

        data = _envelope(
            await self.fetcher.get_json(
                f"{self.api_base}/v2/chapter?hid={quote(api_hid)}",
                headers={"Referer": f"{self.reader_base}/"},
            )
        )

        payload = data.get("images")
        if not isinstance(payload, str):
            if isinstance(payload, list):
                plain = [p for p in payload if isinstance(p, str)]
                return self._build_pages(drop_decoy(plain, data.get("order_id"), data.get("sid")))
            logger.warning("hipmh: chapter hid '%s' had no images field", api_hid)
            return []

        try:
            decoded = decode_images(payload)
        except ValueError as err:
            logger.error("hipmh: could not decode chapter '%s': %s", chapter.ref, err)
            return []

        pages = self._build_pages(drop_decoy(decoded, data.get("order_id"), data.get("sid")))
        logger.info("hipmh: resolved %d pages for chapter %s", len(pages), chapter.ref)
        return pages

    def _build_pages(self, paths: list[str]) -> list[Page]:

        base = self.img_base
        return [
            Page(
                index=index,
                image_url=path if path.startswith("http") else f"{base}{path}",
            )
            for index, path in enumerate(paths)
            if isinstance(path, str) and path.strip()
        ]


def _genres_from_page(page: str) -> list[str]:
    """Genre names from the /works sidebar links, de-duplicated in page order."""
    names: list[str] = []
    for name in _GENRE_LINK.findall(page):
        cleaned = name.strip()
        if cleaned and cleaned not in names:
            names.append(cleaned)
    return names

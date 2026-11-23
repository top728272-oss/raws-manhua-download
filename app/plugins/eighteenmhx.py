"""18MHX (18+ Manhua) source plugin.

The site is an Astro app on `m.18mhx.com` whose chapter pages are filled in by a
`fetch()` to `/chapter/getcontent`. Three details are easy to get wrong and each
one was a real bug here:

* ``/chapter/getcontent`` takes ``m`` = the manga id and **``c`` = the chapter's
  ``data-cs`` id** - *not* the chapter href. Passing the href 404s.
* ``data-cs`` is an upload counter, **not** a reading-order key (sorting by it
  scrambles chapters on most books). Reading order is the trailing index of the
  href, ``/manga/{slug}/{mid}-{cid}-{index}``.
* Every manga page opens with an age-gate modal whose ``<h1>警告！</h1>`` comes
  before the real title ``<h1>``, so the document's *first* ``<h1>`` is never
  the title. ``<title>`` is ``"{title}-18漫畫"``.
"""

from __future__ import annotations

import html as _html
import logging
import re
from urllib.parse import quote, urlparse

from app.core.htmlutil import cut_recommendations, is_placeholder_cover, strip_chrome
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

_DATA_MID = re.compile(r'data-mid=["\'](\d+)["\']', re.I)
_ALT = re.compile(r'alt=["\']([^"\']+)["\']', re.I)
_SRC = re.compile(r'src=["\']([^"\']+)["\']', re.I)
_DATA_SRC = re.compile(r'data-src=["\']([^"\']+)["\']', re.I)
_TAG = re.compile(r"<[^>]+>")

_ANCHOR = re.compile(r"<a\b([^>]*)>", re.I | re.S)
_IMG = re.compile(r"<img\b([^>]*)>", re.I | re.S)
_ATTR = re.compile(r'([:\w-]+)\s*=\s*"([^"]*)"')

_HREF_TAIL = re.compile(r"-(\d+)\s*$")
_MANGA_LINK = re.compile(r"/manga/[^\"?/#]+", re.I)
_SITE_SUFFIX = re.compile(r"\s*-\s*18漫畫\s*$")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG.sub("", fragment))).strip()


def _attrs(fragment: str) -> dict[str, str]:
    return {name.lower(): _html.unescape(value) for name, value in _ATTR.findall(fragment)}


def _clean_ref(query: str) -> str:
    query = query.strip()
    if query.startswith("http://") or query.startswith("https://"):
        parsed = urlparse(query)
        path = parsed.path.strip("/")
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in ("manga", "chapterlist", "s"):
            return parts[1]
        return parts[-1]
    return query


class EighteenMHXPlugin(SourcePlugin):
    id = "eighteenmhx"
    name = "18MHX (18+)"
    category = "18+"
    fetch_mode = FetchMode.HTTP

    @property
    def base_url(self) -> str:
        return self.settings.get("base_url", "https://m.18mhx.com").rstrip("/")

    async def search(self, query: str) -> list[SearchResult]:
        ref = _clean_ref(query)
        if query.startswith("http://") or query.startswith("https://"):
            try:
                url = f"{self.base_url}/manga/{ref}"
                html = await self.fetcher.get_text(url)
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
                logger.warning("18mhx direct ref search failed: %s", err)
                return []

        try:
            url = f"{self.base_url}/s/{quote(query)}"
            html = await self.fetcher.get_text(url)
            return self._parse_search(html)
        except Exception as err:
            logger.warning("18mhx search URL failed: %s", err)
            return []

    def _parse_search(self, html: str) -> list[SearchResult]:
        seen: set[str] = set()
        out: list[SearchResult] = []
        html = cut_recommendations(html)
        html = strip_chrome(html)
        pattern = re.compile(
            r'<a[^>]+href=["\'](?:https?://[^/"\']*)?/manga/([^"?/#]+)["\'][^>]*>(.*?)</a>',
            re.I | re.S,
        )
        for match in pattern.finditer(html):
            slug = match.group(1)
            inner = match.group(2)
            if slug in seen:
                continue
            seen.add(slug)
            alt = _ALT.search(inner)
            src = _DATA_SRC.search(inner) or _SRC.search(inner)
            text_clean = _text(inner)
            title = text_clean if text_clean else (_text(alt.group(1)) if alt else "")
            if not title or title == slug:
                continue
            cover = _html.unescape(src.group(1)) if src else None
            if cover and not cover.startswith("http"):
                cover = f"{self.base_url}{cover}"
            if is_placeholder_cover(cover):
                cover = None
            out.append(
                SearchResult(
                    source_id=self.id,
                    title=title,
                    ref=slug,
                    cover_url=cover,
                )
            )
        logger.info("18mhx: search parsed %d results", len(out))
        return out[:20]

    async def _resolve_mid(self, ref: str) -> str:
        clean = _clean_ref(ref)
        if clean.isdigit():
            return clean

        url = f"{self.base_url}/manga/{clean}"
        html = await self.fetcher.get_text(url)
        mids = _DATA_MID.findall(html)
        if mids:
            return mids[0]

        m = re.search(r'data-mid=["\']?(\d+)["\']?', html)
        if m:
            return m.group(1)
        m2 = re.search(r'data-ms=["\']?(\d+)["\']?', html)
        if m2:
            return m2.group(1)

        raise ValueError(f"Could not resolve MID for 18mhx ref '{ref}'")

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        mid = await self._resolve_mid(result.ref)
        url = f"{self.base_url}/manga/{result.ref}"
        html = await self.fetcher.get_text(url)
        return self._parse_detail(html, result, mid)

    def _parse_detail(self, html: str, result: SearchResult, mid: str | None = None) -> MangaDetail:

        title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = _SITE_SUFFIX.sub("", _text(title_m.group(1))) if title_m else ""
        if not title:
            title = result.title

        desc_m = re.search(
            r'<meta[^>]+name="description"[^>]+content=["\']([^"\']+)["\']', html, re.I
        )
        desc = _html.unescape(desc_m.group(1)).strip() if desc_m else None

        og_cover = re.search(
            r'<meta[^>]+property="og:image"[^>]+content=["\']([^"\']+)["\']', html, re.I
        )
        cover = _html.unescape(og_cover.group(1)) if og_cover else result.cover_url

        badge = re.search(r"<h1[^>]*>[^<]*<span[^>]*>(.*?)</span>", html, re.I | re.S)
        status = _text(badge.group(1)) if badge else None

        if mid is None:
            found = _DATA_MID.search(html)
            mid = found.group(1) if found else None

        return MangaDetail(
            source_id=self.id,
            title=title,
            ref=result.ref,
            cover_url=cover,
            description=desc,
            status=status,
            extra={"mid": mid},
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        mid = detail.extra.get("mid")
        if not mid:
            mid = await self._resolve_mid(detail.ref)

        url = f"{self.base_url}/manga/get?mid={mid}&mode=all"
        html = await self.fetcher.get_text(url)
        return self._parse_chapters(html, detail.ref, mid)

    def _parse_chapters(self, html: str, manga_ref: str, mid: str | None = None) -> list[Chapter]:
        chapters: list[Chapter] = []
        seen: set[str] = set()

        for attrs_text in _ANCHOR.findall(html):
            attrs = _attrs(attrs_text)
            href = attrs.get("href", "").strip()
            cid = attrs.get("data-cs")
            if not cid or not _MANGA_LINK.search(href) or href in seen:
                continue
            seen.add(href)
            tail = _HREF_TAIL.search(href)
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=manga_ref,
                    title=_html.unescape(attrs.get("data-ct") or "").strip() or f"Chapter {cid}",
                    ref=href,
                    index=0,
                    extra={
                        "mid": attrs.get("data-ms") or mid or manga_ref,
                        "c": cid,
                        "href": href,
                        "sort": int(tail.group(1)) if tail else None,
                    },
                )
            )

        if chapters and all(ch.extra["sort"] is not None for ch in chapters):
            chapters.sort(key=lambda ch: ch.extra["sort"])
        for position, ch in enumerate(chapters, start=1):
            ch.index = position

        logger.info("18mhx: parsed %d chapters for ref '%s'", len(chapters), manga_ref)
        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        mid = chapter.extra.get("mid") or chapter.manga_ref

        cid = chapter.extra.get("c") or chapter.ref
        url = f"{self.base_url}/chapter/getcontent?m={mid}&c={cid}"
        html = await self.fetcher.get_text(url)
        return self._parse_pages(html)

    def _parse_pages(self, html: str) -> list[Page]:
        pages: list[Page] = []
        seen_urls: set[str] = set()

        for attrs_text in _IMG.findall(html):
            attrs = _attrs(attrs_text)

            raw = attrs.get("data-src") or attrs.get("src")
            if not raw:
                continue
            image_url = _html.unescape(raw).strip()
            if not image_url or image_url.startswith("data:") or image_url in seen_urls:
                continue
            seen_urls.add(image_url)

            pages.append(
                Page(
                    index=len(pages),
                    image_url=image_url,
                    referer=f"{self.base_url}/",
                )
            )

        return pages

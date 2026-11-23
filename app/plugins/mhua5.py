"""Mhua5 (mhua5.com) source plugin.

A stock **Mccms** comic site, which makes it pleasantly boring: every list is
rendered server-side as plain HTML and the reader's image tags carry their real
URLs in ``data-original``. No JavaScript, no encryption, no cookie handshake.

Two things are worth knowing:

1. **Zero-hit search still returns items.** ``/index.php/search?key=<garbage>``
   answers 200 with a dozen ``.common-comic-item`` cards under the same
   ``.cate-comic-list`` container a real search uses - they are recommendations,
   not matches. The only reliable signal is the ``.search_head`` caption: a real
   search renders ``“<query>”搜索结果（17）`` while a miss renders
   ``很遗憾，您搜索的内容暂时没有找到…``. We require the count form and a count > 0
   before parsing any cards.

2. **The chapter list is complete in the HTML** (~700 entries for a long series,
   no pagination), so no extra request is needed to enumerate a whole series.
"""

from __future__ import annotations

import html as _html
import logging
import re
from urllib.parse import quote, urljoin, urlparse

from app.core.htmlutil import iter_containers, strip_chrome
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]+>")
_COMIC_PATH = re.compile(r"/index\.php/comic/([A-Za-z0-9_\-]+)")
_CHAPTER_PATH = re.compile(r"/index\.php/chapter/(\d+)")

_SEARCH_SCOPE = re.compile(
    r'<div[^>]*class=["\'][^"\']*search-comic-list[^"\']*["\'][^>]*>(.*)', re.I | re.S
)
_SEARCH_HEAD = re.compile(
    r'<div[^>]*class=["\'][^"\']*search_head[^"\']*["\'][^>]*>(.*?)</div>', re.I | re.S
)
_SEARCH_COUNT = re.compile(r"搜索结果\s*[（(]\s*(\d+)\s*[)）]")

_SEARCH_ITEM_HREF = re.compile(r'<a[^>]+href=["\']([^"\']+)["\']', re.I)
_SEARCH_ITEM_IMG = re.compile(r"<img[^>]+>", re.I)
_SEARCH_ITEM_TITLE = re.compile(
    r'<p[^>]*class=["\'][^"\']*comic__title[^"\']*["\'][^>]*>(.*?)</p>', re.I | re.S
)

_DETAIL_TITLE = re.compile(
    r'<p[^>]*class=["\'][^"\']*comic-title[^"\']*["\'][^>]*>(.*?)</p>', re.I | re.S
)
_DETAIL_COVER_BOX = re.compile(
    r'<div[^>]*class=["\'][^"\']*de-info__cover[^"\']*["\'][^>]*>(.*?)</div>', re.I | re.S
)
_DETAIL_AUTHOR = re.compile(
    r'<div[^>]*class=["\'][^"\']*comic-author[^"\']*["\'][^>]*>(.*?)</div>', re.I | re.S
)
_DETAIL_CHAPTER_HEAD = re.compile(
    r'<div[^>]*class=["\'][^"\']*de-chapter__title[^"\']*["\'][^>]*>(.*?)</div>', re.I | re.S
)
_DETAIL_STATUS_BOX = re.compile(
    r'<div[^>]*class=["\'][^"\']*comic-status[^"\']*["\'][^>]*>(.*?)</div>', re.I | re.S
)
_DETAIL_INTRO_TOTAL = re.compile(
    r'<p[^>]*class=["\'][^"\']*intro-total[^"\']*["\'][^>]*>(.*?)</p>', re.I | re.S
)
_DETAIL_INTRO = re.compile(r'<p[^>]*class=["\'][^"\']*intro["\'][^>]*>(.*?)</p>', re.I | re.S)


_SRC_PRIORITY = (
    re.compile(r'data-original=["\']([^"\']+)["\']', re.I),
    re.compile(r'data-src=["\']([^"\']+)["\']', re.I),
    re.compile(r'src=["\']([^"\']+)["\']', re.I),
)

_CHAPTER_LIST = re.compile(
    r'<ul[^>]*class=["\'][^"\']*chapter__list-box[^"\']*["\'][^>]*>(.*?)</ul>', re.I | re.S
)
_CHAPTER_LINK = re.compile(
    r'<a[^>]+class=["\'][^"\']*j-chapter-link[^"\']*["\'][^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.I | re.S,
)
_CHAPTER_LINK_ALT = re.compile(
    r'<a[^>]+href=["\']([^"\']*?/index\.php/chapter/\d+)["\'][^>]*>(.*?)</a>', re.I | re.S
)

_READER_IMG = re.compile(r'<img[^>]+class=["\'][^"\']*lazy-read[^"\']*["\'][^>]*>', re.I)
_PLACEHOLDER = re.compile(r"lazyload_img|bg_loadimg|placeholder|empty\.png", re.I)


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
    """Normalize a comic URL, ``/index.php/comic/<slug>`` path or slug to its slug."""
    value = value.strip()
    if not value:
        return ""
    if "://" in value or value.startswith("/"):
        parsed = urlparse(value)
        match = _COMIC_PATH.search(parsed.path or value)
        if match:
            return match.group(1)
        return (parsed.path or value).strip("/").split("/")[-1]
    return value.split("?")[0].strip("/")


def _search_count(head_html: str) -> int:
    """Number of real hits reported by the search page caption (0 when it is a miss).

    Pure helper so the zero-hit rule can be unit-tested without network access.
    """
    match = _SEARCH_COUNT.search(_text(head_html))
    return int(match.group(1)) if match else 0


class Mhua5Plugin(SourcePlugin):
    id = "mhua5"
    name = "漫画屋 (mhua5)"
    category = "General"
    fetch_mode = FetchMode.HTTP

    @property
    def base_url(self) -> str:
        return self.settings.get("base_url", "https://www.mhua5.com").rstrip("/")

    async def search(self, query: str) -> list[SearchResult]:
        query = query.strip()
        if not query:
            return []

        if "://" in query or query.startswith("/index.php/"):
            result = await self._result_from_ref(query)
            return [result] if result else []

        try:
            url = f"{self.base_url}/index.php/search?key={quote(query)}"
            page = await self.fetcher.get_text(url)
        except Exception as err:
            logger.warning("mhua5 search failed for '%s': %s", query, err)
            return []

        return self._parse_search(page)

    def _parse_search(self, page: str) -> list[SearchResult]:
        head_m = _SEARCH_HEAD.search(page)
        if not head_m or _search_count(head_m.group(1)) == 0:
            logger.info("mhua5: search reported no matches")
            return []

        scope_m = _SEARCH_SCOPE.search(page)
        if not scope_m:
            logger.info("mhua5: search page had no results container")
            return []

        scope = strip_chrome(scope_m.group(1))
        results: list[SearchResult] = []
        seen: set[str] = set()

        for body in iter_containers(scope, "common-comic-item"):
            href_m = _SEARCH_ITEM_HREF.search(body)
            if not href_m:
                continue
            slug = _comic_slug(href_m.group(1))
            if not slug or slug in seen:
                continue
            seen.add(slug)

            img_m = _SEARCH_ITEM_IMG.search(body)
            cover = _image_src(img_m.group(0)) if img_m else None
            if cover and not cover.startswith("http"):
                cover = urljoin(self.base_url + "/", cover)
            if cover and _PLACEHOLDER.search(cover):
                cover = None

            title_m = _SEARCH_ITEM_TITLE.search(body)
            results.append(
                SearchResult(
                    source_id=self.id,
                    title=_text(title_m.group(1)) if title_m else slug,
                    ref=slug,
                    cover_url=cover,
                )
            )

        logger.info("mhua5: search parsed %d results", len(results))
        return results

    async def _result_from_ref(self, ref: str) -> SearchResult | None:
        """Build a single result from a pasted comic or chapter URL."""
        path = urlparse(ref).path or ref
        chapter_m = _CHAPTER_PATH.search(path)
        try:
            if chapter_m:
                page = await self.fetcher.get_text(self._chapter_url(chapter_m.group(1)))
                slug_m = _COMIC_PATH.search(page)
                if not slug_m:
                    logger.warning("mhua5: chapter %s exposed no comic link", ref)
                    return None
                slug = slug_m.group(1)
            else:
                slug = _comic_slug(ref)
                if not slug:
                    return None
        except Exception as err:
            logger.warning("mhua5 ref lookup failed for '%s': %s", ref, err)
            return None

        try:
            detail = await self.get_detail(SearchResult(source_id=self.id, title=slug, ref=slug))
        except Exception as err:
            logger.warning("mhua5 ref detail failed for '%s': %s", slug, err)
            return None
        return SearchResult(
            source_id=self.id,
            title=detail.title,
            ref=detail.ref,
            cover_url=detail.cover_url,
            author=detail.author,
        )

    def _comic_url(self, slug: str) -> str:
        return f"{self.base_url}/index.php/comic/{_comic_slug(slug)}"

    def _chapter_url(self, ref: str) -> str:
        if ref.startswith("http"):
            return ref
        if ref.startswith("/"):
            return urljoin(self.base_url + "/", ref.lstrip("/"))
        return f"{self.base_url}/index.php/chapter/{ref}"

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        slug = _comic_slug(result.ref or result.title)
        page = await self.fetcher.get_text(self._comic_url(slug))
        return self._parse_detail(page, result, slug)

    def _parse_detail(self, page: str, result: SearchResult, slug: str) -> MangaDetail:

        page = strip_chrome(page)
        title_m = _DETAIL_TITLE.search(page)
        cover_box = _DETAIL_COVER_BOX.search(page)
        author_box = _DETAIL_AUTHOR.search(page)
        status_box = _DETAIL_CHAPTER_HEAD.search(page)
        tags_box = _DETAIL_STATUS_BOX.search(page)
        intro_total = _DETAIL_INTRO_TOTAL.search(page)
        intro = _DETAIL_INTRO.search(page)

        title = _text(title_m.group(1)) if title_m else ""
        if not title:
            head_m = re.search(r"<title>(.*?)</title>", page, re.I | re.S)
            title = _text(head_m.group(1)).split(" - ")[0] if head_m else ""

        cover = _image_src(cover_box.group(1)) if cover_box else None
        if cover and not cover.startswith("http"):
            cover = urljoin(self.base_url + "/", cover)
        if not cover:
            cover = result.cover_url

        author = None
        if author_box:
            name_m = re.search(
                r'<span[^>]*class=["\'][^"\']*name[^"\']*["\'][^>]*>(.*?)</span>',
                author_box.group(1),
                re.I | re.S,
            )
            if name_m:
                author = _text(name_m.group(1)) or None

        status = None
        if status_box:
            first_m = re.search(r"<span[^>]*>(.*?)</span>", status_box.group(1), re.I | re.S)
            if first_m:
                status = _text(first_m.group(1)) or None

        tags: list[str] = []
        if tags_box:
            tag_m = re.search(r"题材[:：](.*)", tags_box.group(1), re.S)
            if tag_m:
                tags = [
                    _text(t)
                    for t in re.findall(r"<a[^>]*>(.*?)</a>", tag_m.group(1), re.S)
                    if _text(t)
                ]

        description = None
        if intro_total:
            description = _text(intro_total.group(1)) or None
        if not description and intro:
            description = _text(intro.group(1)) or None

        return MangaDetail(
            source_id=self.id,
            title=title or result.title or slug,
            ref=slug,
            cover_url=cover,
            author=author,
            description=description,
            status=status,
            tags=tags,
            extra={"detail_html": page},
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        page = detail.extra.get("detail_html")
        if not page:
            page = await self.fetcher.get_text(self._comic_url(detail.ref))
        return self._parse_chapters(page, detail.ref)

    def _parse_chapters(self, page: str, manga_ref: str) -> list[Chapter]:
        block_m = _CHAPTER_LIST.search(page)
        if not block_m:
            logger.warning("mhua5: no chapter list found for ref '%s'", manga_ref)
            return []

        block = block_m.group(1)
        items = _CHAPTER_LINK.findall(block) or _CHAPTER_LINK_ALT.findall(block)

        chapters: list[Chapter] = []
        seen: set[str] = set()
        for href, label in items:
            href = _html.unescape(href).strip()
            if not href or href in seen:
                continue
            seen.add(href)

            ids_m = _CHAPTER_PATH.search(href)
            title = _text(label)
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=manga_ref,
                    title=title or f"第{len(chapters) + 1}话",
                    ref=href,
                    index=len(chapters) + 1,
                    extra={"cid": ids_m.group(1) if ids_m else None},
                )
            )

        logger.info("mhua5: parsed %d chapters for ref '%s'", len(chapters), manga_ref)
        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        page = await self.fetcher.get_text(self._chapter_url(chapter.ref))
        return self._parse_pages(page)

    def _parse_pages(self, page: str) -> list[Page]:
        pages: list[Page] = []
        for tag in _READER_IMG.findall(page):
            url = _image_src(tag) or ""
            if not url or _PLACEHOLDER.search(url):
                continue
            if not url.startswith("http"):
                url = urljoin(self.base_url + "/", url)
            pages.append(Page(index=len(pages), image_url=url, referer=None))

        logger.info("mhua5: parsed %d pages", len(pages))
        return pages

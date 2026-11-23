"""漫小肆韓漫 (jjmhw9.top) source plugin.

A plain server-rendered Korean-webtoon mirror - no browser, no image
scrambling, no signed URLs. Captured HARs (`scratch/www.jjmhw9.top *.har`)
show the whole flow is static HTML:

    /search?keyword=…   ->  <ul class="mh-list"> of <a href="/book/{id}">
    /book/{id}          ->  metadata + <ul id="detail-list-select"> chapters
    /chapter/{id}       ->  <img class="lazy" data-original="…jpg"> in order

Two details worth keeping in mind:

* The search engine is **fuzzy** - a garbage query still reports "found N
  results" of completely unrelated comics (verified live: `zzqqwwxx1122`
  returned 9 hits, none containing a single query character). Hits are
  therefore filtered by query/title character coverage, exactly like ComicBox.
* Chapter links render oldest-first already, but chapter **ids are
  auto-increment**, so sorting by id normalises the list to reading order no
  matter which end the site decides to display. Never sort by the number in
  the title - many books use named chapters ("最終話", "後記", "清純主婦與噪音(上)")
  with no numeric component at all.
"""

from __future__ import annotations

import html as _html
import logging
import re
from urllib.parse import quote, urljoin, urlparse

from app.core.htmlutil import strip_chrome
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]+>")
_BOOK_ANCHOR = re.compile(
    r'<a[^>]+href=["\'](?:https?://[^/"\']*)?/book/(\d+)["\'][^>]*title=["\']([^"\']*)["\'][^>]*>'
    r"(.*?)</a>",
    re.I | re.S,
)
_COVER_STYLE = re.compile(r"background-image\s*:\s*url\(([^)]+)\)", re.I)
_COVER_IMG = re.compile(
    r'<div[^>]*class=["\']cover["\'][^>]*>\s*<img[^>]+src=["\']([^"\']+)["\']', re.I | re.S
)
_CHAPTER_LIST = re.compile(r'<ul[^>]*id=["\']detail-list-select["\'][^>]*>(.*?)</ul>', re.I | re.S)
_CHAPTER_ANCHOR = re.compile(
    r'<a[^>]+href=["\'](?:https?://[^/"\']*)?/chapter/(\d+)["\'][^>]*>(.*?)</a>', re.I | re.S
)
_READER_IMG = re.compile(
    r'<img[^>]+class=["\'][^"\']*\blazy\b[^"\']*["\'][^>]*data-original=["\']([^"\']+)["\']', re.I
)

_TITLE_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
_AUTHOR = re.compile(r'<p[^>]*class=["\']subtitle["\'][^>]*>\s*作者[：:]\s*(.*?)</p>', re.I | re.S)
_DESCRIPTION = re.compile(r'<p[^>]*class=["\']content["\'][^>]*>(.*?)</p>', re.I | re.S)
_STATUS = re.compile(r"状态[：:]\s*<span[^>]*>(.*?)</span>", re.I | re.S)
_TAG_LINK = re.compile(
    r'<a[^>]+href=["\'][^"\']*/booklist/\?tag=[^"\']*["\'][^>]*>(.*?)</a>', re.I | re.S
)


_RELEVANT_CHARS = re.compile(r"[0-9A-Za-z\u3400-\u9fff]")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG.sub("", fragment))).strip()


def _query_relevant(query: str, title: str, min_coverage: float = 0.75) -> bool:
    """True if a search hit plausibly relates to the query.

    jjmhw9's search is fuzzy in the same way ComicBox's is: it answers any
    string with a page of unrelated comics, so hits must be filtered by how much
    of the query actually appears in the title. Comparing normalized character
    SETS at 75% coverage keeps exact hits and traditional/simplified variants
    (秘密教学 vs 秘密教學 = 3/4) while dropping fuzzy junk. Queries with fewer
    than two distinct characters are accepted as-is.
    """
    q = set(_RELEVANT_CHARS.findall(query))
    if len(q) < 2:
        return True
    return len(q & set(_RELEVANT_CHARS.findall(title))) / len(q) >= min_coverage


def _clean_ref(query: str) -> str:
    """Reduce a URL / ``/book/{id}`` / bare id to the numeric book ref."""
    query = query.strip()
    if query.startswith(("http://", "https://", "/")):
        parts = [part for part in urlparse(query).path.split("/") if part]
        if "book" in parts:
            pos = parts.index("book")
            if len(parts) > pos + 1:
                return parts[pos + 1]
        return parts[-1] if parts else query
    return query


class Jjmhw9Plugin(SourcePlugin):
    id = "jjmhw9"
    name = "漫小肆韓漫 (18+)"
    category = "18+"
    fetch_mode = FetchMode.HTTP

    @property
    def base_url(self) -> str:
        return self.settings.get("base_url", "https://www.jjmhw9.top").rstrip("/")

    async def search(self, query: str) -> list[SearchResult]:
        query = query.strip()
        ref = _clean_ref(query)
        if query.startswith(("http://", "https://", "/book/")) or (ref.isdigit() and len(ref) >= 3):
            try:
                html = await self.fetcher.get_text(f"{self.base_url}/book/{ref}")
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
                logger.warning("jjmhw9 direct ref search failed for %s: %s", ref, err)
                return []

        try:
            html = await self.fetcher.get_text(f"{self.base_url}/search?keyword={quote(query)}")
            return self._parse_search(html, query)
        except Exception as err:
            logger.warning("jjmhw9 search failed for '%s': %s", query, err)
            return []

    def _parse_search(self, html: str, query: str = "") -> list[SearchResult]:
        html = strip_chrome(html)
        seen: set[str] = set()
        out: list[SearchResult] = []

        for match in _BOOK_ANCHOR.finditer(html):
            ref, title_attr, inner = match.group(1), match.group(2), match.group(3)
            if ref in seen:
                continue
            title = _html.unescape(title_attr).strip() or _text(inner)
            if query and not _query_relevant(query, title):
                continue
            seen.add(ref)

            cover_m = _COVER_STYLE.search(inner)
            cover = _html.unescape(cover_m.group(1)).strip() if cover_m else None
            if cover:
                cover = urljoin(self.base_url + "/", cover)

            out.append(
                SearchResult(source_id=self.id, title=title or ref, ref=ref, cover_url=cover)
            )

        return out[:30]

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        html = await self.fetcher.get_text(f"{self.base_url}/book/{ref}")
        return self._parse_detail(html, result)

    def _parse_detail(self, html: str, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        title_m = _TITLE_H1.search(html)
        title = _text(title_m.group(1)) if title_m else result.title

        cover_m = _COVER_IMG.search(html)
        cover = _html.unescape(cover_m.group(1)).strip() if cover_m else result.cover_url
        if cover:
            cover = urljoin(self.base_url + "/", cover)

        author_m = _AUTHOR.search(html)
        author = _text(author_m.group(1)) if author_m else None

        desc_m = _DESCRIPTION.search(html)
        description = _text(desc_m.group(1)) if desc_m else None

        status_m = _STATUS.search(html)
        status = _text(status_m.group(1)) if status_m else None

        tags = [_text(tag) for tag in _TAG_LINK.findall(html)]
        chapters = self._parse_chapters(html, ref)

        return MangaDetail(
            source_id=self.id,
            title=title or result.title,
            ref=ref,
            cover_url=cover,
            author=author,
            description=description,
            status=status,
            tags=[tag for tag in tags if tag],
            chapter_count=len(chapters) or None,
            extra={"detail_html": html},
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        ref = _clean_ref(detail.ref)
        html = detail.extra.get("detail_html")
        if not html:
            html = await self.fetcher.get_text(f"{self.base_url}/book/{ref}")
        return self._parse_chapters(html, ref)

    def _parse_chapters(self, html: str, manga_ref: str) -> list[Chapter]:
        container = _CHAPTER_LIST.search(html)

        body = container.group(1) if container else html

        raw: list[tuple[int, str, str]] = []
        seen: set[str] = set()
        for match in _CHAPTER_ANCHOR.finditer(body):
            ch_id, inner = match.group(1), match.group(2)
            if ch_id in seen:
                continue
            seen.add(ch_id)
            raw.append((int(ch_id), ch_id, _text(inner)))

        raw.sort(key=lambda item: item[0])

        chapters = [
            Chapter(
                source_id=self.id,
                manga_ref=manga_ref,
                title=title or f"Chapter {ch_id}",
                ref=ch_id,
                index=index,
                extra={"sort": sort_key},
            )
            for index, (sort_key, ch_id, title) in enumerate(raw, start=1)
        ]
        logger.info("jjmhw9: parsed %d chapters for ref '%s'", len(chapters), manga_ref)
        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        html = await self.fetcher.get_text(f"{self.base_url}/chapter/{chapter.ref}")
        return self._parse_pages(html)

    def _parse_pages(self, html: str) -> list[Page]:
        pages: list[Page] = []
        seen: set[str] = set()
        for raw_url in _READER_IMG.findall(html):
            image_url = urljoin(self.base_url + "/", _html.unescape(raw_url).strip())
            if not image_url or image_url in seen:
                continue
            seen.add(image_url)

            pages.append(Page(index=len(pages), image_url=image_url, referer=f"{self.base_url}/"))
        return pages

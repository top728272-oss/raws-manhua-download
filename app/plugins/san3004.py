"""三四娱乐 (3004ss.com) source plugin.

A plain server-rendered WordPress site running the `xiu` theme plus a small
`comic-image-extractor` plugin. No browser, no signature, no image scrambling;
the HAR (`scratch/www.3004ss.com.har`) and live probing agree on the whole flow:

    /wp-admin/admin-ajax.php?action=comic_fuzzy_search&q=…
                        ->  [{"id":…,"title":…,"url":…,"subtitle":…}]
    /post/{id}.html     ->  metadata + every chapter as <a class="chapter-link">
    /post/{id}/page-{n}.html  ->  chapter n-1, <img data-src="…" class="lazy">

Four details worth keeping in mind:

* **Chapter N is `page-(N+1).html`.** `post/{id}.html` is page 1 of the post, so
  the chapter list starts at `page-2.html`. Chapter order comes from that page
  number, never from the title: completed series name their last chapter `完结`
  and the 排序 toggle is client-side only.
* **`data-src` precedes `class` in this theme's markup**, so the
  `class="lazy"…data-src=` shape used by jjmhw9 matches nothing here. The whole
  tag is matched instead and both markers are required inside it.
* **Two Referer gates.** The ajax search endpoint answers
  `{"success":false,"data":"非法来源"}` unless the Referer is under a site domain,
  and the image CDN 404s (HTML body, not a 403) without one. Both accept any
  page path, so the site root is enough.
* The site advertises rotating backup domains (`m.3004aa.cc` works today,
  `www.ggfree.cc` is advertised but dead), so `base_url` stays in config and
  `backup_urls` is tried before giving up on a page fetch.
"""

from __future__ import annotations

import html as _html
import logging
import re
from urllib.parse import urljoin, urlparse

from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]+>")

_TITLE = re.compile(r'<h1[^>]*class=["\']article-title["\'][^>]*>(.*?)</h1>', re.I | re.S)
_SUBTITLE = re.compile(r'<span[^>]*class=["\']subtitle["\'][^>]*>(.*?)</span>', re.I | re.S)
_COVER = re.compile(
    r'<div[^>]*class=["\']c-img["\'][^>]*>\s*<img[^>]+src=["\']([^"\']+)["\']', re.I
)
_DESCRIPTION = re.compile(r'<span[^>]*class=["\']dis["\'][^>]*>(.*?)</span>', re.I | re.S)
_META = re.compile(r'<ul[^>]*class=["\']article-meta["\'][^>]*>(.*?)</ul>', re.I | re.S)
_CATEGORY = re.compile(r'<a[^>]+rel=["\']category tag["\'][^>]*>(.*?)</a>', re.I | re.S)
_MODIFIED = re.compile(r"最后修改[：:]\s*([0-9][0-9\-: ]*)")

_CHAPTER_LINK = re.compile(
    r'<a[^>]+href=["\']([^"\']*/page-(\d+)\.html)["\'][^>]*>(.*?)</a>', re.I | re.S
)


_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
_DATA_SRC = re.compile(r'data-src=["\']([^"\']+)["\']', re.I)

_COVER_BASE = "https://imgs.34img.cc/manga-cover"
_STATUS_WORDS = ("完结", "連載", "连载", "已完结", "暂停")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG.sub("", fragment))).strip()


def _clean_ref(query: str) -> str:
    """Reduce a URL / ``/post/{id}.html`` / bare id to the numeric post ref."""
    query = query.strip()
    if query.startswith(("http://", "https://", "/")):
        parts = [part for part in urlparse(query).path.split("/") if part]
        if "post" in parts:
            pos = parts.index("post")
            if len(parts) > pos + 1:
                return parts[pos + 1].removesuffix(".html")
        return parts[-1].removesuffix(".html") if parts else query
    return query


class San3004Plugin(SourcePlugin):
    id = "san3004"
    name = "三四娱乐 (3004ss)"
    category = "18+"
    fetch_mode = FetchMode.HTTP

    @property
    def base_url(self) -> str:
        return self.settings.get("base_url", "https://www.3004ss.com").rstrip("/")

    @property
    def domains(self) -> list[str]:
        candidates = [self.base_url]
        for url in self.settings.get("backup_urls", []):
            clean = (url or "").rstrip("/")
            if clean and clean not in candidates:
                candidates.append(clean)
        return candidates

    def _referer(self, host: str | None = None) -> dict[str, str]:

        return {"Referer": (host or self.base_url) + "/"}

    async def search(self, query: str) -> list[SearchResult]:
        query = query.strip()
        ref = _clean_ref(query)
        if query.startswith(("http://", "https://", "/post/")) or (ref.isdigit() and len(ref) >= 3):
            try:
                detail = await self.get_detail(SearchResult(self.id, ref, ref))
                return [
                    SearchResult(
                        source_id=self.id,
                        title=detail.title,
                        ref=ref,
                        cover_url=detail.cover_url,
                    )
                ]
            except Exception as err:
                logger.warning("san3004 direct ref search failed for %s: %s", ref, err)
                return []

        last_exc: Exception | None = None
        for host in self.domains:
            try:
                data = await self.fetcher.get_json(
                    f"{host}/wp-admin/admin-ajax.php",
                    params={"action": "comic_fuzzy_search", "q": query},
                    headers=self._referer(host),
                )
                return self._parse_search(data)
            except Exception as err:
                last_exc = err
                logger.warning("san3004 search host failed (%s): %s", host, err)
        logger.warning("san3004 search failed for '%s': %s", query, last_exc)
        return []

    def _parse_search(self, data: object) -> list[SearchResult]:

        if isinstance(data, dict):
            logger.warning("san3004 search refused: %s", data.get("data") or data)
            return []
        if not isinstance(data, list):
            return []
        out: list[SearchResult] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("id") or "").strip()
            if not ref:
                continue
            subtitle = (item.get("subtitle") or "").strip()
            out.append(
                SearchResult(
                    source_id=self.id,
                    title=(item.get("title") or ref).strip(),
                    ref=ref,
                    cover_url=f"{_COVER_BASE}/{ref}/cover.jpg",
                    extra={"subtitle": subtitle, "url": item.get("url")},
                )
            )
        return out

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        html = await self._fetch_text(f"/post/{ref}.html")
        return self._parse_detail(html, result)

    async def _fetch_text(self, path: str) -> str:
        last_exc: Exception | None = None
        for host in self.domains:
            try:
                return await self.fetcher.get_text(f"{host}{path}", headers=self._referer(host))
            except Exception as err:
                last_exc = err
                logger.warning("san3004 host failed (%s%s): %s", host, path, err)
        raise last_exc or RuntimeError(f"no host served {path}")

    def _parse_detail(self, html: str, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        title = result.title
        latest: str | None = None
        status: str | None = None
        title_m = _TITLE.search(html)
        if title_m:
            title = _text(_SUBTITLE.sub("", title_m.group(1))) or result.title
            sub_m = _SUBTITLE.search(title_m.group(1))
            if sub_m:
                subtitle = _text(sub_m.group(1))

                if subtitle in _STATUS_WORDS:
                    status = subtitle
                else:
                    latest = subtitle

        cover_m = _COVER.search(html)
        cover = None
        if cover_m:
            cover = urljoin(self.base_url + "/", _html.unescape(cover_m.group(1)).strip())
        if not cover:
            cover = result.cover_url or f"{_COVER_BASE}/{ref}/cover.jpg"

        desc_m = _DESCRIPTION.search(html)
        description = _text(desc_m.group(1)) if desc_m else None

        tags: list[str] = []
        modified: str | None = None
        meta_m = _META.search(html)
        if meta_m:
            tags = [_text(tag) for tag in _CATEGORY.findall(meta_m.group(1))]
            mod_m = _MODIFIED.search(_text(meta_m.group(1)))
            modified = mod_m.group(1).strip() if mod_m else None

        chapters = self._parse_chapters(html, ref)
        return MangaDetail(
            source_id=self.id,
            title=title,
            ref=ref,
            cover_url=cover,
            description=description,
            status=status,
            tags=[tag for tag in tags if tag],
            chapter_count=len(chapters) or None,
            extra={
                "detail_html": html,
                "latest_chapter": latest,
                "modified": modified,
            },
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        ref = _clean_ref(detail.ref)
        html = detail.extra.get("detail_html")
        if not html:
            html = await self._fetch_text(f"/post/{ref}.html")
        return self._parse_chapters(html, ref)

    def _parse_chapters(self, html: str, manga_ref: str) -> list[Chapter]:

        raw: dict[int, tuple[str, str]] = {}
        for href, page_no, inner in _CHAPTER_LINK.findall(html):
            number = int(page_no)
            if number in raw:
                continue
            raw[number] = (urljoin(self.base_url + "/", href), _text(inner))

        chapters: list[Chapter] = []
        for number in sorted(raw):
            href, title = raw[number]
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=manga_ref,
                    title=title or f"第{number - 1}话",
                    ref=href,
                    index=len(chapters) + 1,
                    extra={"page": number},
                )
            )
        logger.info("san3004: parsed %d chapters for ref '%s'", len(chapters), manga_ref)
        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        html = await self._fetch_text(_path_of(chapter.ref))
        return self._parse_pages(html)

    def _parse_pages(self, html: str) -> list[Page]:
        pages: list[Page] = []
        seen: set[str] = set()
        for tag in _IMG_TAG.findall(html):
            if "lazy" not in tag:
                continue
            src_m = _DATA_SRC.search(tag)
            if not src_m:
                continue
            image_url = urljoin(self.base_url + "/", _html.unescape(src_m.group(1)).strip())
            if not image_url or image_url in seen:
                continue
            seen.add(image_url)

            pages.append(Page(index=len(pages), image_url=image_url, referer=self.base_url + "/"))
        return pages


def _path_of(url: str) -> str:
    """The path of a chapter URL, so the host failover can retarget it."""
    parsed = urlparse(url)
    return f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path

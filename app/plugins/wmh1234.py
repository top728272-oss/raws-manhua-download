"""Wmh1234 (wmh1234.com / reader.hqread.cc) source plugin."""

from __future__ import annotations

import html as _html
import logging
import re
from urllib.parse import parse_qs, quote, urlparse

from app.core.htmlutil import cut_recommendations, is_placeholder_cover, strip_chrome
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]+>")
_H1_TITLE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
_OG_TITLE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_OG_IMAGE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_OG_DESC = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_BOOK_AUTHOR = re.compile(
    r'<meta[^>]+property=["\']book:author["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_META_DESC = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_CARD_PATTERN = re.compile(
    r'<a[^>]+href=["\'](?:https?://[^/"\']*)?/comic/(\d+)\.html["\'][^>]*>(.*?)</a>',
    re.I | re.S,
)
_CHAPTER_ITEM_PATTERN = re.compile(
    r'<div[^>]*class=["\'][^"\']*chapter-item[^"\']*["\'][^>]*>(.*?)</div>\s*</div>',
    re.I | re.S,
)
_READER_IMG = re.compile(
    r'<img[^>]+class=["\'][^"\']*reader-image[^"\']*["\'][^>]*>',
    re.I,
)
_SRC_ATTR = re.compile(r'(?:data-src|data-original|src)=["\']([^"\']+)["\']', re.I)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG.sub("", fragment))).strip()


def _clean_ref(query: str) -> str:
    query = query.strip()
    if (
        query.startswith("http://")
        or query.startswith("https://")
        or "/" in query
        or ".html" in query
    ):
        parsed = urlparse(query)
        qs = parse_qs(parsed.query)
        if "id" in qs and qs["id"]:
            return qs["id"][0]
        path = parsed.path.strip("/")
        parts = path.split("/")
        if parts:
            last = parts[-1].replace(".html", "")
            if last.isdigit():
                return last
            for p in parts:
                p_clean = p.replace(".html", "")
                if p_clean.isdigit():
                    return p_clean
    return query.replace(".html", "").strip()


class Wmh1234Plugin(SourcePlugin):
    id = "wmh1234"
    name = "漫画1234 (wmh1234)"
    category = "General"
    fetch_mode = FetchMode.HTTP

    @property
    def base_url(self) -> str:
        return self.settings.get("base_url", "https://www.wmh1234.com").rstrip("/")

    @property
    def reader_url(self) -> str:
        return self.settings.get("reader_url", "https://reader.hqread.cc").rstrip("/")

    async def search(self, query: str) -> list[SearchResult]:
        ref = _clean_ref(query)
        if (
            query.startswith("http://")
            or query.startswith("https://")
            or (ref.isdigit() and len(ref) >= 4)
        ):
            try:
                url = f"{self.base_url}/comic/{ref}.html"
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
                logger.warning("wmh1234 ref search failed for '%s': %s", ref, err)
                return []

        try:
            url = f"{self.base_url}/search?key={quote(query)}"
            html = await self.fetcher.get_text(url)
            results = self._parse_search(html)
            if results:
                return results
        except Exception as err:
            logger.warning("wmh1234 search failed: %s; trying index.php/search fallback", err)

        try:
            html = await self.fetcher.get_text(
                f"{self.base_url}/index.php/search?key={quote(query)}"
            )
            return self._parse_search(html)
        except Exception as alt_err:
            logger.warning("wmh1234 fallback search failed: %s", alt_err)
            return []

    def _parse_search(self, html: str) -> list[SearchResult]:
        seen: set[str] = set()
        out: list[SearchResult] = []
        html = cut_recommendations(html)
        html = strip_chrome(html)

        for match in _CARD_PATTERN.finditer(html):
            slug = match.group(1)
            inner = match.group(2)
            if slug in seen:
                continue
            seen.add(slug)

            title_m = re.search(
                r'<h3[^>]*class=["\'][^"\']*comic-title[^"\']*["\'][^>]*>(.*?)</h3>',
                inner,
                re.I | re.S,
            )
            img_m = _SRC_ATTR.search(inner)
            author_m = re.search(
                r'<p[^>]*class=["\'][^"\']*comic-update[^"\']*["\'][^>]*>(.*?)</p>',
                inner,
                re.I | re.S,
            )

            title = _text(title_m.group(1)) if title_m else slug
            cover = _html.unescape(img_m.group(1)) if img_m else None
            if cover and not cover.startswith("http"):
                cover = f"{self.base_url}{cover}"
            if is_placeholder_cover(cover):
                cover = None
            author = _text(author_m.group(1)) if author_m else None

            out.append(
                SearchResult(
                    source_id=self.id,
                    title=title,
                    ref=slug,
                    cover_url=cover,
                    author=author,
                )
            )

        logger.info("wmh1234: search parsed %d results", len(out))
        return out

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        url = f"{self.base_url}/comic/{ref}.html"
        html = await self.fetcher.get_text(url)
        return self._parse_detail(html, result)

    def _parse_detail(self, html: str, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        h1_m = _H1_TITLE.search(html)
        og_t_m = _OG_TITLE.search(html)
        og_i_m = _OG_IMAGE.search(html)
        og_d_m = _OG_DESC.search(html)
        desc_m = _META_DESC.search(html)
        author_m = _BOOK_AUTHOR.search(html)

        title = (
            _text(h1_m.group(1)) if h1_m else (_text(og_t_m.group(1)) if og_t_m else result.title)
        )
        if title.endswith("漫画"):
            title = title[:-2].strip()
        if "_" in title:
            title = title.split("_")[0].strip()

        cover = _html.unescape(og_i_m.group(1)) if og_i_m else result.cover_url
        desc = _text(og_d_m.group(1)) if og_d_m else (_text(desc_m.group(1)) if desc_m else None)
        author = _text(author_m.group(1)) if author_m else result.author

        return MangaDetail(
            source_id=self.id,
            title=title or ref,
            ref=ref,
            cover_url=cover,
            author=author,
            description=desc,
            extra={"detail_html": html},
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        ref = _clean_ref(detail.ref)
        html = detail.extra.get("detail_html")
        if not html:
            html = await self.fetcher.get_text(f"{self.base_url}/comic/{ref}.html")
        return self._parse_chapters(html, ref)

    def _parse_chapters(self, html: str, manga_ref: str) -> list[Chapter]:
        chapters: list[Chapter] = []
        seen: set[str] = set()

        raw_items: list[tuple[str, str, int | None]] = []

        def _chapter_number(text: str) -> int | None:
            m = re.search(r"第\s*(\d+)\s*[話话]", text)
            return int(m.group(1)) if m else None

        item_pattern = re.compile(
            r'<div[^>]*class=["\'][^"\']*chapter-item[^"\']*["\'][^>]*>(.*?)</div>\s*</div>',
            re.I | re.S,
        )
        for match in item_pattern.finditer(html):
            item_html = match.group(1)
            token_m = re.search(
                r'href=["\'](?:https?://[^/"\']*)?/go/([A-Za-z0-9_-]+)["\']', item_html, re.I
            )
            if not token_m:
                continue
            token = token_m.group(1)
            if token in seen:
                continue
            seen.add(token)

            num_m = re.search(
                r'<span[^>]*class=["\'][^"\']*chapter-number[^"\']*["\'][^>]*>(.*?)</span>',
                item_html,
                re.I | re.S,
            )
            title_m = re.search(
                r'<span[^>]*class=["\'][^"\']*chapter-title[^"\']*["\'][^>]*>(.*?)</span>',
                item_html,
                re.I | re.S,
            )

            ch_num = _text(num_m.group(1)) if num_m else ""
            ch_t = _text(title_m.group(1)) if title_m else ""
            full_title = f"{ch_num} {ch_t}".strip() if (ch_num and ch_t) else (ch_t or ch_num)
            raw_items.append((token, full_title, _chapter_number(full_title)))

        if not raw_items:
            pattern = re.compile(
                r'<a[^>]+href=["\'](?:https?://[^/"\']*)?/go/([A-Za-z0-9_-]+)["\'][^>]*>(.*?)</a>',
                re.I | re.S,
            )
            for token, inner in pattern.findall(html):
                if token in seen:
                    continue
                title = _text(inner)
                if title in ("开始阅读", "阅读"):
                    continue
                seen.add(token)
                raw_items.append((token, title, _chapter_number(title)))

        if raw_items and all(num is not None for _t, _r, num in raw_items):
            raw_items.sort(key=lambda item: item[2] or 0)

        for idx, (token, title, _num) in enumerate(raw_items):
            ch_title = title or f"第{idx + 1}话"
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=manga_ref,
                    title=ch_title,
                    ref=token,
                    index=idx + 1,
                    extra={"token": token, "go_url": f"/go/{token}"},
                )
            )

        logger.info("wmh1234: parsed %d chapters for ref '%s'", len(chapters), manga_ref)
        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        token = (chapter.extra or {}).get("token") or chapter.ref
        url = f"{self.reader_url}/r/{token}"
        try:
            html = await self.fetcher.get_text(url)
            pages = self._parse_pages(html)
            if pages:
                return pages
            logger.warning("wmh1234 reader URL '%s' listed 0 pages; trying /go/ path", url)
        except Exception as err:
            logger.warning("wmh1234 reader URL '%s' failed: %s; trying /go/ path", url, err)
        try:
            html = await self.fetcher.get_text(f"{self.base_url}/go/{token}")
            pages = self._parse_pages(html)
            if pages:
                return pages
        except Exception as err:
            logger.warning("wmh1234 /go/ path failed: %s", err)
        return []

    def _parse_pages(self, html: str) -> list[Page]:
        pages: list[Page] = []
        idx = 0

        for match in _READER_IMG.finditer(html):
            img_tag = match.group(0)
            src_m = _SRC_ATTR.search(img_tag)
            if src_m:
                img_url = _html.unescape(src_m.group(1)).strip()
                if not img_url or "placeholder" in img_url:
                    continue
                pages.append(
                    Page(
                        index=idx,
                        image_url=img_url,
                        referer=f"{self.reader_url}/",
                    )
                )
                idx += 1

        if not pages:
            all_imgs = _SRC_ATTR.findall(html)
            seen_urls: set[str] = set()
            for src in all_imgs:
                src_clean = _html.unescape(src).strip()
                if not src_clean or "placeholder" in src_clean or src_clean in seen_urls:
                    continue
                if "kantu" in src_clean or "wszwhg" in src_clean:
                    seen_urls.add(src_clean)
                    pages.append(
                        Page(
                            index=idx,
                            image_url=src_clean,
                            referer=f"{self.reader_url}/",
                        )
                    )
                    idx += 1

        logger.info("wmh1234: parsed %d pages", len(pages))
        return pages

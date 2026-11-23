"""GuaziManhua (guazimanhua.com) source plugin."""

from __future__ import annotations

import html as _html
import logging
import re
from urllib.parse import parse_qs, quote, urlparse

from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.core.output import leading_chapter_number
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
_META_DESC = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_CHAPTER_LINK = re.compile(
    r'<a[^>]+href=["\'](?:https?://[^/"\']*)?/(?:chapter\.php\?id=|\?chapter=)(\d+)["\'][^>]*>(.*?)</a>',
    re.I | re.S,
)
_READING_IMG = re.compile(
    r'<img[^>]+class=["\'][^"\']*reading-image[^"\']*["\'][^>]*>',
    re.I,
)
_SRC_ATTR = re.compile(r'(?:src|data-src|data-original)=["\']([^"\']+)["\']', re.I)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG.sub("", fragment))).strip()


def _clean_ref(query: str) -> str:
    query = query.strip()
    if (
        query.startswith("http://")
        or query.startswith("https://")
        or "/" in query
        or "id=" in query
    ):
        parsed = urlparse(query)
        qs = parse_qs(parsed.query)
        if "id" in qs and qs["id"]:
            return qs["id"][0]
        path = parsed.path.strip("/")
        parts = path.split("/")
        if parts:
            last = parts[-1]
            if last.isdigit():
                return last
    return query


class GuaziManhuaPlugin(SourcePlugin):
    id = "guazimanhua"
    name = "瓜子漫画 (guazimanhua)"
    category = "General"
    fetch_mode = FetchMode.HTTP

    @property
    def base_url(self) -> str:
        return self.settings.get("base_url", "https://www.guazimanhua.com").rstrip("/")

    async def search(self, query: str) -> list[SearchResult]:
        ref = _clean_ref(query)
        if (
            query.startswith("http://")
            or query.startswith("https://")
            or (ref.isdigit() and len(ref) >= 4)
        ):
            try:
                url = f"{self.base_url}/comic.php?id={ref}"
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
                logger.warning("guazimanhua ref search failed for '%s': %s", ref, err)
                return []

        try:
            url = f"{self.base_url}/category.php?keyword={quote(query)}"
            html = await self.fetcher.get_text(url)
            return self._parse_search(html)
        except Exception as err:
            logger.warning("guazimanhua search URL failed: %s", err)
            return []

    def _parse_search(self, html: str) -> list[SearchResult]:
        seen: set[str] = set()
        out: list[SearchResult] = []

        card_pattern = re.compile(
            r'<article[^>]*class=["\'][^"\']*card[^"\']*["\'][^>]*>(.*?)</article>',
            re.I | re.S,
        )

        for match in card_pattern.finditer(html):
            card_html = match.group(1)
            link_m = re.search(
                r'href=["\'](?:https?://[^/"\']*)?/comic\.php\?id=(\d+)["\']', card_html, re.I
            )
            if not link_m:
                continue
            cid = link_m.group(1)
            if cid in seen:
                continue
            seen.add(cid)

            title_m = re.search(r"<h3[^>]*>(.*?)</h3>", card_html, re.I | re.S)
            img_m = re.search(r'<img[^>]+(?:src|data-src)=["\']([^"\']+)["\']', card_html, re.I)
            meta_m = re.search(
                r'<div[^>]*class=["\'][^"\']*meta[^"\']*["\'][^>]*>(.*?)</div>',
                card_html,
                re.I | re.S,
            )

            title = _text(title_m.group(1)) if title_m else cid
            cover = _html.unescape(img_m.group(1)) if img_m else None
            author = None
            if meta_m:
                meta_text = _text(meta_m.group(1))
                if "·" in meta_text:
                    author = meta_text.split("·")[0].strip()

            out.append(
                SearchResult(
                    source_id=self.id,
                    title=title,
                    ref=cid,
                    cover_url=cover,
                    author=author,
                )
            )

        logger.info("guazimanhua: search parsed %d results", len(out))
        return out

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        url = f"{self.base_url}/comic.php?id={ref}"
        html = await self.fetcher.get_text(url)
        return self._parse_detail(html, result)

    def _parse_detail(self, html: str, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        h1_m = _H1_TITLE.search(html)
        og_t_m = _OG_TITLE.search(html)
        og_i_m = _OG_IMAGE.search(html)
        desc_m = _META_DESC.search(html)

        title = (
            _text(h1_m.group(1)) if h1_m else (_text(og_t_m.group(1)) if og_t_m else result.title)
        )
        cover = _html.unescape(og_i_m.group(1)) if og_i_m else result.cover_url
        if not cover:
            cover_m = re.search(
                r'<img[^>]+class=["\'][^"\']*cover[^"\']*["\'][^>]+src=["\']([^"\']+)["\']',
                html,
                re.I,
            )
            if cover_m:
                cover = _html.unescape(cover_m.group(1))

        desc = desc_m.group(1).strip() if desc_m else None
        author = result.author
        status = None

        meta_m = re.search(
            r'<div[^>]*class=["\'][^"\']*meta[^"\']*["\'][^>]*>(.*?)</div>', html, re.I | re.S
        )
        if meta_m:
            meta_text = _text(meta_m.group(1))
            if "·" in meta_text:
                parts = [p.strip() for p in meta_text.split("·")]
                author = parts[0]
                if len(parts) > 1:
                    status = parts[1]

        return MangaDetail(
            source_id=self.id,
            title=title or ref,
            ref=ref,
            cover_url=cover,
            author=author,
            description=desc,
            status=status,
            extra={"detail_html": html},
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        ref = _clean_ref(detail.ref)
        html = detail.extra.get("detail_html")
        if not html:
            html = await self.fetcher.get_text(f"{self.base_url}/comic.php?id={ref}")

        return self._parse_chapters(html, ref)

    def _parse_chapters(self, html: str, manga_ref: str) -> list[Chapter]:
        raw_chapters: list[tuple[str, str]] = []
        seen: set[str] = set()

        for cid, inner in _CHAPTER_LINK.findall(html):
            title = _text(inner)
            if not title or title in ("开始阅读", "从第一章开始阅读"):
                continue
            if "阅读" in title and len(title) > 30:
                continue
            if title.endswith(" 阅读"):
                title = title[:-3].strip()

            if cid in seen:
                continue
            seen.add(cid)
            raw_chapters.append((cid, title))

        raw_chapters.reverse()

        chapters: list[Chapter] = []
        total = len(raw_chapters)
        pad_width = max(3, len(str(total)))

        for idx, (cid, title) in enumerate(raw_chapters):
            ch_num = idx + 1
            raw_t = title or f"Chapter {ch_num}"
            if leading_chapter_number(raw_t) is None:
                formatted_title = f"{ch_num:0{pad_width}d} - {raw_t}"
            else:
                formatted_title = raw_t

            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=manga_ref,
                    title=formatted_title,
                    ref=cid,
                    index=ch_num,
                    extra={"href": f"/chapter.php?id={cid}"},
                )
            )

        logger.info("guazimanhua: parsed %d chapters for ref '%s'", len(chapters), manga_ref)
        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        ref = _clean_ref(chapter.ref)
        url = f"{self.base_url}/chapter.php?id={ref}"
        html = await self.fetcher.get_text(url)
        return self._parse_pages(html)

    def _parse_pages(self, html: str) -> list[Page]:
        pages: list[Page] = []
        idx = 0

        for match in _READING_IMG.finditer(html):
            img_tag = match.group(0)
            src_m = _SRC_ATTR.search(img_tag)
            if src_m:
                img_url = _html.unescape(src_m.group(1))
                pages.append(Page(index=idx, image_url=img_url, referer=f"{self.base_url}/"))
                idx += 1

        if not pages:
            all_imgs = _SRC_ATTR.findall(html)
            seen_urls: set[str] = set()
            for src in all_imgs:
                src_clean = _html.unescape(src)
                if (
                    "chapters" in src_clean or "guazicdn" in src_clean
                ) and src_clean not in seen_urls:
                    seen_urls.add(src_clean)
                    pages.append(Page(index=idx, image_url=src_clean, referer=f"{self.base_url}/"))
                    idx += 1

        logger.info("guazimanhua: parsed %d pages", len(pages))
        return pages

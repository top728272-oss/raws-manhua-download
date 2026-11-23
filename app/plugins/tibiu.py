"""Tibiu (comic.tibiu.net) source plugin."""

from __future__ import annotations

import html as _html
import logging
import re
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from app.core.htmlutil import strip_chrome
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
_META_DESC = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_CHAPTER_HREF = re.compile(r"/chapter/(\d+)/(\d+)", re.I)


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
        if "mid" in qs and qs["mid"]:
            return qs["mid"][0]
        path = parsed.path.strip("/")
        parts = path.split("/")
        if parts:
            last = parts[-1]
            if last.isdigit():
                return last
    return query


class TibiuPlugin(SourcePlugin):
    id = "tibiu"
    name = "TIBIU漫画 (tibiu)"
    category = "General"
    fetch_mode = FetchMode.HTTP

    @property
    def base_url(self) -> str:
        return self.settings.get("base_url", "https://comic.tibiu.net").rstrip("/")

    async def search(self, query: str) -> list[SearchResult]:
        ref = _clean_ref(query)
        if (
            query.startswith("http://")
            or query.startswith("https://")
            or (ref.isdigit() and len(ref) >= 4)
        ):
            try:
                url = f"{self.base_url}/comic/{ref}"
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
                logger.warning("tibiu ref search failed for '%s': %s", ref, err)
                return []

        try:
            url = f"{self.base_url}/index.php/api/data/search?key={quote(query)}&page=1"
            data = await self.fetcher.get_json(url)

            return self._parse_search_json(data)
        except Exception as err:
            logger.warning("tibiu search API failed: %s; trying html search fallback", err)

        try:
            html = await self.fetcher.get_text(f"{self.base_url}/search?key={quote(query)}")
            return self._parse_search_html(html)
        except Exception as html_err:
            logger.warning("tibiu search HTML fallback failed: %s", html_err)
            return []

    def _parse_search_json(self, data: Any) -> list[SearchResult]:
        if isinstance(data, dict):
            items = data.get("data", [])
        elif isinstance(data, list):
            items = data
        else:
            return []

        out: list[SearchResult] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            mid = str(it.get("id", "")).strip()
            name = _text(str(it.get("name", ""))) or mid
            if not mid:
                continue
            cover = it.get("pic")
            if cover and isinstance(cover, str) and not cover.startswith("http"):
                cover = f"{self.base_url}{cover}"
            author = _text(str(it.get("author", ""))) or None
            out.append(
                SearchResult(
                    source_id=self.id,
                    title=name,
                    ref=mid,
                    cover_url=cover,
                    author=author,
                )
            )
        logger.info("tibiu: search API parsed %d results", len(out))
        return out

    def _parse_search_html(self, html: str) -> list[SearchResult]:
        seen: set[str] = set()
        out: list[SearchResult] = []
        html = strip_chrome(html)
        pattern = re.compile(
            r'<a[^>]+href=["\'](?:https?://[^/"\']*)?/comic/(\d+)["\'][^>]*>(.*?)</a>',
            re.I | re.S,
        )
        for mid, inner in pattern.findall(html):
            if mid in seen:
                continue
            title = _text(inner)
            if not title or len(title) > 100:
                continue
            seen.add(mid)
            out.append(SearchResult(source_id=self.id, title=title, ref=mid))
        return out

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        url = f"{self.base_url}/comic/{ref}"
        html = await self.fetcher.get_text(url)
        return self._parse_detail(html, result)

    def _parse_detail(self, html: str, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        h1_m = _H1_TITLE.search(html)
        og_t_m = _OG_TITLE.search(html)
        og_i_m = _OG_IMAGE.search(html)
        og_d_m = _OG_DESC.search(html)
        desc_m = _META_DESC.search(html)
        title_tag_m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)

        candidates = []
        if og_t_m:
            candidates.append(_text(og_t_m.group(1)))
        if title_tag_m:
            candidates.append(_text(title_tag_m.group(1)))
        if h1_m:
            candidates.append(_text(h1_m.group(1)))
        if result.title:
            candidates.append(result.title)

        title = ""
        for cand in candidates:
            if not cand:
                continue
            cleaned = cand.split(" - ")[0].split("_")[0].strip()
            if cleaned and cleaned not in ("加载中", "加载中...", "Loading...", "请稍候"):
                title = cleaned
                break

        cover = _html.unescape(og_i_m.group(1)) if og_i_m else result.cover_url
        desc = _text(og_d_m.group(1)) if og_d_m else (_text(desc_m.group(1)) if desc_m else None)
        author = result.author

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
        url = f"{self.base_url}/index.php/api/comic/chapter?mid={ref}"
        try:
            data = await self.fetcher.get_json(url)
            chapters = self._parse_chapters_json(data, ref)
            if chapters:
                return chapters
        except Exception as err:
            logger.warning("tibiu list_chapters API failed: %s; trying html fallback", err)

        html = detail.extra.get("detail_html")
        if not html:
            html = await self.fetcher.get_text(f"{self.base_url}/comic/{ref}")
        return self._parse_chapters_html(html, ref)

    def _parse_chapters_json(self, data: Any, manga_ref: str) -> list[Chapter]:
        if isinstance(data, dict):
            raw_list = data.get("data", []) or data.get("list", [])
        elif isinstance(data, list):
            raw_list = data
        else:
            return []

        chapters: list[Chapter] = []
        for idx, item in enumerate(raw_list):
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or item.get("cid", "")).strip()
            name = _text(str(item.get("name", ""))) or f"第{idx + 1}话"
            if not cid:
                continue
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=manga_ref,
                    title=name,
                    ref=f"{manga_ref}/{cid}",
                    index=idx + 1,
                    extra={"cid": cid, "mid": manga_ref},
                )
            )
        logger.info("tibiu: parsed %d chapters for ref '%s'", len(chapters), manga_ref)
        return chapters

    def _parse_chapters_html(self, html: str, manga_ref: str) -> list[Chapter]:
        chapters: list[Chapter] = []
        seen: set[str] = set()
        pattern = re.compile(
            r'<a[^>]+href=["\'](?:https?://[^/"\']*)?/chapter/(\d+)/(\d+)["\'][^>]*>(.*?)</a>',
            re.I | re.S,
        )
        for mid, cid, inner in pattern.findall(html):
            if cid in seen:
                continue
            seen.add(cid)
            title = _text(inner) or f"第{len(chapters) + 1}话"
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=manga_ref,
                    title=title,
                    ref=f"{manga_ref}/{cid}",
                    index=len(chapters) + 1,
                    extra={"cid": cid, "mid": mid},
                )
            )
        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        cid = chapter.extra.get("cid") if chapter.extra else None
        if not cid:
            parts = chapter.ref.split("/")
            cid = parts[-1] if len(parts) > 1 else chapter.ref

        url = f"{self.base_url}/index.php/api/data/pic?cid={cid}"
        data = await self.fetcher.get_json(url)
        return self._parse_pages_json(data)

    def _parse_pages_json(self, data: Any) -> list[Page]:
        if isinstance(data, dict):
            raw_list = data.get("data", [])
        elif isinstance(data, list):
            raw_list = data
        else:
            return []

        items = [it for it in raw_list if isinstance(it, dict) and it.get("img")]

        def _sort_key(item: dict[str, Any]) -> str:
            img = str(item.get("img", ""))
            return img.rsplit("/", 1)[-1] if "/" in img else img

        sorted_items = sorted(items, key=_sort_key)

        pages: list[Page] = []
        for idx, it in enumerate(sorted_items):
            img_url = str(it.get("img", "")).strip()
            if not img_url:
                continue
            pages.append(
                Page(
                    index=idx,
                    image_url=img_url,
                    referer=f"{self.base_url}/",
                )
            )
        logger.info("tibiu: parsed %d pages", len(pages))
        return pages

"""HanMan18 (hanman18.com) source plugin."""

from __future__ import annotations

import base64
import binascii
import html as _html
import logging
import re
from urllib.parse import quote, urljoin, urlparse

from app.core.htmlutil import strip_chrome
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]+>")
_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
_IMG = re.compile(r"<img\b([^>]*)>", re.I | re.S)
_META = re.compile(r"<meta\b([^>]*)>", re.I | re.S)
_ATTR = re.compile(r"([:\w-]+)\s*=\s*([\"'])(.*?)\2", re.I | re.S)
_READER_IMAGES = re.compile(r"\bvar\s+slides_p_path\s*=\s*\[(.*?)\]\s*;", re.I | re.S)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG.sub("", fragment))).strip()


def _attrs(fragment: str) -> dict[str, str]:
    return {name.lower(): _html.unescape(value) for name, _, value in _ATTR.findall(fragment)}


def _clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", _html.unescape(title)).strip()
    return re.sub(r"\s+-\s+hanman18\.com\s*$", "", title, flags=re.I).strip()


def _clean_ref(query: str) -> str:
    query = query.strip()
    if query.startswith(("http://", "https://", "/")):
        parsed = urlparse(query)
        parts = [part for part in parsed.path.split("/") if part]
        if "manhwa" in parts:
            pos = parts.index("manhwa")
            if len(parts) > pos + 1:
                return parts[pos + 1]
        return parts[-1] if parts else query
    return query.rstrip("/")


def _meta_value(html: str, key: str) -> str | None:
    key = key.lower()
    for match in _META.finditer(html):
        attrs = _attrs(match.group(1))
        if attrs.get("name", "").lower() == key or attrs.get("property", "").lower() == key:
            return attrs.get("content")
    return None


class HanMan18Plugin(SourcePlugin):
    id = "hanman18"
    name = "HanMan18"
    category = "18+"
    fetch_mode = FetchMode.HTTP

    @property
    def base_url(self) -> str:
        return self.settings.get("base_url", "https://hanman18.com").rstrip("/")

    @property
    def image_base_url(self) -> str:
        return self.settings.get("image_base_url", "https://azminv2.hanman18.com").rstrip("/")

    async def search(self, query: str) -> list[SearchResult]:
        query = query.strip()
        ref = _clean_ref(query)
        if query.startswith(("http://", "https://", "/manhwa/")):
            try:
                html = await self.fetcher.get_text(f"{self.base_url}/manhwa/{ref}")
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
                logger.warning("hanman18 direct ref search failed: %s", err)
                return []

        try:
            html = await self.fetcher.get_text(f"{self.base_url}/list-manga?search={quote(query)}")
            return self._parse_search(html)
        except Exception as err:
            logger.warning("hanman18 search failed: %s", err)
            return []

    def _parse_search(self, html: str) -> list[SearchResult]:
        seen: set[str] = set()
        results: list[SearchResult] = []
        html = strip_chrome(html)

        for attrs_text, inner in _ANCHOR.findall(html):
            attrs = _attrs(attrs_text)
            href = attrs.get("href", "")
            parsed = urlparse(urljoin(self.base_url + "/", href))
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) != 2 or parts[0].lower() != "manhwa":
                continue

            ref = parts[1]
            if ref in seen:
                continue
            seen.add(ref)

            image = _IMG.search(inner)
            image_attrs = _attrs(image.group(1)) if image else {}
            cover = image_attrs.get("data-src") or image_attrs.get("src")
            if cover:
                cover = urljoin(self.base_url + "/", cover)

            heading = re.search(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", inner, re.I | re.S)
            title = _text(heading.group(1)) if heading else ""
            if not title and image_attrs.get("alt"):
                title = image_attrs["alt"].strip()
            if not title:
                title = attrs.get("title", "") or _text(inner)
            if not title and not cover:
                continue

            results.append(
                SearchResult(
                    source_id=self.id,
                    title=title or ref,
                    ref=ref,
                    cover_url=cover,
                )
            )

        logger.info("hanman18: search parsed %d results", len(results))
        return results[:20]

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        html = await self.fetcher.get_text(f"{self.base_url}/manhwa/{ref}")
        return self._parse_detail(html, result)

    def _parse_detail(self, html: str, result: SearchResult) -> MangaDetail:
        title = _meta_value(html, "title") or _meta_value(html, "og:title")
        if not title:
            title_match = re.search(r"<title\b[^>]*>(.*?)</title>", html, re.I | re.S)
            title = title_match.group(1) if title_match else result.title

        description = _meta_value(html, "description") or _meta_value(html, "og:description")
        cover = _meta_value(html, "og:image") or result.cover_url
        chapter_count = len(self._chapter_links(html, _clean_ref(result.ref)))

        return MangaDetail(
            source_id=self.id,
            title=_clean_title(title or result.title) or _clean_ref(result.ref),
            ref=_clean_ref(result.ref),
            cover_url=cover,
            description=description.strip() if description else None,
            chapter_count=chapter_count or None,
            extra={"detail_html": html},
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        ref = _clean_ref(detail.ref)
        html = detail.extra.get("detail_html")
        if not html:
            html = await self.fetcher.get_text(f"{self.base_url}/manhwa/{ref}")
        return self._parse_chapters(html, ref)

    def _chapter_links(self, html: str, manga_ref: str) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = []
        seen: set[str] = set()

        for attrs_text, inner in _ANCHOR.findall(html):
            attrs = _attrs(attrs_text)
            if "chapter_num" not in attrs.get("class", "").split():
                continue
            href = attrs.get("href", "").strip()
            parsed = urlparse(urljoin(self.base_url + "/", href))
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) != 3 or parts[0].lower() != "manhwa" or parts[1] != manga_ref:
                continue
            chapter_ref = parts[2]
            if chapter_ref in seen:
                continue
            seen.add(chapter_ref)
            links.append((chapter_ref, _text(inner) or chapter_ref))

        return links

    def _parse_chapters(self, html: str, manga_ref: str) -> list[Chapter]:
        raw = self._chapter_links(html, _clean_ref(manga_ref))
        raw.reverse()
        chapters = [
            Chapter(
                source_id=self.id,
                manga_ref=_clean_ref(manga_ref),
                title=title,
                ref=chapter_ref,
                index=index,
                extra={"href": f"/manhwa/{_clean_ref(manga_ref)}/{chapter_ref}"},
            )
            for index, (chapter_ref, title) in enumerate(raw, start=1)
        ]
        logger.info("hanman18: parsed %d chapters for ref '%s'", len(chapters), manga_ref)
        return chapters

    def _parse_pages(self, html: str) -> list[Page]:
        match = _READER_IMAGES.search(html)
        if not match:
            raise ValueError("HanMan18 reader did not contain slides_p_path")

        tokens = re.findall(r"[\"']([^\"']+)[\"']", match.group(1))
        pages: list[Page] = []
        seen: set[str] = set()
        for token in tokens:
            try:
                decoded = base64.b64decode(token, validate=True).decode("utf-8").strip()
            except (binascii.Error, UnicodeDecodeError) as err:
                logger.warning("hanman18: skipping invalid image token: %s", err)
                continue
            image_url = urljoin(self.image_base_url + "/", decoded)
            if not image_url or image_url in seen:
                continue
            seen.add(image_url)
            pages.append(Page(index=len(pages), image_url=image_url, referer=f"{self.base_url}/"))

        if not pages:
            raise ValueError("HanMan18 reader contained no valid image URLs")
        return pages

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        href = chapter.extra.get("href")
        url = urljoin(self.base_url + "/", href or f"manhwa/{chapter.manga_ref}/{chapter.ref}")
        html = await self.fetcher.get_text(url)
        return self._parse_pages(html)

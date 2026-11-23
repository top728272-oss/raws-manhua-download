"""ManWang source plugin (漫王 - https://www.manwang.net)."""

from __future__ import annotations

import base64
import html as _html
import json
import logging
import re
from typing import Any
from urllib.parse import quote, urlparse

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from app.core.htmlutil import is_placeholder_cover
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

_H1_TITLE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
_OG_TITLE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']*)["\']|<meta[^>]+content=["\']([^"\']*)["\'][^>]*property=["\']og:title["\']',
    re.I,
)
_OG_IMAGE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\']*)["\']|<meta[^>]+content=["\']([^"\']*)["\'][^>]*property=["\']og:image["\']',
    re.I,
)
_OG_DESC = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]*content=["\']([^"\']*)["\']|<meta[^>]+content=["\']([^"\']*)["\'][^>]*property=["\']og:description["\']',
    re.I,
)
_OG_AUTHOR = re.compile(
    r'<meta[^>]+property=["\']og:novel:author["\'][^>]*content=["\']([^"\']*)["\']|<meta[^>]+content=["\']([^"\']*)["\'][^>]*property=["\']og:novel:author["\']',
    re.I,
)
_META_DESC = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']*)["\']|<meta[^>]+content=["\']([^"\']*)["\'][^>]*name=["\']description["\']',
    re.I,
)
_CHAPTER_PATTERN = re.compile(
    r'<a[^>]+href=["\'](?:https?://[^/"\']*)?/chapter/([^"\'/?#]+)["\'][^>]*>(.*?)</a>',
    re.I | re.S,
)
_COMIC_CARD_PATTERN = re.compile(
    r'<section[^>]*class=["\'][^"\']*mod-hitem-comic[^"\']*["\'][^>]*>(.*?)</section>',
    re.I | re.S,
)
_PARAMS_PATTERN = re.compile(r"params\s*=\s*['\"]([^'\"]+)['\"]", re.I)

AES_KEY = b"9S8$vJnU2ANeSRoF"


def _clean_ref(query: str) -> str:
    query = query.strip()
    if query.startswith(("http://", "https://", "/")):
        parsed = urlparse(query)
        path = parsed.path.strip("/")
        parts = path.split("/")
        if "book" in parts:
            idx = parts.index("book")
            if idx + 1 < len(parts):
                return parts[idx + 1].replace(".html", "")
        if "chapter" in parts:
            idx = parts.index("chapter")
            if idx + 1 < len(parts):
                return parts[idx + 1].replace(".html", "")
        return parts[-1].replace(".html", "") if parts else query
    return query.replace(".html", "").strip()


def _text(raw: str | None) -> str:
    if not raw:
        return ""
    cleaned = re.sub(r"<[^>]+>", "", raw)
    return _html.unescape(cleaned).strip()


def _decrypt_params(params_b64: str) -> dict[str, Any]:
    raw = base64.b64decode(params_b64)
    if len(raw) < 16:
        raise ValueError("ManWang params payload too short")
    iv = raw[:16]
    ciphertext = raw[16:]
    cipher = AES.new(AES_KEY, AES.MODE_CBC, iv)
    decrypted = unpad(cipher.decrypt(ciphertext), AES.block_size)
    return json.loads(decrypted.decode("utf-8"))


class ManWangPlugin(SourcePlugin):
    id = "manwang"
    name = "漫王 (manwang)"
    category = "General"
    fetch_mode = FetchMode.HTTP

    @property
    def base_url(self) -> str:
        return self.settings.get("base_url", "https://www.manwang.net").rstrip("/")

    async def search(self, query: str) -> list[SearchResult]:
        ref = _clean_ref(query)
        if (
            query.startswith("http://")
            or query.startswith("https://")
            or (ref.isdigit() and len(ref) >= 4)
        ):
            try:
                url = f"{self.base_url}/book/{ref}"
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
                logger.warning("manwang direct ref search failed for '%s': %s", ref, err)
                return []

        try:
            url = f"{self.base_url}/index.php/search?key={quote(query)}"
            html = await self.fetcher.get_text(url)
            results = self._parse_search(html)
            if results:
                return results
        except Exception as err:
            logger.warning("manwang search failed: %s; trying /search/ fallback", err)

        try:
            url = f"{self.base_url}/search/{quote(query)}"
            html = await self.fetcher.get_text(url)
            return self._parse_search(html)
        except Exception as alt_err:
            logger.warning("manwang search fallback failed: %s", alt_err)
            return []

    def _parse_search(self, html: str) -> list[SearchResult]:
        seen: set[str] = set()
        out: list[SearchResult] = []

        for match in _COMIC_CARD_PATTERN.finditer(html):
            card_html = match.group(1)
            link_m = re.search(r'href=["\'](?:https?://[^/"\']*)?/book/(\d+)["\']', card_html, re.I)
            if not link_m:
                continue
            book_id = link_m.group(1)
            if book_id in seen:
                continue
            seen.add(book_id)

            title_m = re.search(r"<h2[^>]*>(.*?)</h2>", card_html, re.I | re.S)
            img_m = re.search(r'<img[^>]+data-src=["\']([^"\']+)["\']', card_html, re.I)
            if not img_m:
                img_m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', card_html, re.I)

            title = _text(title_m.group(1)) if title_m else book_id
            cover = _html.unescape(img_m.group(1)) if img_m else None
            if cover and not cover.startswith("http"):
                cover = f"{self.base_url}{cover}"
            if is_placeholder_cover(cover):
                cover = None

            out.append(
                SearchResult(
                    source_id=self.id,
                    title=title,
                    ref=book_id,
                    cover_url=cover,
                )
            )

        logger.info("manwang: search parsed %d results", len(out))
        return out

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        url = f"{self.base_url}/book/{ref}"
        html = await self.fetcher.get_text(url)
        return self._parse_detail(html, result)

    def _parse_detail(self, html: str, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        h1_m = _H1_TITLE.search(html)
        og_t_m = _OG_TITLE.search(html)
        og_i_m = _OG_IMAGE.search(html)
        og_d_m = _OG_DESC.search(html)
        og_a_m = _OG_AUTHOR.search(html)
        desc_m = _META_DESC.search(html)

        candidates = []
        if og_t_m:
            candidates.append(_text(og_t_m.group(1) or og_t_m.group(2)))
        title_tag_m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
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
            cleaned = cand.split("_")[0].split("-")[0].strip()
            if cleaned and cleaned not in ("加载中", "Loading...", "请稍候"):
                title = cleaned
                break

        cover = _html.unescape(og_i_m.group(1) or og_i_m.group(2)) if og_i_m else result.cover_url
        if cover and not cover.startswith("http"):
            cover = f"{self.base_url}{cover}"

        desc = (
            _text(og_d_m.group(1) or og_d_m.group(2))
            if og_d_m
            else (_text(desc_m.group(1) or desc_m.group(2)) if desc_m else None)
        )
        author_raw = (
            _text(og_a_m.group(1) or og_a_m.group(2)) if og_a_m else (result.author or None)
        )
        if author_raw and author_raw.startswith("作者:"):
            author_raw = author_raw[3:].strip()

        return MangaDetail(
            source_id=self.id,
            title=title or ref,
            ref=ref,
            cover_url=cover,
            author=author_raw,
            description=desc,
            extra={"detail_html": html},
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        ref = _clean_ref(detail.ref)
        html = detail.extra.get("detail_html")
        if not html:
            html = await self.fetcher.get_text(f"{self.base_url}/book/{ref}")
        return self._parse_chapters(html, ref)

    def _parse_chapters(self, html: str, manga_ref: str) -> list[Chapter]:
        raw_chapters: list[tuple[str, str]] = []
        seen: set[str] = set()

        for ch_ref, inner in _CHAPTER_PATTERN.findall(html):
            title = _text(inner)
            if not title or title in ("开始阅读", "继续阅读", "倒序", "正序"):
                continue
            if ch_ref in seen:
                continue
            seen.add(ch_ref)
            raw_chapters.append((ch_ref, title))

        chapters: list[Chapter] = []
        for idx, (ch_ref, title) in enumerate(raw_chapters):
            ch_title = title or f"第{idx + 1}话"
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=manga_ref,
                    title=ch_title,
                    ref=ch_ref,
                    index=idx + 1,
                    extra={"chapter_ref": ch_ref, "href": f"/chapter/{ch_ref}"},
                )
            )

        logger.info("manwang: parsed %d chapters for ref '%s'", len(chapters), manga_ref)
        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        ch_ref = chapter.extra.get("chapter_ref") or chapter.ref
        url = f"{self.base_url}/chapter/{ch_ref}"
        html = await self.fetcher.get_text(url)
        return self._parse_pages(html, chapter_title=chapter.title)

    def _parse_pages(self, html: str, chapter_title: str = "") -> list[Page]:
        m = _PARAMS_PATTERN.search(html)
        if not m:
            raise ValueError("ManWang chapter page does not contain params payload")

        params_b64 = m.group(1)
        data = _decrypt_params(params_b64)
        image_urls = data.get("images")
        if not isinstance(image_urls, list) or not image_urls:
            raise ValueError("ManWang decrypted params contain no images")

        pages: list[Page] = []
        for img_url in image_urls:
            if not isinstance(img_url, str) or not img_url:
                continue
            pages.append(
                Page(
                    index=len(pages),
                    image_url=img_url,
                    referer=f"{self.base_url}/",
                )
            )

        logger.info(
            "manwang: resolved %d pages for chapter '%s'",
            len(pages),
            chapter_title or "unknown",
        )
        return pages

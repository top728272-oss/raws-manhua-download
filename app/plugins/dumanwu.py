from __future__ import annotations

import base64
import html as _html
import json
import logging
import re
from urllib.parse import urljoin

from app.core.models import (
    Chapter,
    FetchMode,
    MangaDetail,
    Page,
    SearchResult,
)
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")

_XOR_KEYS: list[str] = [
    "c21raHkyNTg=",
    "c21rZDk1ZnY=",
    "bWQ0OTY5NTI=",
    "Y2Rjc2R3cQ==",
    "dmJmc2EyNTY=",
    "Y2F3ZjE1MWM=",
    "Y2Q1NmN2ZGE=",
    "OGtpaG50OQ==",
    "ZHNvMTV0bG8=",
    "NWtvNnBsaHk=",
]


def _clean_ref(raw: str) -> str:
    cleaned = raw.strip()
    cleaned = re.sub(r"^https?://[^/]+/", "", cleaned)
    cleaned = cleaned.strip("/").removesuffix(".html")
    if "/" in cleaned:
        cleaned = cleaned.split("/")[0]
    return cleaned


def _text(raw: str | None) -> str:
    if not raw:
        return ""
    stripped = _TAG_RE.sub("", raw)
    return _html.unescape(stripped).strip()


def _unpack_dean_edwards(packed_js: str) -> str:
    match = re.search(
        r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.*?)',(\d+),(\d+),'(.*?)'\.split\('\|'\),(\d+),(\{\})\)\)",
        packed_js,
        re.DOTALL,
    )
    if not match:
        return ""

    p, a_str, c_str, k_str, _, _ = match.groups()
    a = int(a_str)
    c = int(c_str)
    k_list = k_str.split("|")

    def get_word(c_val: int, a_val: int) -> str:
        def _e(val: int) -> str:
            res = ""
            if val >= a_val:
                res += _e(val // a_val)
            rem = val % a_val
            if rem > 35:
                res += chr(rem + 29)
            else:
                res += "0123456789abcdefghijklmnopqrstuvwxyz"[rem]
            return res

        return _e(c_val)

    for i in range(c - 1, -1, -1):
        key = get_word(i, a)
        val = k_list[i] if i < len(k_list) and k_list[i] else key
        p = re.sub(r"\b" + re.escape(key) + r"\b", val, p)

    return p


def _decrypt_c0rst(c0rst_raw: str, key_idx: int | None = None) -> list[str]:
    try:
        c0rst_bytes = base64.b64decode(c0rst_raw)
    except Exception:
        return []
    candidate_indices = (
        [key_idx]
        if key_idx is not None and 0 <= key_idx < len(_XOR_KEYS)
        else list(range(len(_XOR_KEYS)))
    )

    for idx in candidate_indices:
        try:
            key_str = base64.b64decode(_XOR_KEYS[idx]).decode("latin1")
            xored = bytearray()
            for i in range(len(c0rst_bytes)):
                xored.append(c0rst_bytes[i] ^ ord(key_str[i % len(key_str)]))

            decrypted_json_str = base64.b64decode(xored).decode("utf-8")
            images = json.loads(decrypted_json_str)
            if isinstance(images, list) and images:
                return [str(img) for img in images]
        except Exception:
            continue

    if key_idx is not None:
        for idx in range(len(_XOR_KEYS)):
            if idx == key_idx:
                continue
            try:
                key_str = base64.b64decode(_XOR_KEYS[idx]).decode("latin1")
                xored = bytearray()
                for i in range(len(c0rst_bytes)):
                    xored.append(c0rst_bytes[i] ^ ord(key_str[i % len(key_str)]))

                decrypted_json_str = base64.b64decode(xored).decode("utf-8")
                images = json.loads(decrypted_json_str)
                if isinstance(images, list) and images:
                    return [str(img) for img in images]
            except Exception:
                continue

    return []


class DumanwuPlugin(SourcePlugin):
    id = "dumanwu"
    name = "读漫屋 (dumanwu)"
    category = "General"
    fetch_mode = FetchMode.HTTP

    @property
    def base_url(self) -> str:
        return self.settings.get("base_url", "https://www.dumanwu.com").rstrip("/")

    async def search(self, query: str) -> list[SearchResult]:
        ref = _clean_ref(query)
        is_url = query.startswith(("http://", "https://"))
        is_direct = is_url or (
            "/" not in query and len(query) >= 5 and re.match(r"^[A-Za-z0-9]+$", query)
        )

        if is_direct:
            try:
                url = f"{self.base_url}/{ref}/"
                html = await self.fetcher.get_text(url)

                name_m = re.search(
                    r'<meta[^>]+property=["\']og:novel:book_name["\'][^>]+content=["\']([^"\']+)["\']',
                    html,
                    re.I,
                ) or re.search(
                    r'<h1[^>]*class=["\'][^"\']*name_mh[^"\']*["\'][^>]*>(.*?)</h1>',
                    html,
                    re.I | re.S,
                )
                if name_m:
                    cover_m = re.search(
                        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                        html,
                        re.I,
                    ) or re.search(
                        r'<div[^>]+class=["\']mhcover["\'][^>]*>.*?<img[^>]+data-src=["\']([^"\']+)["\']',
                        html,
                        re.I | re.S,
                    )
                    title = _text(name_m.group(1))
                    if "_" in title:
                        title = title.split("_")[0]
                    cover = _html.unescape(cover_m.group(1)) if cover_m else None
                    return [SearchResult(source_id=self.id, title=title, ref=ref, cover_url=cover)]
                if is_url:
                    logger.info("dumanwu: no comic identity at %s; not fabricating a result", ref)
                    return []

            except Exception as err:
                logger.warning("dumanwu direct ref lookup failed for '%s': %s", ref, err)
                if is_url:
                    return []

        try:
            url = f"{self.base_url}/s"
            headers = {"Referer": f"{self.base_url}/"}
            if hasattr(self.fetcher, "post_text"):
                html = await self.fetcher.post_text(url, data={"k": query}, headers=headers)
            else:
                html = await self.fetcher.get_text(f"{self.base_url}/s?k={query}", headers=headers)
            return self._parse_search(html)
        except Exception as err:
            logger.warning("dumanwu search failed: %s", err)
            return []

    def _parse_search(self, html: str) -> list[SearchResult]:
        seen: set[str] = set()
        out: list[SearchResult] = []

        card_pattern = re.compile(
            r'<div[^>]+class=["\'][^"\']*itemnar[^"\']*["\'][^>]*>(.*?)</div>\s*</div>',
            re.I | re.S,
        )

        for match in card_pattern.finditer(html):
            inner = match.group(1)
            link_m = re.search(
                r'<a[^>]+href=["\'](?:https?://[^/"\']*)?/([A-Za-z0-9_-]+)/?["\']', inner, re.I
            )
            if not link_m:
                continue

            slug = link_m.group(1)
            if slug in seen:
                continue
            seen.add(slug)

            title_m = (
                re.search(
                    r'<p[^>]+class=["\']title["\'][^>]*>.*?<a[^>]*>(.*?)</a>', inner, re.I | re.S
                )
                or re.search(r'<img[^>]+alt=["\']([^"\']+)["\']', inner, re.I)
                or re.search(r'<img[^>]+title=["\']([^"\']+)["\']', inner, re.I)
            )

            cover_m = re.search(r'<img[^>]+data-src=["\']([^"\']+)["\']', inner, re.I) or re.search(
                r'<img[^>]+src=["\']([^"\']+)["\']', inner, re.I
            )

            title = _text(title_m.group(1)) if title_m else slug
            cover = _html.unescape(cover_m.group(1)) if cover_m else None
            if cover and "/static/images/" in cover:
                cover = None
            if cover and not cover.startswith("http"):
                cover = urljoin(self.base_url + "/", cover.lstrip("/"))

            out.append(
                SearchResult(
                    source_id=self.id,
                    title=title,
                    ref=slug,
                    cover_url=cover,
                )
            )

        logger.info("dumanwu: search parsed %d results", len(out))
        return out[:20]

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        url = f"{self.base_url}/{ref}/"
        html = await self.fetcher.get_text(url)
        return self._parse_detail(html, ref, result)

    def _parse_detail(self, html: str, ref: str, result: SearchResult | None = None) -> MangaDetail:
        title_m = re.search(
            r'<h1[^>]*class=["\'][^"\']*name_mh[^"\']*["\'][^>]*>(.*?)</h1>', html, re.I | re.S
        ) or re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I
        )
        title = _text(title_m.group(1)) if title_m else (result.title if result else ref)
        if "_" in title:
            title = title.split("_")[0]

        cover_m = (
            re.search(
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I
            )
            or re.search(
                r'<div[^>]+class=["\']mhcover["\'][^>]*>.*?<img[^>]+data-src=["\']([^"\']+)["\']',
                html,
                re.I | re.S,
            )
            or re.search(
                r'<div[^>]+class=["\']mhcover["\'][^>]*>.*?<img[^>]+src=["\']([^"\']+)["\']',
                html,
                re.I | re.S,
            )
        )
        cover = (
            _html.unescape(cover_m.group(1)) if cover_m else (result.cover_url if result else None)
        )
        if cover and "/static/images/" in cover:
            cover = None
        if cover and not cover.startswith("http"):
            cover = urljoin(self.base_url + "/", cover.lstrip("/"))

        author_m = re.search(
            r'<meta[^>]+property=["\']og:author["\'][^>]+content=["\']([^"\']+)["\']', html, re.I
        ) or re.search(r"<span>作\s*者[：:]\s*(.*?)</span>", html, re.I | re.S)
        author = (
            _text(author_m.group(1))
            if author_m and _text(author_m.group(1))
            else (result.author if result else None)
        )

        desc_m = re.search(
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
            html,
            re.I,
        ) or re.search(r'<p[^>]+class=["\']content["\'][^>]*>(.*?)</p>', html, re.I | re.S)
        description = _text(desc_m.group(1)) if desc_m else ""

        status: str | None = None
        status_m = re.search(r"<span>状\s*态[：:]\s*(.*?)</span>", html, re.I | re.S)
        if status_m:
            status = _text(status_m.group(1))

        return MangaDetail(
            source_id=self.id,
            title=title,
            ref=ref,
            cover_url=cover,
            author=author,
            description=description,
            status=status,
            extra={"detail_html": html},
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        ref = _clean_ref(detail.ref)
        html = detail.extra.get("detail_html")
        if not html:
            html = await self.fetcher.get_text(f"{self.base_url}/{ref}/")

        button_labels = ("开始阅读", "继续阅读", "阅读", "更多话点这里")

        raw_chapters: list[tuple[str, str]] = []
        seen_refs: set[str] = set()

        def _chapter_num(text: str) -> int | None:
            m = re.search(r"第\s*(\d+)\s*[話话]", text)
            if not m:
                m = re.match(r"\s*(\d{1,4})", text)
            return int(m.group(1)) if m else None

        ch_pattern = re.compile(
            r'<a[^>]+href=["\'](?:https?://[^/"\']*)?/[^/]+/([^/"\']+)\.html["\'][^>]*>(?:<li[^>]*>)?(.*?)(?:</li>)?</a>',
            re.I | re.S,
        )
        for match in ch_pattern.finditer(html):
            cid = match.group(1).strip()
            cname = _text(match.group(2))
            if not cname or cname in button_labels:
                continue
            if cid and cid not in seen_refs:
                seen_refs.add(cid)
                raw_chapters.append((cid, cname))

        try:
            more_url = f"{self.base_url}/morechapter"
            resp_data = None
            if hasattr(self.fetcher, "post_json"):
                resp_data = await self.fetcher.post_json(more_url, data={"id": ref})
            elif hasattr(self.fetcher, "post_text"):
                text_res = await self.fetcher.post_text(more_url, data={"id": ref})
                resp_data = json.loads(text_res)

            if (
                isinstance(resp_data, dict)
                and str(resp_data.get("code")) == "200"
                and "data" in resp_data
            ):
                for item in resp_data["data"]:
                    cid = str(item.get("chapterid", "")).strip()
                    cname = _text(str(item.get("chaptername", "")))
                    if cid and cid not in seen_refs:
                        seen_refs.add(cid)
                        raw_chapters.append((cid, cname))
        except Exception as err:
            logger.debug("dumanwu /morechapter fetch skipped or failed: %s", err)

        if not raw_chapters:
            return []

        numbered = [(cid, cname, _chapter_num(cname)) for cid, cname in raw_chapters]
        if all(num is not None for _cid, _cname, num in numbered):
            seen_nums: set[int] = set()
            ordered_tuples: list[tuple[str, str]] = []
            for cid, cname, num in sorted(numbered, key=lambda item: item[2] or 0):
                if num in seen_nums:
                    continue
                seen_nums.add(num)
                ordered_tuples.append((cid, cname))
        else:
            ordered_tuples = list(reversed(raw_chapters))

        chapters: list[Chapter] = []
        for idx, (cid, cname) in enumerate(ordered_tuples, start=1):
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=ref,
                    title=cname or f"Chapter {idx}",
                    ref=cid,
                    index=idx,
                    extra={"href": f"/{ref}/{cid}.html"},
                )
            )

        logger.info("dumanwu: listed %d chapters for %s", len(chapters), ref)
        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        manga_ref = _clean_ref(chapter.manga_ref)
        ch_ref = _clean_ref(chapter.ref).removesuffix(".html")
        href = chapter.extra.get("href") or f"/{manga_ref}/{ch_ref}.html"
        url = urljoin(self.base_url + "/", href.lstrip("/"))
        html = await self.fetcher.get_text(url)
        return self._parse_pages(html)

    def _parse_pages(self, html: str) -> list[Page]:
        unpacked_js = _unpack_dean_edwards(html)
        m = re.search(r'var __c0rst96\s*=\s*"([^"]+)"', unpacked_js)
        if not m:
            m = re.search(r'var __c0rst96\s*=\s*"([^"]+)"', html)
        if not m:
            raise ValueError("dumanwu: reader did not contain __c0rst96 payload")

        raw_c0rst = m.group(1)

        key_idx = None
        data_id_m = re.search(
            r'class="[^"]*readerContainer[^"]*"\s+data-id="(\d+)"', html
        ) or re.search(r'data-id="(\d+)"[^>]*class="[^"]*readerContainer[^"]*"', html)
        if data_id_m:
            key_idx = int(data_id_m.group(1))

        image_urls = _decrypt_c0rst(raw_c0rst, key_idx)
        if not image_urls:
            raise ValueError("dumanwu: failed to decrypt reader image URLs")

        pages: list[Page] = []
        for idx, img_url in enumerate(image_urls):
            pages.append(
                Page(
                    index=idx,
                    image_url=img_url,
                    referer=f"{self.base_url}/",
                )
            )

        logger.info("dumanwu: resolved %d pages", len(pages))
        return pages

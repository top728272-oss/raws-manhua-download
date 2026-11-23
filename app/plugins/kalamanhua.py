"""KalaManhua (kalamanhua.com) source plugin.

A MacCMS/stui manga aggregator sitting behind two separate gate systems, both
solved here without a browser:

1. **Main site** - any HTML page answers 403 with a 1.8 KB interstitial whose
   JS sets ``omo_token`` from a value embedded in the page itself
   (``k="<16 hex chars>"``). Sending that value back as the ``omo_token``
   cookie (httpx per-request cookies keep the two domains' cookies apart)
   unlocks every page. The token is near-static but parsed live.

2. **Image CDN** (``image.kalaimg.top``) - the reader page answers HTTP 200
   with a challenge containing ``var token='...'``; solved the same way and
   carried either as the ``omo_token`` cookie or a ``cf_verify`` query param.

Three site-level behaviors worth knowing:

- **Zero-hit search renders 6 recommendation thumbnails.** The miss page
  (``抱歉，没有找到相关漫画``) is visually identical to a hit page except for a
  heading, so results are only parsed when it is absent.
- **The chapter list is already in reading order** (oldest first) and includes
  special entries in upload order; the leading ``立即播放`` button shares the
  chapter (sid,nid) identity of chapter 1, so it is dropped by caption before
  any dedupe.
- **Cover URLs carry a trailing space inside the attribute.** Unstripped, the
  CDN answers HTTP 555 ``读取失败``. Covers need NO token - only the site
  Referer; page images need BOTH a Referer and the ``cf_verify`` token.

Etiquette matters here: the CDN zone runs a real Cloudflare WAF that only
blocks tokenless/refererless bursts, so every request carries the token (or
Referer) and plugin-made HTML fetches keep >= 1 s spacing.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import random
import re
import time
from urllib.parse import quote

import httpx

from app.core.htmlutil import cut_recommendations, strip_chrome
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)


_MAIN_CHALLENGE_MARK = "安全验证"
_CDN_CHALLENGE_MARK = "加载中"
_MAIN_TOKEN_COOKIE = "omo_token"
_RE_MAIN_TOKEN = re.compile(r'\bk\s*=\s*"([0-9a-fA-F]{8,64})"')
_RE_CDN_TOKEN = re.compile(r"var\s+token\s*=\s*'([A-Za-z0-9_\-]+)'")
_RE_CF_BLOCK = re.compile(r"you have been blocked|Attention Required", re.I)


_RE_SEARCH_TAG = re.compile(r"<a[^>]+class=\"[^\"]*stui-vodlist__thumb[^\"]*\"[^>]*>", re.I | re.S)
_RE_HREF = re.compile(r"href=\"([^\"]+)\"", re.I)
_RE_TITLE_ATTR = re.compile(r"title=\"([^\"]*)\"", re.I)
_RE_COMIC_ID = re.compile(r"/comic/(\d+)\.html", re.I)
_SEARCH_MISS_MARK = "没有找到相关漫画"
_RE_PAGER = re.compile(r'class="active num[^"]*"><a>\s*(\d+)\s*/\s*(\d+)\s*</a>')

_RE_TITLE = re.compile(r"<h1[^>]*class=\"[^\"]*title[^\"]*\"[^>]*>(.*?)</h1>", re.I | re.S)
_RE_DATA_ROW = re.compile(r"<p class=\"data\">%s：(.*?)</p>", re.S)
_RE_GENRES_ROW = re.compile(r"<p class=\"data\">类型：(.*?)</p>", re.S)
_RE_DESCRIPTION = re.compile(r"<span class=\"detail-content\"[^>]*>(.*?)</span>", re.S)


_RE_COVER = re.compile(r"stui-content__thumb.*?data-original=\"([^\"]+)\"", re.I | re.S)
_RE_PAGE_TITLE = re.compile(r"<title>([^<]+)</title>", re.I)

_RE_CHAPTER_LINK = re.compile(
    r"<a[^>]+href=\"(?:https?://[^\"]*?)?/chapter/(\d+)-(\d+)-(\d+)\.html\"[^>]*>(.*?)</a>",
    re.I | re.S,
)
_RE_TAG = re.compile(r"<[^>]+>")


_RE_PLAYER_AAAA = re.compile(r"var\s+player_aaaa\s*=\s*(\{.*?\});", re.S)
_RE_HEX = re.compile(r"[0-9a-f]+")
_RE_DATA_SRC = re.compile(r"data-src=\"([^\"]+)\"")
_READER_PAGE_MARK = "漫画阅读器"

_TAG_STRIP = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG_STRIP.sub("", fragment))).strip()


def _attr(tag: str, name: str) -> str | None:
    match = re.search(rf"{name}=\"([^\"]*)\"", tag, re.I)
    return _html.unescape(match.group(1)) if match else None


def _comic_id_of(value: str) -> int | None:
    """Comic id from a bare id, a comic URL path, or a chapter URL on that comic."""
    match = _RE_COMIC_ID.search(value.strip())
    if match:
        return int(match.group(1))
    chapter_match = re.search(r"/chapter/(\d+)-\d+-\d+", value.strip())
    if chapter_match:
        return int(chapter_match.group(1))
    if re.fullmatch(r"\d+", value.strip()):
        return int(value.strip())
    return None


class KalaManhuaPlugin(SourcePlugin):
    id = "kalamanhua"
    name = "卡拉漫画 (kalamanhua)"
    category = "General"
    fetch_mode = FetchMode.HTTP

    def __init__(self, cfg, fetcher) -> None:
        super().__init__(cfg, fetcher)

        override = self.settings.get("request_delay_ms")
        if override and hasattr(self.fetcher, "_delay"):
            self.fetcher._delay = max(self.fetcher._delay, float(override) / 1000.0)

        self._max_gate_attempts = 4

        self._html_interval = 1.0
        self._pace_lock = asyncio.Lock()
        self._last_html = 0.0

        self._main_token: str | None = None
        self._cdn_token: str | None = None

    @property
    def base_url(self) -> str:
        return str(self.settings.get("base_url", "https://www.kalamanhua.com")).rstrip("/")

    @property
    def cdn_url(self) -> str:
        return str(self.settings.get("cdn_url", "https://image.kalaimg.top")).rstrip("/")

    @property
    def referer(self) -> str:
        return f"{self.base_url}/"

    async def _pace_html(self) -> None:
        async with self._pace_lock:
            wait = self._html_interval - (time.monotonic() - self._last_html)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_html = time.monotonic()

    async def _backoff(self, attempt: int, what: str) -> None:
        seconds = min(10 * (attempt + 1) + random.uniform(0, 5), 35)
        logger.warning("kalamanhua: Cloudflare block on %s, backing off %.1fs", what, seconds)
        await asyncio.sleep(seconds)

    async def _main_html(self, url: str) -> str:
        """Fetch a main-site page, solving the omo_token gate when it appears."""
        for attempt in range(self._max_gate_attempts):
            await self._pace_html()
            try:
                kwargs: dict = {}
                if self._main_token:
                    kwargs["cookies"] = {_MAIN_TOKEN_COOKIE: self._main_token}
                return await self.fetcher.get_text(url, **kwargs)
            except httpx.HTTPStatusError as exc:
                body = exc.response.text if exc.response is not None else ""
                if _RE_CF_BLOCK.search(body):
                    await self._backoff(attempt, url)
                    continue
                if exc.response is not None and exc.response.status_code == 403:
                    match = _RE_MAIN_TOKEN.search(body)
                    if match:
                        self._main_token = match.group(1)
                        logger.info("kalamanhua: main gate solved (token %s)", self._main_token)
                        continue
                    if _MAIN_CHALLENGE_MARK in body:
                        raise RuntimeError(f"kalamanhua: challenge page without token: {url}")
                raise
        raise RuntimeError(f"kalamanhua: main gate still failing after retries: {url}")

    async def _reader_html(self, credential: str) -> str:
        """Fetch the CDN reader page, solving the cf_verify gate when it appears."""
        base = f"{self.cdn_url}/play/min/{credential}"
        for attempt in range(self._max_gate_attempts):
            await self._pace_html()
            url = base + (f"?cf_verify={self._cdn_token}" if self._cdn_token else "")
            try:
                text = await self.fetcher.get_text(url, headers={"Referer": self.referer})
            except httpx.HTTPStatusError as exc:
                body = exc.response.text if exc.response is not None else ""
                if _RE_CF_BLOCK.search(body) or (
                    exc.response is not None and exc.response.status_code in (403, 503)
                ):
                    await self._backoff(attempt, url)
                    continue
                raise
            if _CDN_CHALLENGE_MARK in text:
                match = _RE_CDN_TOKEN.search(text)
                if not match:
                    raise RuntimeError(f"kalamanhua: CDN challenge without token: {credential}")
                self._cdn_token = match.group(1)
                logger.info("kalamanhua: CDN gate solved (token %s)", self._cdn_token)
                continue
            if _RE_CF_BLOCK.search(text):
                await self._backoff(attempt, url)
                continue
            return text
        raise RuntimeError(f"kalamanhua: CDN gate still failing after retries: {credential}")

    async def search(self, query: str) -> list[SearchResult]:
        query = query.strip()
        if not query:
            return []

        comic_id = _comic_id_of(query)
        if comic_id is not None:
            result = await self._result_from_id(comic_id)
            return [result] if result else []

        pages = max(1, int(self.settings.get("search_pages", 2)))
        keyword = quote(query, safe="")
        results: list[SearchResult] = []
        seen: set[int] = set()
        for page in range(1, pages + 1):
            url = (
                f"{self.base_url}/search/wd/{keyword}.html"
                if page == 1
                else f"{self.base_url}/search/wd/{keyword}/page/{page}.html"
            )
            try:
                html_page = await self._main_html(url)
            except Exception as err:
                logger.warning("kalamanhua: search page %d failed for '%s': %s", page, query, err)
                break
            scope = strip_chrome(cut_recommendations(html_page))
            if _SEARCH_MISS_MARK in scope:
                break
            fresh = self._parse_search_page(scope)
            new = [r for r in fresh if r.ref not in seen]
            if not new:
                break
            for result in new:
                seen.add(int(result.ref))
                results.append(result)
            logger.info("kalamanhua: search page %d for '%s' -> %d", page, query, len(fresh))
            match = _RE_PAGER.search(scope)
            if not match or page >= int(match.group(2)):
                break
        logger.info("kalamanhua: search for '%s' -> %d results", query, len(results))
        return results

    def _parse_search_page(self, scope: str) -> list[SearchResult]:
        out: list[SearchResult] = []
        for tag in _RE_SEARCH_TAG.findall(scope):
            href = _attr(tag, "href") or ""
            match = _RE_COMIC_ID.search(href)
            if not match:
                continue
            cover = (_attr(tag, "data-original") or "").strip() or None
            title = _text(_attr(tag, "title") or "")
            out.append(
                SearchResult(
                    source_id=self.id,
                    title=title or match.group(1),
                    ref=match.group(1),
                    cover_url=cover,
                )
            )
        return out

    async def _result_from_id(self, comic_id: int) -> SearchResult | None:
        try:
            detail = await self.get_detail(
                SearchResult(source_id=self.id, title=str(comic_id), ref=str(comic_id))
            )
        except Exception as err:
            logger.warning("kalamanhua: ref lookup failed for %s: %s", comic_id, err)
            return None
        return SearchResult(
            source_id=self.id,
            title=detail.title,
            ref=detail.ref,
            cover_url=detail.cover_url,
            author=detail.author,
        )

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        comic_id = _comic_id_of(result.ref) or _comic_id_of(result.title)
        if comic_id is None:
            raise RuntimeError(f"kalamanhua: cannot resolve ref {result.ref!r}")
        html_page = await self._main_html(f"{self.base_url}/comic/{comic_id}.html")
        return self._parse_detail(html_page, comic_id, result)

    def _parse_detail(self, html_page: str, comic_id: int, result: SearchResult) -> MangaDetail:
        title_match = _RE_TITLE.search(html_page)
        title = _text(title_match.group(1)) if title_match else ""
        if not title:
            page_title = _RE_PAGE_TITLE.search(html_page)
            title = _text(page_title.group(1)).split("_")[0] if page_title else ""

        def data_row(label: str) -> str:
            match = re.search(rf"<p class=\"data\">{label}：(.*?)</p>", html_page, re.S)
            return _text(match.group(1)) if match else ""

        cover_match = _RE_COVER.search(html_page)
        cover = cover_match.group(1).strip() if cover_match else (result.cover_url or "")
        tags: list[str] = []
        genres_match = _RE_GENRES_ROW.search(html_page)
        if genres_match:
            tags = [
                t
                for t in (_text(x) for x in re.findall(r">([^<>]+)</a>", genres_match.group(1)))
                if t
            ]

        description_match = _RE_DESCRIPTION.search(html_page)
        return MangaDetail(
            source_id=self.id,
            title=title or result.title or str(comic_id),
            ref=str(comic_id),
            cover_url=cover or None,
            author=data_row("作者") or None,
            description=_text(description_match.group(1)) if description_match else None,
            status=data_row("状态") or None,
            tags=tags,
            extra={"detail_html": html_page},
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        comic_id = int(detail.ref)
        html_page = detail.extra.get("detail_html")
        if not html_page:
            html_page = await self._main_html(f"{self.base_url}/comic/{comic_id}.html")
        return self._parse_chapters(html_page, comic_id)

    def _parse_chapters(self, html_page: str, comic_id: int) -> list[Chapter]:
        include_all_lines = str(self.settings.get("source_lines", "first")).lower() in {
            "all",
            "true",
            "1",
        }

        ordered: list[Chapter] = []
        seen: set[tuple[int, int]] = set()
        line_order: list[int] = []
        skipped_lines: set[int] = set()

        for _cid, sid, nid, name_html in _RE_CHAPTER_LINK.findall(html_page):
            key = (int(sid), int(nid))

            if _text(name_html) == "立即播放":
                continue
            if key in seen:
                continue
            seen.add(key)
            if key[0] not in line_order:
                line_order.append(key[0])
            if not include_all_lines and key[0] != line_order[0]:
                skipped_lines.add(key[0])
                continue
            ordered.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=str(comic_id),
                    title=_text(name_html),
                    ref=f"{self.base_url}/chapter/{comic_id}-{key[0]}-{key[1]}.html",
                    index=len(ordered) + 1,
                    group=f"line{key[0]}" if include_all_lines else "default",
                )
            )

        if skipped_lines:
            logger.info(
                "kalamanhua: comic %s has %d extra source line(s) (%s) hidden by default "
                "(source_lines: 'all' to include)",
                comic_id,
                len(skipped_lines),
                ",".join(str(s) for s in sorted(skipped_lines)),
            )
        logger.info("kalamanhua: comic %s -> %d chapters", comic_id, len(ordered))
        return ordered

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        html_page = await self._main_html(chapter.ref)
        match = _RE_PLAYER_AAAA.search(html_page)
        if not match:
            raise RuntimeError(f"kalamanhua: no player_aaaa in {chapter.ref}")
        try:
            player = json.loads(match.group(1))
        except json.JSONDecodeError as err:
            raise RuntimeError(f"kalamanhua: bad player_aaaa JSON in {chapter.ref}") from err
        credential = player.get("url") or ""
        if not _RE_HEX.fullmatch(credential):
            raise RuntimeError(
                f"kalamanhua: unexpected credential in {chapter.ref}: {credential!r}"
            )

        reader = await self._reader_html(credential)
        pages = self._parse_reader(reader)
        if not pages:
            raise RuntimeError(f"kalamanhua: reader page contained no images for {chapter.ref}")
        logger.info("kalamanhua: %d pages for '%s'", len(pages), chapter.title)
        return pages

    def _parse_reader(self, reader_html: str) -> list[Page]:
        if _READER_PAGE_MARK not in reader_html and "data-src" not in reader_html:
            raise RuntimeError(f"kalamanhua: not a reader page: {reader_html[:120]!r}")

        pages: list[Page] = []
        for raw in _RE_DATA_SRC.findall(reader_html):
            url = _html.unescape(raw.strip())
            if url.startswith("//"):
                url = f"https:{url}"
            elif url.startswith("/"):
                url = self.cdn_url + url
            if not url.startswith("http"):
                continue

            if "cf_verify=" not in url and self._cdn_token:
                url += ("&" if "?" in url else "?") + f"cf_verify={self._cdn_token}"
            pages.append(Page(index=len(pages), image_url=url, referer=self.referer))
        return pages

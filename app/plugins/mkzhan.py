"""Mkzhan (漫客栈, www.mkzhan.com) source plugin.

All comic data comes from a plain JSON API on comic.mkzcdn.com - no encryption,
no request signing, no cookies needed for free chapters. Chapter image URLs are
server-signed (Aliyun-OSS style auth_key), short-lived and path-bound, so they
are always resolved fresh per chapter by the download engine.

Endpoint map (all GET, envelope {"code":"200", ...}):
  /comic/info/?comic_id=<id>            comic metadata
  /chapter/v1/?comic_id=<id>            chapter list (API array order = reading order)
  /chapter/content/v1/?chapter_id=&comic_id=&format=1&quality=<1|2|3>&type=1
                                        page image list (format=1 -> webp)
  /search/keyword/?keyword=<q>&page_num=1&page_size=20
                                        keyword search

Error codes: 302 -> login required; 102/103 -> paywall; 199 -> early-access;
101 -> invalid quality. Non-200 codes raise ValueError (the download engine
marks that chapter failed and continues the queue).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

API_BASE_DEFAULT = "https://comic.mkzcdn.com"
SITE_BASE_DEFAULT = "https://www.mkzhan.com"
_REFERER_PATH = "/"
_VALID_QUALITY = (1, 2, 3)
FINISH_LABELS = {"1": "连载中", "2": "完结"}


def _clean_ref(query: str) -> str:
    """Extract a comic_id from a URL, an API URL, a bare id, or pass through a keyword."""
    query = query.strip()
    if (
        query.startswith("http://")
        or query.startswith("https://")
        or "/" in query
        or "comic_id=" in query
    ):
        parsed = urlparse(query)
        qs = parse_qs(parsed.query)
        if "comic_id" in qs and qs["comic_id"]:
            return qs["comic_id"][0].strip()
        path = parsed.path.strip("/")
        parts = path.split("/")
        if parts and parts[0]:
            first = parts[0]
            if first.endswith(".html"):
                first = first[: -len(".html")]
            return first
    if query.endswith(".html"):
        query = query[: -len(".html")]
    return query


def _httpify(url: str | None) -> str | None:
    """The API returns http:// image/cover URLs; the site rewrites them to https."""
    if url and url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def _check_code(data: Any) -> dict[str, Any]:
    """Validate the mkzhan response envelope and raise on business-error codes."""
    if not isinstance(data, dict):
        raise ValueError(f"mkzhan: unexpected non-object API response: {type(data).__name__}")
    code = str(data.get("code", ""))
    message = str(data.get("message", ""))
    if code == "200":
        return data
    if code == "302":
        raise ValueError("mkzhan: login required (code 302)")
    if code in ("102", "103"):
        raise ValueError("mkzhan: chapter is paywalled/VIP (code 102/103)")
    if code == "199":
        raise ValueError("mkzhan: chapter requires the app / early-access (code 199)")
    if code == "101":
        raise ValueError(f"mkzhan: invalid request parameter (code 101): {message}")
    raise ValueError(f"mkzhan: unexpected API code {code!r}: {message}")


class MkzhanPlugin(SourcePlugin):
    id = "mkzhan"
    name = "漫客栈 (mkzhan)"
    category = "General"
    fetch_mode = FetchMode.HTTP

    @property
    def api_base(self) -> str:
        return str(self.settings.get("api_base", API_BASE_DEFAULT)).rstrip("/")

    @property
    def base_url(self) -> str:
        return str(self.settings.get("base_url", SITE_BASE_DEFAULT)).rstrip("/")

    @property
    def quality(self) -> int:
        """Image width: 1=800px, 2=1000px, 3=1200px. Anything else falls back to 3."""
        try:
            q = int(self.settings.get("quality", 3))
        except (TypeError, ValueError):
            return 3
        return q if q in _VALID_QUALITY else 3

    @property
    def include_vip(self) -> bool:
        return bool(self.settings.get("include_vip", False))

    async def search(self, query: str) -> list[SearchResult]:
        ref = _clean_ref(query)
        if query.startswith("http://") or query.startswith("https://") or ref.isdigit():
            try:
                data = _check_code(
                    await self.fetcher.get_json(f"{self.api_base}/comic/info/?comic_id={ref}")
                )
                info = data.get("data") or {}
                return [
                    SearchResult(
                        source_id=self.id,
                        title=info.get("title") or ref,
                        ref=ref,
                        cover_url=_httpify(info.get("cover")),
                    )
                ]
            except Exception as err:
                logger.warning("mkzhan ref search failed for '%s': %s", ref, err)
                return []

        try:
            url = f"{self.api_base}/search/keyword/?keyword={quote(query)}&page_num=1&page_size=20"
            raw = await self.fetcher.get_json(url)
            code = str(raw.get("code", "")) if isinstance(raw, dict) else ""
            if code == "199":
                logger.info("mkzhan: search '%s' has no results (code 199)", query)
                return []
            data = _check_code(raw)
            results = self._parse_search(data.get("data"))
            logger.info("mkzhan: search parsed %d results", len(results))
            return results
        except Exception as err:
            logger.warning("mkzhan search failed for '%s': %s", query, err)
            return []

    def _parse_search(self, data: Any) -> list[SearchResult]:
        """Parse search results with a defensive fallback over response shapes."""
        items: Any = []
        if isinstance(data, dict):
            items = data.get("list")
            if items is None:
                inner = data.get("data")
                if isinstance(inner, dict):
                    items = inner.get("list")
                else:
                    items = inner
        elif isinstance(data, list):
            items = data
        if not isinstance(items, list):
            items = []

        out: list[SearchResult] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            cid = str(it.get("comic_id") or it.get("id") or "").strip()
            if not cid:
                continue
            title = str(it.get("title") or it.get("name") or cid).strip()
            author = it.get("author_title") or it.get("author")
            author = str(author).strip() if isinstance(author, str) and author.strip() else None
            out.append(
                SearchResult(
                    source_id=self.id,
                    title=title,
                    ref=cid,
                    cover_url=_httpify(it.get("cover") or it.get("pic")),
                    author=author,
                )
            )
        return out

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        data = _check_code(
            await self.fetcher.get_json(f"{self.api_base}/comic/info/?comic_id={ref}")
        )
        info = data.get("data") or {}
        chapter_count: int | None = None
        raw_count = info.get("chapter_number")
        if isinstance(raw_count, (int, float)):
            chapter_count = int(raw_count)
        elif isinstance(raw_count, str) and raw_count.isdigit():
            chapter_count = int(raw_count)
        return MangaDetail(
            source_id=self.id,
            title=info.get("title") or result.title or ref,
            ref=ref,
            cover_url=_httpify(info.get("cover")) or result.cover_url,
            author=result.author,
            description=info.get("content"),
            status=FINISH_LABELS.get(str(info.get("finish"))),
            chapter_count=chapter_count,
            extra={
                "comic_id": ref,
                "finish": info.get("finish"),
                "is_vip": info.get("is_vip"),
                "new_chapter_id": info.get("new_chapter_id"),
            },
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        ref = _clean_ref(detail.ref)
        data = _check_code(
            await self.fetcher.get_json(f"{self.api_base}/chapter/v1/?comic_id={ref}")
        )
        raw = data.get("data")
        if isinstance(raw, dict):
            raw = raw.get("list")
        if not isinstance(raw, list):
            raw = []

        chapters: list[Chapter] = []
        skipped_vip = 0

        for item in raw:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("chapter_id") or "").strip()
            if not cid:
                continue
            is_vip = str(item.get("is_vip", "0")) == "1"
            if is_vip and not self.include_vip:
                skipped_vip += 1
                continue
            title = str(item.get("title") or f"第{item.get('number')}话").strip()
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=ref,
                    title=title,
                    ref=cid,
                    index=len(chapters) + 1,
                    extra={
                        "comic_id": ref,
                        "chapter_id": cid,
                        "number": item.get("number"),
                        "part": item.get("part"),
                        "is_vip": item.get("is_vip"),
                        "start_time": item.get("start_time"),
                    },
                )
            )
        if skipped_vip:
            logger.info("mkzhan: skipped %d VIP chapter(s) for comic %s", skipped_vip, ref)
        logger.info("mkzhan: parsed %d chapters for comic %s", len(chapters), ref)
        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        comic_id = (chapter.extra or {}).get("comic_id") or chapter.manga_ref
        cid = (chapter.extra or {}).get("chapter_id") or chapter.ref
        url = (
            f"{self.api_base}/chapter/content/v1/"
            f"?chapter_id={cid}&comic_id={comic_id}"
            f"&format=1&quality={self.quality}&type=1"
        )
        data = _check_code(await self.fetcher.get_json(url))
        payload = data.get("data") or {}
        pages_raw = payload.get("page") or [] if isinstance(payload, dict) else []

        pages: list[Page] = []
        for item in pages_raw:
            if not isinstance(item, dict):
                continue
            image = _httpify(item.get("image"))
            if not image:
                continue
            pages.append(
                Page(
                    index=len(pages),
                    image_url=image,
                    referer=f"{self.base_url}{_REFERER_PATH}",
                )
            )
        logger.info("mkzhan: resolved %d pages for chapter %s", len(pages), cid)
        return pages

"""ManhuaFree / CloudMe source plugin."""

from __future__ import annotations

import html as _html
import logging
import re
from urllib.parse import quote, urlparse

from app.core.decoder import decode_images
from app.core.htmlutil import cut_recommendations, strip_chrome
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

_DATA_MID = re.compile(r'data-mid=["\'](\d+)["\']', re.I)
_ALT = re.compile(r'alt=["\']([^"\']+)["\']', re.I)
_SRC = re.compile(r'src=["\']([^"\']+)["\']', re.I)
_TAG = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG.sub("", fragment))).strip()


def _clean_ref(query: str) -> str:
    query = query.strip()
    if query.startswith("http://") or query.startswith("https://"):
        parsed = urlparse(query)
        path = parsed.path.strip("/")
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in ("manga", "refs"):
            return parts[1]
        return parts[-1]
    return query


class ManhuaFreePlugin(SourcePlugin):
    id = "manhuafree"
    name = "ManhuaFree (CloudMe)"
    fetch_mode = FetchMode.HTTP

    @property
    def base_url(self) -> str:
        return self.settings.get("base_url", "https://manhuafree.com").rstrip("/")

    @property
    def api_host(self) -> str:
        return self.settings.get("api_host", "https://v2.apikk.top").rstrip("/")

    @property
    def default_cdn(self) -> str:
        return self.settings.get("default_cdn", "https://c-nd2-1.6wm.top").rstrip("/")

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
                logger.warning("manhuafree direct ref search failed: %s", err)
                return []

        try:
            url = f"{self.base_url}/s/{quote(query)}"
            html = await self.fetcher.get_text(url)
            return self._parse_search(html)
        except Exception as err:
            logger.warning("manhuafree search URL failed: %s", err)
            return []

    def _parse_search(self, html: str) -> list[SearchResult]:
        seen: set[str] = set()
        out: list[SearchResult] = []
        html = cut_recommendations(html)
        html = strip_chrome(html)
        pattern = re.compile(
            r'<a[^>]+href="/(?:manga|refs)/([^"?/#]+)"[^>]*>(.*?)</a>', re.I | re.S
        )
        for match in pattern.finditer(html):
            slug = match.group(1)
            inner = match.group(2)
            if slug in seen:
                continue
            seen.add(slug)
            alt = _ALT.search(inner)
            src = _SRC.search(inner)
            text_clean = _text(inner)
            title = text_clean if text_clean else (_text(alt.group(1)) if alt else "")
            if not title or title == slug:
                continue
            cover = _html.unescape(src.group(1)) if src else None
            out.append(
                SearchResult(
                    source_id=self.id,
                    title=title,
                    ref=slug,
                    cover_url=cover,
                )
            )
        logger.info("manhuafree: search parsed %d results", len(out))
        return out[:20]

    async def _resolve_mid(self, ref: str) -> str:
        clean = _clean_ref(ref)
        if clean.isdigit():
            return clean

        url = f"{self.base_url}/manga/{clean}"
        html = await self.fetcher.get_text(url)
        mids = _DATA_MID.findall(html)
        if mids:
            return mids[0]

        m = re.search(r'mid[:=]\s*["\']?(\d+)["\']?', html)
        if m:
            return m.group(1)

        raise ValueError(f"Could not resolve MID for ref '{ref}'")

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        mid = await self._resolve_mid(result.ref)
        data = await self.fetcher.get_json(f"{self.api_host}/api/v2/manga/get?mid={mid}&mode=all")
        manga = data.get("data", {})

        title = manga.get("title") or result.title
        cover = manga.get("cover") or result.cover_url
        desc = manga.get("desc")
        status = str(manga.get("status", ""))
        chapters_raw = manga.get("chapters", [])

        return MangaDetail(
            source_id=self.id,
            title=title,
            ref=mid,
            cover_url=cover,
            description=desc,
            status=status,
            chapter_count=len(chapters_raw),
            extra={"mid": mid, "chapters_raw": chapters_raw},
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        chapters_raw = detail.extra.get("chapters_raw", [])
        if not chapters_raw:
            mid = detail.ref
            data = await self.fetcher.get_json(
                f"{self.api_host}/api/v2/manga/get?mid={mid}&mode=all"
            )
            chapters_raw = data.get("data", {}).get("chapters", [])

        chapters: list[Chapter] = []
        for idx, c in enumerate(chapters_raw):
            cid = str(c["id"])
            attrs = c.get("attributes", {})
            ctitle = attrs.get("title", f"Chapter {idx + 1}")

            order = attrs.get("order")
            sort_order = order if isinstance(order, (int, float)) else float(idx)
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=detail.ref,
                    title=ctitle,
                    ref=cid,
                    index=idx + 1,
                    extra={"mid": detail.ref, "chapter_id": cid, "order": sort_order},
                )
            )

        chapters.sort(key=lambda ch: ch.extra["order"])
        for position, ch in enumerate(chapters, start=1):
            ch.index = position
        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        mid = chapter.extra.get("mid") or chapter.manga_ref
        cid = chapter.ref
        data = await self.fetcher.get_json(
            f"{self.api_host}/api/v2/chapter/getinfo?m={mid}&c={cid}"
        )
        info = data.get("data", {}).get("info", {})
        raw_payload = info.get("images", {}).get("images", "")

        decoded_items = decode_images(raw_payload)

        def _order(pair: tuple[int, object]) -> float:
            position, item = pair
            if isinstance(item, dict):
                value = item.get("order", position)
                if isinstance(value, (int, float)):
                    return float(value)
            return float(position)

        ordered = sorted(enumerate(decoded_items), key=_order)

        pages: list[Page] = []
        for item in (item for _position, item in ordered):
            rel_url = item.get("url", "") if isinstance(item, dict) else str(item)
            if not rel_url:
                continue
            full_url = rel_url if rel_url.startswith("http") else f"{self.default_cdn}{rel_url}"
            pages.append(
                Page(
                    index=len(pages),
                    image_url=full_url,
                    referer=f"{self.base_url}/",
                )
            )

        return pages

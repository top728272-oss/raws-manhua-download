"""W漫画 (wmanhua.com) source plugin.

Chapter order
-------------
Both chapter sources on this site list the **newest chapter first**:

* ``POST /comic/{id}`` -> ``data.chapters[]``, sorted by chapter ``id``
  **descending** (verified across 16 comics: ids are strictly monotonic, so
  ascending id IS reading order). ``chapterNum`` is a page count, not a
  chapter number, and ``createdAt`` is identical for every row - neither can be
  used to order the list.
* the detail page's ``/chapter/{id}-{cid}.html`` links (the HTML fallback) list
  the latest 25 the same way.

Reading order is therefore the same sequence **ascending by chapter id**, which
is what :func:`_in_reading_order` applies. Sorting (rather than reversing) keeps
the result correct if the site ever flips its array back to ascending, and it
tolerates rows with a missing/non-numeric id. Do NOT re-sort by the number in
the title: several series interleave bonus chapters whose numbers are not
monotonic (``第8话`` then ``3.5``, ``番外`` between ``45`` and ``44``).
"""

from __future__ import annotations

import html as _html
import logging
import re
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from app.core.htmlutil import is_placeholder_cover, strip_chrome
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG.sub("", fragment))).strip()


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ref_chapter_id(ref: str) -> int | None:
    """``1969-160013`` -> ``160013``, the site's own ascending chapter id."""
    return _as_int(ref.rpartition("-")[2])


def _in_reading_order(raw_chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort the API's newest-first chapter array into reading order.

    The site returns ``id`` descending; reading order is the same rows
    ascending. Sorting (not reversing) is idempotent, so this stays correct if
    the API ever hands the array back ascending. Rows without a numeric id keep
    their relative order and land at the end.
    """

    def key(item: dict[str, Any]) -> tuple[bool, int]:
        chapter_id = _as_int(item.get("id"))
        return (chapter_id is None, chapter_id or 0)

    return sorted(raw_chapters, key=key)


def _clean_ref(query: str) -> str:
    query = query.strip()
    if query.startswith("http://") or query.startswith("https://") or query.startswith("/"):
        parsed = urlparse(query)
        path = parsed.path.strip("/")
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in ("comic", "chapter", "search"):
            file_part = parts[1]
            if file_part.endswith(".html"):
                file_part = file_part[:-5]
            return file_part
        last = parts[-1]
        if last.endswith(".html"):
            last = last[:-5]
        return last
    if query.endswith(".html"):
        query = query[:-5]
    return query


_JS_ALT = re.compile(r"\(\s*(['\"])(.*?)\1\s*\|\|\s*(['\"])(.*?)\3\s*\)")
_JS_STRING = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_JS_ESCAPE = re.compile(r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|(.))")


def _js_unescape(s: str) -> str:
    def repl(m: re.Match[str]) -> str:
        if m.group(1):
            return chr(int(m.group(1), 16))
        if m.group(2):
            return chr(int(m.group(2), 16))
        return {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(3), m.group(3))

    return _JS_ESCAPE.sub(repl, s)


def _js_concat_base(expr: str) -> str | None:
    """Evaluate a plain JS string concatenation: 'base/' + ('' || 'title') + '/'.

    The `||` picks the first truthy operand: newer chapters put a UUID
    directory on the left (the CDN 404s the title path for those), older ones
    leave `''` and the chapter title on the right applies. Returns None when
    the expression mixes in anything that is not a string literal.
    """

    alt = _JS_ALT.search(expr)
    if alt:
        chosen = alt.group(2) or alt.group(4)
        expr = expr[: alt.start()] + "'" + chosen + "'" + expr[alt.end() :]
    if re.search(r"[^\s+]", _JS_STRING.sub("", expr)):
        return None
    parts = [_js_unescape(m.group(0)[1:-1]) for m in _JS_STRING.finditer(expr)]
    joined = "".join(parts)
    return joined if joined.startswith("http") else None


class WManhuaPlugin(SourcePlugin):
    id = "wmanhua"
    name = "W漫画 (wmanhua)"
    category = "General"
    fetch_mode = FetchMode.HTTP

    @property
    def base_url(self) -> str:
        return self.settings.get("base_url", "https://www.wmanhua.com").rstrip("/")

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
                title_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
                cover_m = re.search(r'<img[^>]+data-src=["\']([^"\']+)["\']', html, re.I)
                title = _text(title_m.group(1)) if title_m else ref
                cover = _html.unescape(cover_m.group(1)) if cover_m else None
                return [SearchResult(source_id=self.id, title=title, ref=ref, cover_url=cover)]
            except Exception:
                return []

        try:
            url = f"{self.base_url}/search?query={quote(query)}"
            html = await self.fetcher.get_text(url)
            return self._parse_search(html)
        except Exception as err:
            logger.warning("wmanhua search URL failed: %s", err)
            return []

    def _parse_search(self, html: str) -> list[SearchResult]:
        seen: set[str] = set()
        out: list[SearchResult] = []
        html = strip_chrome(html)

        card_pattern = re.compile(
            r'<a[^>]+href=["\'](?:https?://[^/"\']*)?/comic/(\d+)\.html["\'][^>]*>(.*?)</a>',
            re.I | re.S,
        )

        for match in card_pattern.finditer(html):
            slug = match.group(1)
            inner = match.group(2)
            if slug in seen:
                continue
            seen.add(slug)

            alt_m = re.search(r'alt=["\']([^"\']+)["\']', inner, re.I)

            bg_m = re.search(r"background-image:\s*url\(['\"]?([^'\")]+)['\"]?\)", inner, re.I)
            src_m = re.search(r'data-src=["\']([^"\']+)["\']', inner, re.I) or re.search(
                r'src=["\']([^"\']+)["\']', inner, re.I
            )
            title_m = re.search(r"<h3[^>]*>(.*?)</h3>", inner, re.I | re.S)

            title = (
                _text(title_m.group(1)) if title_m else (_text(alt_m.group(1)) if alt_m else slug)
            )
            cover = (
                _html.unescape(bg_m.group(1))
                if bg_m
                else (_html.unescape(src_m.group(1)) if src_m else None)
            )

            if cover and not cover.startswith("http"):
                cover = f"{self.base_url}{cover}"
            if is_placeholder_cover(cover):
                cover = None

            out.append(
                SearchResult(
                    source_id=self.id,
                    title=title,
                    ref=slug,
                    cover_url=cover,
                )
            )

        logger.info("wmanhua: search parsed %d results", len(out))
        return out[:20]

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        url = f"{self.base_url}/comic/{ref}.html"
        html = await self.fetcher.get_text(url)

        title_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
        title = _text(title_m.group(1)) if title_m else result.title

        desc_m = re.search(
            r'<meta[^>]+name="description"[^>]+content=["\']([^"\']+)["\']', html, re.I | re.S
        )
        desc = _text(desc_m.group(1)) if desc_m else None

        og_cover = re.search(
            r'<meta[^>]+property="og:image"[^>]+content=["\']([^"\']+)["\']', html, re.I
        )
        cover_m = re.search(r'<img[^>]+data-src=["\']([^"\']+)["\']', html, re.I)
        cover = result.cover_url
        if og_cover:
            cover = _html.unescape(og_cover.group(1))
        elif cover_m:
            cover = _html.unescape(cover_m.group(1))

        return MangaDetail(
            source_id=self.id,
            title=title,
            ref=ref,
            cover_url=cover,
            description=desc,
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        ref = _clean_ref(detail.ref)
        chapters: list[Chapter] = []

        try:
            api_url = f"{self.base_url}/comic/{ref}"
            data = await self.fetcher.post_json(
                api_url,
                json={},
                headers={
                    "Content-Type": "application/json",
                    "Referer": f"{self.base_url}/comic/{ref}.html",
                },
            )

            if (
                isinstance(data, dict)
                and data.get("code") == 0
                and "data" in data
                and "chapters" in data["data"]
            ):
                raw_chapters = _in_reading_order(list(data["data"]["chapters"]))
                for idx, ch in enumerate(raw_chapters):
                    ch_id = str(ch.get("id"))
                    content_id = str(ch.get("contentId", ref))
                    ch_name = _text(str(ch.get("chapterName", f"Chapter {idx + 1}")))
                    ch_ref = f"{content_id}-{ch_id}"
                    chapters.append(
                        Chapter(
                            source_id=self.id,
                            manga_ref=ref,
                            title=ch_name,
                            ref=ch_ref,
                            index=idx + 1,
                            extra={"href": f"/chapter/{ch_ref}.html"},
                        )
                    )
                if chapters:
                    return chapters
        except Exception as err:
            logger.warning(
                "wmanhua POST chapters API failed for ref '%s': %s; falling back to HTML parsing",
                ref,
                err,
            )

        detail_url = f"{self.base_url}/comic/{ref}.html"
        html = await self.fetcher.get_text(detail_url)
        ch_pattern = re.compile(
            r'<a[^>]+href=["\'](?:https?://[^/"\']*)?/chapter/(\d+-\d+)\.html["\'][^>]*>(.*?)</a>',
            re.I | re.S,
        )

        matches = sorted(ch_pattern.finditer(html), key=lambda m: _ref_chapter_id(m.group(1)) or 0)
        for idx, match in enumerate(matches):
            ch_ref = match.group(1)
            ch_title = _text(match.group(2))
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=ref,
                    title=ch_title or f"Chapter {idx + 1}",
                    ref=ch_ref,
                    index=idx + 1,
                    extra={"href": f"/chapter/{ch_ref}.html"},
                )
            )

        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        ch_ref = chapter.ref
        chapter_url = f"{self.base_url}/chapter/{ch_ref}.html"
        html = await self.fetcher.get_text(chapter_url)

        num_m = re.search(r'var\s+num\s*=\s*(?:eval\(["\'])?(\d+)(?:["\']\))?', html, re.I)
        pasd_m = re.search(r'var\s+pasd\s*=\s*["\']([^"\']+)["\']', html, re.I)

        pages: list[Page] = []
        referer = f"{self.base_url}/"

        if num_m and pasd_m:
            page_count = int(num_m.group(1))
            pasd = pasd_m.group(1)

            for i in range(page_count):
                pages.append(
                    Page(
                        index=i,
                        image_url=f"{pasd}{i + 1}.webp",
                        referer=referer,
                    )
                )
            return pages

        total_m = re.search(r"var\s+totalPages\s*=\s*parseInt\(\s*['\"]?(\d+)", html, re.I)
        base_m = re.search(r"var\s+imgBaseUrl\s*=\s*(.+?);", html, re.S)
        if total_m and base_m:
            img_base = _js_concat_base(base_m.group(1))
            if img_base:
                for i in range(int(total_m.group(1))):
                    pages.append(
                        Page(index=i, image_url=f"{img_base}{i + 1}.webp", referer=referer)
                    )
                return pages

        img_pattern = re.compile(r'<img[^>]+(?:data-src|src)=["\']([^"\']+)["\']', re.I)
        seen_urls: set[str] = set()
        idx = 0
        for match in img_pattern.finditer(html):
            img_url = _html.unescape(match.group(1)).strip()
            if not img_url or img_url.startswith("data:image") or img_url in seen_urls:
                continue
            lowered = img_url.lower()
            if any(tok in lowered for tok in ("favicon", "loading", "logo", ".svg", ".ico")):
                continue
            img_url = urljoin(chapter_url, img_url)
            seen_urls.add(img_url)
            pages.append(
                Page(
                    index=idx,
                    image_url=img_url,
                    referer=referer,
                )
            )
            idx += 1

        return pages

"""楠楠漫画 (nnmh.cc family) source plugin.

Reverse-engineered from HAR captures + site JS (nnman-analysis/nnmh_dl.py):

    Search   POST /index.php?m=&c=mh&a=load_searchpage
             body: page=N&key=<utf8>&paixu=3&status=0&limitStatus=0
             (X-Requested-With: XMLHttpRequest) -> JSON {status, info:[book,...]}
    Book     GET  /home/book/index/id/<bid>/   (chapter list is server-rendered,
             two templates: data-order chips, or the older #html_box item list)
    Reader   GET  /home/book/inforedit/<bid>/<cid>
             HTML embeds  var encryptedData = "<base64>"
             IV = first 16 decoded bytes, rest is AES-256-CBC, PKCS7.
             Key = first 32 chars of the base64-looking string that heads the
             obfuscated `var _0x…= ['…', 'lib', …]` table on the same page.
             Plaintext is a JSON array of image URLs.
    Images   https://img.yidanmh.xyz/bookimage/<bid>/<cid>/<uuid>.<ext>
             hotlink-protected: requires Referer: https://<site-domain>/

The domain pool rotates. The live list is published at
https://nnmhcc.github.io/config.YAML ("domains:"); candidates are probed and
the alive ones cached in `.cache/nnmh_domains.json` for `domain_ttl_hours`.
A `base_url` setting pins one domain and skips resolution entirely.

The reader API is flaky: it may return an empty or undecryptable blob on the
first hits, so `get_pages` re-probes, rotating domains every couple of tries.
"""

from __future__ import annotations

import asyncio
import base64
import html as _html
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from Crypto.Cipher import AES

from app.config import PROJECT_ROOT
from app.core.aes import strip_pkcs7
from app.core.fetcher import FetchError
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

_DEFAULT_PORTAL = "https://nnmhcc.github.io/config.YAML"

_FALLBACK_DOMAINS = ["nnmh.cc", "nnmh.info", "nnboook.com"]

_FALLBACK_KEY = b"tH1rU6qZ4vU1sK7pN1wO7mX4bY6dQ9gX"


_SEARCH_PAGE_MARK = "漫画搜索"

_ENCRYPTED = re.compile(r'var encryptedData = "([A-Za-z0-9+/=]*)"')
_KEY_TABLE = re.compile(
    r"""var _0x\w+\s*=\s*\[\s*['"]([A-Za-z0-9+/=]{20,60})['"]\s*,\s*['"]lib['"]""", re.S
)

_TITLE_TAG = re.compile(r"<title>(.*?)</title>", re.S)
_OG_IMAGE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_DESCRIPTION = re.compile(r'<meta\s+name="description"\s+content="([^"]*)"', re.I)


_CHAPTER_ORDERED = re.compile(
    r'data-order="(\d+)"[^>]*>\s*'
    r'<a\s+href="([^"]*?/home/book/inforedit/(\d+)/(\d+)[^"]*)"[^>]*>'
    r'.*?<span class="truncate">(.*?)</span>',
    re.S,
)

_HTML_BOX = re.compile(r'id="html_box".*?(?=<div id="other_book"|$)', re.S)
_CHAPTER_PLAIN = re.compile(
    r'<a\s+href="([^"]*?/home/book/inforedit/(\d+)/(\d+)[^"]*)"[^>]*>(.*?)</a>', re.S
)
_TAG = re.compile(r"<[^>]+>")

_TITLE_SUFFIX = re.compile(r"漫画在线阅读|-楠楠漫画|- 楠楠漫画")


def _strip_title_alias(title: str) -> str:
    """Drop the site's trailing parenthesised alias: ``魔臨(魔临)`` -> ``魔臨``.

    Books are titled ``main(alias)`` with the alias being a simplified/variant
    rendering of the same work; only ONE trailing balanced group is removed so
    markers like ``前男友前女友(完結)`` survive a ``name(name(完结))`` alias.
    """
    title = title.strip()
    if not title.endswith((")", "）")):
        return title
    depth = 0
    for pos in range(len(title) - 1, -1, -1):
        char = title[pos]
        if char in ")）":
            depth += 1
        elif char in "(（":
            depth -= 1
            if depth == 0:
                return title[:pos].strip() or title
    return title


def _clean_ref(query: str) -> str:
    """Reduce a book URL / ``/home/book/index/id/<bid>/`` path / bare id to the id."""
    query = query.strip()
    if query.startswith(("http://", "https://", "/")):
        parts = [part for part in urlparse(query).path.split("/") if part]
        if "id" in parts:
            pos = parts.index("id")
            if len(parts) > pos + 1:
                return parts[pos + 1]
        return parts[-1] if parts else query
    return query


def _host_of(url: str) -> str:
    host = urlparse(url).netloc or url
    return host.strip("/")


class NnmhPlugin(SourcePlugin):
    id = "nnmh"
    name = "楠楠漫画 (nnmh)"
    category = "18+"
    fetch_mode = FetchMode.HTTP

    def __init__(self, cfg, fetcher) -> None:
        super().__init__(cfg, fetcher)
        self._domains: list[str] = []
        self._domain_i = 0

    @property
    def base_url(self) -> str:
        """Origin used as the Referer for image/cover requests (GUI cover map)."""
        return f"https://{self._referer_host()}"

    def _referer_host(self) -> str:
        """Host the image CDN's hotlink whitelist accepts.

        Not always the serving domain: verified live, nnmh.me serves pages but
        a Referer of https://nnmh.me/ is rejected (403) while nnmh.cc/info/
        nnboook.com pass. `image_referer_host` pins it; empty falls back to the
        current serving domain.
        """
        pinned = str(self.settings.get("image_referer_host") or "").strip()
        if pinned:
            return _host_of(pinned)
        if self._domains:
            return self._domain
        override = self._override_domain()
        if override:
            return override
        pool, _ = self._load_domain_cache()
        return pool[0] if pool else _FALLBACK_DOMAINS[0]

    def _override_domain(self) -> str | None:
        base = str(self.settings.get("base_url") or "").strip()
        return _host_of(base) if base else None

    @property
    def _domain(self) -> str:
        return self._domains[self._domain_i]

    def _rotate(self) -> None:
        if self._domains:
            self._domain_i = (self._domain_i + 1) % len(self._domains)

    def _domain_cache_path(self):
        return PROJECT_ROOT / ".cache" / "nnmh_domains.json"

    def _load_domain_cache(self) -> tuple[list[str], bool]:
        path = self._domain_cache_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return [], False
        ttl_hours = float(self.settings.get("domain_ttl_hours", 6))
        fresh = time.time() - float(data.get("ts", 0)) < ttl_hours * 3600
        pool = [d for d in data.get("pool", []) if isinstance(d, str) and d]
        return pool, bool(pool) and fresh

    def _save_domain_cache(self, pool: list[str]) -> None:
        try:
            path = self._domain_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"ts": time.time(), "pool": pool}), encoding="utf-8")
        except Exception as exc:
            logger.debug("nnmh: domain cache write failed: %s", exc)

    async def _probe(self, domain: str) -> str:
        """Alive domains serve the search page directly (no redirect, real body)."""
        return await self.fetcher.get_text(f"https://{domain}/home/mh/load_search")

    async def _ensure_domains(self) -> list[str]:
        if self._domains:
            return self._domains

        override = self._override_domain()
        if override:
            self._domains = [override]
            return self._domains

        cached, fresh = self._load_domain_cache()
        if fresh:
            self._domains = cached
            logger.debug("nnmh: domains from cache: %s", ", ".join(cached))
            return self._domains

        pool = await self._resolve_from_portal(cached)
        self._domains = pool or cached or list(_FALLBACK_DOMAINS)
        logger.info("nnmh: domains: %s", ", ".join(self._domains))
        return self._domains

    async def _resolve_from_portal(self, cached: list[str]) -> list[str]:
        portal = str(self.settings.get("portal_url") or _DEFAULT_PORTAL)
        try:
            text = await self.fetcher.get_text(portal)
        except Exception as exc:
            logger.warning("nnmh: portal fetch failed (%s); using cached pool", exc)
            return cached

        candidates = self._parse_portal_domains(text)
        if not candidates:
            return cached

        results = await asyncio.gather(
            *[self._probe(domain) for domain in candidates], return_exceptions=True
        )
        alive = [
            domain
            for domain, body in zip(candidates, results, strict=True)
            if isinstance(body, str) and _SEARCH_PAGE_MARK in body
        ]
        if alive:
            self._save_domain_cache(alive)
            return alive
        logger.warning("nnmh: no portal domain probed alive; trying them unprobed")
        return candidates

    @staticmethod
    def _parse_portal_domains(yaml_text: str) -> list[str]:
        """Pull the bare hosts out of the portal's `domains:` block (no PyYAML)."""
        domains: list[str] = []
        in_block = False
        for line in yaml_text.splitlines():
            if re.match(r"^domains\s*:", line):
                in_block = True
                continue
            if not in_block:
                continue
            if re.match(r"^\s*#", line) or not line.strip():
                continue
            if re.match(r"^\S", line):
                break
            m = re.match(r"""^\s*-\s*["']?([^"'\s]+)["']?\s*$""", line)
            if not m:
                continue
            host = _host_of(re.sub(r"^https?://", "", m.group(1)))
            if host and "/" not in host and host not in domains:
                domains.append(host)
        return domains

    async def _get_rotating(
        self,
        url_for: Callable[[str], str],
        referer_for: Callable[[str], str] | None = None,
        validate: Callable[[str], bool] | None = None,
    ) -> str:
        domains = await self._ensure_domains()
        last: Exception | None = None
        for _ in range(len(domains)):
            headers = {"Referer": referer_for(self._domain)} if referer_for else None
            try:
                body = await self.fetcher.get_text(url_for(self._domain), headers=headers)
                if validate is not None and not validate(body):
                    raise FetchError(f"nnmh: {self._domain} served a landing page")
                return body
            except Exception as exc:
                last = exc
                logger.warning("nnmh: GET failed on %s (%s); rotating", self._domain, exc)
                self._rotate()
        raise FetchError(f"nnmh: all domains failed for {url_for(self._domain)}: {last}")

    @staticmethod
    def _is_book_page(html: str) -> bool:
        return "html_box" in html or "data-order" in html

    async def search(self, query: str) -> list[SearchResult]:
        query = query.strip()
        if not query:
            return []

        ref = _clean_ref(query)
        if query.startswith(("http://", "https://", "/")) or (ref.isdigit() and len(ref) >= 3):
            return await self._search_by_ref(ref)

        books = await self._search_pages(query)
        return [
            SearchResult(
                source_id=self.id,
                title=_strip_title_alias(str(book.get("title") or book.get("id") or "").strip()),
                ref=str(book["id"]),
                cover_url=book.get("cover_pic") or None,
                author=book.get("author") or None,
                extra={
                    "status": book.get("status") or None,
                    "latest": book.get("maxepisodes") or None,
                },
            )
            for book in books
            if book.get("id")
        ][:30]

    async def _search_by_ref(self, ref: str) -> list[SearchResult]:
        try:
            result = SearchResult(source_id=self.id, title=ref, ref=ref)
            detail = await self.get_detail(result)
        except Exception as err:
            logger.warning("nnmh: direct ref search failed for %s: %s", ref, err)
            return []
        return [
            SearchResult(
                source_id=self.id,
                title=detail.title,
                ref=ref,
                cover_url=detail.cover_url,
                author=detail.author,
            )
        ]

    async def _search_pages(self, key: str) -> list[dict[str, Any]]:
        await self._ensure_domains()
        paixu = int(self.settings.get("search_paixu", 3))
        books: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in (1, 2, 3):
            try:
                res = await self._search_post(key, page, paixu)
            except FetchError as exc:
                logger.warning("nnmh: search page %d failed on all domains: %s", page, exc)
                break

            info = res.get("info") if isinstance(res, dict) else None
            if not isinstance(info, list):
                break
            fresh = [
                b for b in info if isinstance(b, dict) and b.get("id") and str(b["id"]) not in seen
            ]
            if not fresh:
                break
            seen.update(str(b["id"]) for b in fresh)
            books.extend(fresh)
            if len(info) < 10:
                break
        return books

    async def _search_post(self, key: str, page: int, paixu: int) -> dict[str, Any]:
        """POST the search endpoint, rotating across the pool until one answers."""
        payload = {"page": page, "key": key, "paixu": paixu, "status": 0, "limitStatus": 0}
        domains = await self._ensure_domains()
        last: Exception | None = None
        for _ in range(len(domains)):
            try:
                text = await self.fetcher.post_text(
                    f"https://{self._domain}/index.php?m=&c=mh&a=load_searchpage",
                    data=payload,
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": f"https://{self._domain}/home/mh/load_search",
                    },
                )
                return json.loads(text)
            except Exception as exc:
                last = exc
                logger.warning("nnmh: search failed on %s (%s); rotating", self._domain, exc)
                self._rotate()
        raise FetchError(f"nnmh: search failed on all domains: {last}")

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        html = await self._get_rotating(
            lambda domain: f"https://{domain}/home/book/index/id/{ref}/",
            validate=self._is_book_page,
        )
        return self._parse_detail(html, result, ref)

    def _parse_detail(self, html: str, result: SearchResult, ref: str) -> MangaDetail:
        title_m = _TITLE_TAG.search(html)
        title = title_m.group(1).strip() if title_m else result.title
        title = _TITLE_SUFFIX.split(title)[0].strip() or result.title
        title = _strip_title_alias(title) or result.title

        cover_m = _OG_IMAGE.search(html)
        cover = _html.unescape(cover_m.group(1)).strip() if cover_m else result.cover_url

        desc_m = _DESCRIPTION.search(html)
        description = _html.unescape(desc_m.group(1)).strip() if desc_m else None

        chapters = self._parse_chapters(html, ref, title)

        return MangaDetail(
            source_id=self.id,
            title=title,
            ref=ref,
            cover_url=cover,
            author=result.author,
            description=description or None,
            status=result.extra.get("status"),
            chapter_count=len(chapters) or None,
            extra={"book_html": html, **result.extra},
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        ref = _clean_ref(detail.ref)
        html = detail.extra.get("book_html")
        if not html:
            html = await self._get_rotating(
                lambda domain: f"https://{domain}/home/book/index/id/{ref}/",
                validate=self._is_book_page,
            )
        return self._parse_chapters(html, ref, detail.title)

    def _parse_chapters(self, html: str, manga_ref: str, book_title: str = "") -> list[Chapter]:
        raw: list[tuple[int, str, str]] = []
        for m in _CHAPTER_ORDERED.finditer(html):
            order, _href, bid, cid, title = m.groups()
            raw.append((int(order), cid, self._chapter_title(title, book_title)))
        if not raw:
            box = _HTML_BOX.search(html)
            scope = box.group(0) if box else html
            for order, m in enumerate(_CHAPTER_PLAIN.finditer(scope), 1):
                _href, _bid, cid, inner = m.groups()
                raw.append((order, cid, self._chapter_title(inner, book_title)))

        seen: set[str] = set()
        chapters: list[Chapter] = []
        for order, cid, title in sorted(raw, key=lambda item: item[0]):
            if cid in seen:
                continue
            seen.add(cid)
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=manga_ref,
                    title=title or f"第{order}話",
                    ref=cid,
                    index=len(chapters) + 1,
                    extra={"order": order, "book": str(manga_ref)},
                )
            )
        logger.info("nnmh: parsed %d chapters for book %s", len(chapters), manga_ref)
        return chapters

    @staticmethod
    def _chapter_text(fragment: str) -> str:
        return re.sub(r"\s+", " ", _TAG.sub("", _html.unescape(fragment))).strip()

    @classmethod
    def _chapter_title(cls, fragment: str, book_title: str) -> str:
        """Chip text minus the book-name prefix the site glues onto every chip
        (``打開她的苞 第4話`` -> ``第4話``). Only a whitespace boundary counts,
        so a chip whose real title merely *starts* with the book name survives."""
        text = cls._chapter_text(fragment)
        prefix = book_title.strip()
        if prefix and text.startswith(prefix) and len(text) > len(prefix):
            if text[len(prefix)] in " \t　":
                return text[len(prefix) :].lstrip()
        return text

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        book = str(chapter.extra.get("book") or chapter.manga_ref)
        cid = str(chapter.ref)
        await self._ensure_domains()
        retries = int(self.settings.get("reader_retries", 5))

        last_err = "unknown"
        for attempt in range(1, retries + 1):
            referer = f"https://{self._domain}/home/book/index/id/{book}/"
            try:
                html = await self.fetcher.get_text(
                    f"https://{self._domain}/home/book/inforedit/{book}/{cid}",
                    headers={"Referer": referer},
                )
                urls, err = self._parse_reader(html)
                if urls:
                    if attempt > 1:
                        logger.info(
                            "nnmh: reader probe #%d succeeded (%d images)", attempt, len(urls)
                        )
                    referer_root = f"https://{self._referer_host()}/"
                    return [
                        Page(index=pos, image_url=url, referer=referer_root)
                        for pos, url in enumerate(urls)
                    ]
                last_err = err or "empty payload"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            logger.info("nnmh: reader probe #%d for %s/%s empty (%s)", attempt, book, cid, last_err)
            if attempt % 2 == 0:
                self._rotate()
            await asyncio.sleep(1.0 + 0.5 * attempt)
        raise FetchError(
            f"nnmh: no image list for chapter {book}/{cid} after {retries} probes "
            f"(last: {last_err})"
        )

    def _parse_reader(self, html: str) -> tuple[list[str] | None, str | None]:
        blob_m = _ENCRYPTED.search(html)
        if not blob_m:
            return None, "no encryptedData on page"
        blob = blob_m.group(1)
        if not blob:
            return None, "encryptedData empty"

        key_m = _KEY_TABLE.search(html)
        key = key_m.group(1)[:32].encode() if key_m else _FALLBACK_KEY
        try:
            raw = base64.b64decode(blob)
            if len(raw) <= 16:
                return None, f"blob too short ({len(raw)}B)"
            plain = strip_pkcs7(AES.new(key, AES.MODE_CBC, raw[:16]).decrypt(raw[16:]))
            urls = json.loads(plain.decode("utf-8", "replace"))
        except Exception as exc:
            return None, f"decrypt failed: {exc}"
        if not isinstance(urls, list) or not urls:
            return None, f"unexpected payload: {str(urls)[:80]}"
        return [u for u in urls if isinstance(u, str) and u.startswith("http")], None

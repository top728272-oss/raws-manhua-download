"""CopyManga (拷貝漫畫) source plugin.

The whole service runs the *same* API on two kinds of host, and they have
different rules - getting this wrong is the single failure mode that matters:

* **App hosts** - ``api.copy202601.com`` / ``api.copy202602.com`` /
  ``api.copy3000.com``. They belong to the Android app and require its own
  ``x-auth-signature`` header. Any other client gets ``code:210`` ("please
  update the app") or a null body.
* **Web hosts** - ``api.2024manga.com`` and the ``mapi.hotmangas*`` /
  ``mapi.*.club`` cluster. Same JSON, same paths, **no signature required**.

So this plugin bootstraps from a web host, keeps only the web hosts it hands
back, and refuses to talk to an app host at all (``_is_app_host``). No
``x-auth-signature``, no ``in_mainland``, no ``request_id`` - none of it is
needed on the web hosts.

Endpoint map (all GET, ``?platform=3``; envelope ``{"code":200,"results":…}``):

  /api/v3/system/network2                       live host list (bootstrap)
  /api/v3/search/comic?q=&limit=&offset=        keyword search
  /api/v3/comic2/{path_word}                    comic metadata (+groups)
  /api/v3/comic/{pw}/group/{group}/chapters     chapter list
  /api/v3/comic/{pw}/chapter/{uuid}             page images  (web hosts)
  /api/v3/comic/{pw}/chapter2/{uuid}            page images  (app hosts - 404s here)

``code:210`` means "you reached an app host" - treated as a host failure and
retried on the next host, never surfaced as an empty chapter. Images live on a
CDN and need nothing but a ``User-Agent``.

The public site is ``https://www.mangacopy.com`` (mainland mirror:
``https://www.copy4000.com``). It is a normal web front-end with
``/comic/{path_word}`` URLs - it does **not** serve the API (every ``/api/v3``
path 404s there), so ``base_url`` is only used for the cover ``Referer``; the
plugin itself talks to the API hosts only, via ``api_hosts``.

Page order
----------
``results.chapter`` carries ``contents[]`` (the image URLs) and, on some
chapters, ``words[]`` - a permutation of ``0..n-1``. The contents array is
*not* reliably in reading order; ``words`` is the unscramble key. The rule
(matching the shipped third-party client ``fumiama/copymanga``,
``Downloader.kt::getMangaUrls``) is::

    by_pos[words[i]] = contents[i]
    page[i]          = by_pos[i]        # i.e. contents sorted by words[]

``words`` is frequently absent (verified live) - then the contents order is
used as-is. ``page_order: contents`` in config forces that path.

Verified live (2026-09-27): search -> detail -> chapters -> pages -> WebP bytes.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import quote, urlparse

from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)


WEB_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36 Edg/141.0.0.0"
)
X_REQUESTED_WITH = "com.manga2020.app"
PLATFORM = "3"
VERSION = "2024.4.28"


BOOTSTRAP_HOSTS = [
    "https://api.2024manga.com",
    "https://mapi.hotmangasg.com",
    "https://mapi.hotmangasd.com",
    "https://mapi.hotmangasf.com",
    "https://mapi.elfgjfghkk.club",
    "https://mapi.fgjfghkkcenter.club",
    "https://mapi.fgjfghkk.club",
]

APP_HOST_MARKERS = ("copy202601", "copy202602", "copy3000", "copy2000")

DEFAULT_BASE_URL = "https://www.mangacopy.com"


DEFAULT_IMAGE_HOST = "https://sy.mangafunb.fun"

CODE_OK = 200
CODE_APP_HOST = 210
SEARCH_LIMIT = 20
CHAPTER_LIMIT = 100


_RES_TOKEN = re.compile(r"\.([a-z])\d+x\.")


class CopyMangaError(RuntimeError):
    """Raised on API-level failures (bad code, no host, empty payload)."""


class AppHostBlocked(CopyMangaError):
    """Reached an app-only host, which needs the Android app's x-auth-signature."""


def _is_app_host(host: str) -> bool:
    return any(marker in host for marker in APP_HOST_MARKERS)


def _normalise_host(host: str) -> str:
    host = str(host).strip().rstrip("/")
    if not host:
        return ""
    if not host.startswith("http"):
        host = f"https://{host}"
    return host


def _httpify(url: Any) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def _clean_ref(query: str) -> str:
    """Turn a site URL / ``pw:``-prefixed id into a bare ``path_word``; else pass through.

    Only ``…/comic/{path_word}`` URLs are refs (that is the site's own shape).
    A bare slug is deliberately NOT treated as a ref - path_words look like
    ordinary lowercase words, so guessing would hijack real keyword searches;
    likewise any other URL (e.g. a pasted ``/search?q=`` page) is handed back
    unchanged so it degrades to a keyword search instead of a bogus ref.
    """
    q = (query or "").strip()
    low = q.lower()
    for prefix in ("pw:", "id:", "copymanga:"):
        if low.startswith(prefix):
            return q[len(prefix) :].strip().strip("/")
    if low.startswith("http://") or low.startswith("https://"):
        parts = [p for p in urlparse(q).path.split("/") if p]
        if "comic" in parts:
            idx = parts.index("comic")
            if idx + 1 < len(parts):
                return parts[idx + 1].strip()
    return q


def _join_authors(author: Any) -> str | None:
    """CopyManga `author` is a list of {name, path_word}; join the names."""
    if isinstance(author, str) and author.strip():
        return author.strip()
    if isinstance(author, list):
        names = []
        for item in author:
            if isinstance(item, dict):
                name = item.get("name") or item.get("alias")
            else:
                name = item
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        if names:
            return ", ".join(names)
    return None


def _reorder(contents: list[str], words: Any) -> list[str]:
    """Apply the `words` permutation; fall back to contents order when unusable.

    Mirrors fumiama/copymanga: ``by_pos[words[i]] = contents[i]`` then emit
    ``by_pos[0..n-1]``. If `words` is missing, malformed, or not a permutation
    covering every index, the raw contents order is returned unchanged.
    """
    n = len(contents)
    if n == 0 or not isinstance(words, list) or not words:
        return contents

    perm: list[int] = []
    for value in words:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return contents
        perm.append(int(value))
    if len(perm) < n:
        seen = set(perm)
        perm += [i for i in range(n) if i not in seen]
    if len(perm) < n or len(set(perm[:n])) != n or any(v < 0 or v >= n for v in perm[:n]):
        logger.warning("copymanga: unusable `words` permutation; keeping contents order")
        return contents

    by_pos: dict[int, str] = {perm[i]: contents[i] for i in range(n)}
    ordered = [by_pos.get(i) for i in range(n)]
    if any(url is None for url in ordered):
        return contents
    return [u for u in ordered if u is not None]


def _set_width(url: str, width: int) -> str:
    """Rewrite the CDN size hint (`.h1500x.` / `.c1500x.`) to `width`."""
    return _RES_TOKEN.sub(lambda m: f".{m.group(1)}{width}x.", url)


def _unwrap(body: Any, url: str) -> Any:
    """Validate the `{code, results}` envelope and return `results`."""
    if not isinstance(body, dict):
        raise CopyMangaError(f"unexpected non-object response from {url}")
    raw_code = body.get("code")
    try:
        code: Any = int(raw_code)
    except (TypeError, ValueError):
        code = raw_code
    if code == CODE_APP_HOST:
        raise AppHostBlocked(f"code 210 (app-only host) from {url}: {body.get('message')}")
    if code != CODE_OK:
        raise ValueError(f"copymanga: API code {code!r} for {url}: {body.get('message')}")
    results = body.get("results")
    if results is None:
        raise CopyMangaError(f"code 200 but results is null from {url}")
    return results


def _parse_network(body: Any) -> tuple[list[str], str | None]:
    """Pull (web api hosts, image host) out of a /system/network2 payload."""
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, dict):
        return [], None

    hosts: list[str] = []

    def add(value: Any) -> None:
        host = _normalise_host(value) if value else ""
        if host and not _is_app_host(host) and host not in hosts:
            hosts.append(host)

    for group in results.get("api") or []:
        for host in group or []:
            add(host)
    for host in results.get("share") or []:
        add(host)

    static = results.get("static")
    image_host = _normalise_host(static) if static else None
    return hosts, image_host


class CopyMangaPlugin(SourcePlugin):
    id = "copymanga"
    name = "拷貝漫畫 (CopyManga)"
    category = "General"
    fetch_mode = FetchMode.HTTP

    def __init__(self, cfg, fetcher) -> None:
        super().__init__(cfg, fetcher)
        self._hosts: list[str] = []
        self._image_host = DEFAULT_IMAGE_HOST
        self._boot_lock = asyncio.Lock()
        self._booted = False

    @property
    def base_url(self) -> str:
        """Public site URL - used as the cover Referer. NOT an API host.

        ``https://www.mangacopy.com`` (mainland mirror: ``https://www.copy4000.com``).
        The site answers HTML and 404s every ``/api/v3`` path, so it is never
        contacted by this plugin; it exists for the GUI's Referer map and for
        user-facing links.
        """
        return str(self.settings.get("base_url", DEFAULT_BASE_URL)).rstrip("/")

    @property
    def api_hosts(self) -> list[str]:
        """API hosts to try, in order: config ``hosts``, then the built-ins.

        The website is deliberately absent from this list, and app-only hosts are
        filtered out so they are never contacted.
        """
        hosts: list[str] = []
        for value in list(self.settings.get("hosts") or []) + BOOTSTRAP_HOSTS:
            host = _normalise_host(value) if value else ""
            if host and not _is_app_host(host) and host not in hosts:
                hosts.append(host)
        return hosts

    @property
    def token(self) -> str | None:
        raw = str(self.settings.get("token", "") or "").strip()
        return raw or None

    @property
    def page_order(self) -> str:
        value = str(self.settings.get("page_order", "words")).strip().lower()
        return value if value in ("words", "contents") else "words"

    @property
    def image_width(self) -> int | None:
        raw = self.settings.get("image_width")
        if raw in (None, "", False):
            return None
        try:
            width = int(raw)
        except (TypeError, ValueError):
            return None
        return width if 200 <= width <= 4000 else None

    def _headers(self) -> dict[str, str]:
        headers = {
            "user-agent": WEB_UA,
            "x-requested-with": X_REQUESTED_WITH,
            "platform": PLATFORM,
            "version": VERSION,
            "webp": "1",
            "accept": "application/json",
            "accept-encoding": "gzip",
        }
        if self.token:
            headers["authorization"] = f"Token {self.token}"
        return headers

    async def _ensure_bootstrap(self) -> None:
        if self._booted:
            return
        async with self._boot_lock:
            if self._booted:
                return
            for boot in self.api_hosts:
                try:
                    body = await self.fetcher.get_json(
                        f"{boot}/api/v3/system/network2",
                        params={"platform": PLATFORM},
                        headers=self._headers(),
                    )
                except Exception as err:
                    logger.debug("copymanga: bootstrap %s failed: %s", boot, err)
                    continue
                hosts, image_host = _parse_network(body)
                if hosts:
                    self._hosts = hosts
                    if image_host:
                        self._image_host = image_host
                    break
            if not self._hosts:
                self._hosts = self.api_hosts
            self._booted = True
            logger.info(
                "copymanga: bootstrapped hosts=%s image_host=%s",
                self._hosts[:3],
                self._image_host,
            )

    async def _api_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET `path` on the first host that answers, walking the host list."""
        await self._ensure_bootstrap()

        hosts: list[str] = []
        for host in self._hosts + self.api_hosts:
            if not _is_app_host(host) and host not in hosts:
                hosts.append(host)

        last: Exception | None = None
        for host in hosts:
            url = f"{host}{path}"
            query: dict[str, Any] = {"platform": PLATFORM}
            if params:
                query.update(params)
            try:
                body = await self.fetcher.get_json(url, params=query, headers=self._headers())
            except Exception as err:
                last = err
                continue
            try:
                return _unwrap(body, url)
            except AppHostBlocked as err:
                logger.warning("copymanga: %s", err)
                last = err
                continue
        raise last or CopyMangaError("no usable CopyManga API host")

    async def _fetch_comic(self, path_word: str) -> dict:
        results = await self._api_get(f"/api/v3/comic2/{quote(path_word)}")
        return results if isinstance(results, dict) else {}

    async def search(self, query: str) -> list[SearchResult]:
        ref = _clean_ref(query)
        try:
            if ref != query.strip():
                results = await self._search_direct(ref)
            else:
                results = await self._search_keyword(query)
            logger.info("copymanga: search '%s' -> %d result(s)", query, len(results))
            return results
        except Exception as err:
            logger.warning("copymanga search failed for '%s': %s", query, err)
            return []

    async def _search_keyword(self, query: str) -> list[SearchResult]:
        results = await self._api_get(
            "/api/v3/search/comic",
            {"q": query, "limit": SEARCH_LIMIT, "offset": 0},
        )
        return self._parse_search(results)

    async def _search_direct(self, path_word: str) -> list[SearchResult]:
        if not path_word:
            return []
        try:
            results = await self._fetch_comic(path_word)
        except Exception as err:
            logger.warning("copymanga: direct ref '%s' failed: %s", path_word, err)
            return []
        comic = results.get("comic") if isinstance(results, dict) else None
        if not isinstance(comic, dict):
            return []
        return [
            SearchResult(
                source_id=self.id,
                title=str(comic.get("name") or path_word).strip(),
                ref=str(comic.get("path_word") or path_word).strip(),
                cover_url=_httpify(comic.get("cover")),
                author=_join_authors(comic.get("author")),
            )
        ]

    def _parse_search(self, results: Any) -> list[SearchResult]:
        items = results.get("list") if isinstance(results, dict) else None
        if not isinstance(items, list):
            items = []
        out: list[SearchResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            path_word = str(item.get("path_word") or "").strip()
            if not path_word:
                continue
            out.append(
                SearchResult(
                    source_id=self.id,
                    title=str(item.get("name") or path_word).strip(),
                    ref=path_word,
                    cover_url=_httpify(item.get("cover")),
                    author=_join_authors(item.get("author")),
                    extra={"popular": item.get("popular"), "alias": item.get("alias")},
                )
            )
        return out

    @staticmethod
    def _pick_group(groups: Any, wanted: str = "") -> str:
        if not isinstance(groups, dict) or not groups:
            return wanted or "default"
        if wanted and wanted in groups:
            return wanted
        if "default" in groups:
            return "default"
        return next(iter(groups))

    @staticmethod
    def _display(value: Any) -> str | None:
        if isinstance(value, dict):
            shown = value.get("display")
            return str(shown).strip() if shown else None
        return str(value).strip() if isinstance(value, str) and value.strip() else None

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        results = await self._fetch_comic(ref)
        comic = results.get("comic") if isinstance(results.get("comic"), dict) else {}
        groups = results.get("groups") if isinstance(results.get("groups"), dict) else {}
        group = self._pick_group(groups, str(self.settings.get("group", "") or ""))

        chapter_count: int | None = None
        group_info = groups.get(group)
        if isinstance(group_info, dict) and isinstance(group_info.get("count"), int):
            chapter_count = int(group_info["count"])

        themes = comic.get("theme")
        tags = [
            str(t.get("name")).strip()
            for t in (themes if isinstance(themes, list) else [])
            if isinstance(t, dict) and t.get("name")
        ]

        return MangaDetail(
            source_id=self.id,
            title=str(comic.get("name") or result.title or ref).strip(),
            ref=str(comic.get("path_word") or ref).strip(),
            cover_url=_httpify(comic.get("cover")) or result.cover_url,
            author=_join_authors(comic.get("author")) or result.author,
            description=comic.get("brief"),
            status=self._display(comic.get("status")),
            tags=tags,
            chapter_count=chapter_count,
            extra={
                "path_word": str(comic.get("path_word") or ref).strip(),
                "uuid": comic.get("uuid"),
                "alias": comic.get("alias"),
                "group": group,
                "groups": groups,
                "popular": results.get("popular") or comic.get("popular"),
                "free_type": self._display(comic.get("free_type")),
                "restrict": self._display(comic.get("restrict")),
                "region": self._display(comic.get("region")),
                "reclass": self._display(comic.get("reclass")),
                "last_chapter": comic.get("last_chapter"),
                "datetime_updated": comic.get("datetime_updated"),
                "is_vip": results.get("is_vip"),
                "is_lock": results.get("is_lock"),
            },
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        ref = _clean_ref(detail.ref)
        extra = detail.extra or {}
        groups = extra.get("groups") if isinstance(extra.get("groups"), dict) else {}
        fallback_group = self._pick_group(groups, str(self.settings.get("group", "") or ""))
        group = str(extra.get("group") or fallback_group)

        chapters: list[Chapter] = []
        offset = 0
        while True:
            results = await self._api_get(
                f"/api/v3/comic/{quote(ref)}/group/{quote(group)}/chapters",
                {"limit": CHAPTER_LIMIT, "offset": offset},
            )
            items = results.get("list") if isinstance(results, dict) else None
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                uuid = str(item.get("uuid") or "").strip()
                if not uuid:
                    continue

                chapters.append(
                    Chapter(
                        source_id=self.id,
                        manga_ref=ref,
                        title=str(item.get("name") or f"第{len(chapters) + 1}話").strip(),
                        ref=uuid,
                        index=len(chapters) + 1,
                        group=str(item.get("group_path_word") or group),
                        extra={
                            "path_word": ref,
                            "uuid": uuid,
                            "group": str(item.get("group_path_word") or group),
                            "size": item.get("size"),
                            "ordered": item.get("ordered"),
                            "prev": item.get("prev"),
                            "next": item.get("next"),
                            "datetime_created": item.get("datetime_created"),
                        },
                    )
                )
            total = results.get("total")
            offset += len(items)
            if len(items) < CHAPTER_LIMIT:
                break
            if isinstance(total, int) and offset >= total:
                break

        logger.info("copymanga: %d chapter(s) for %s (group=%s)", len(chapters), ref, group)
        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        extra = chapter.extra or {}
        ref = str(extra.get("path_word") or chapter.manga_ref)
        uuid = str(extra.get("uuid") or chapter.ref)

        payload: Any = None
        last_err: Exception | None = None

        for segment in ("chapter", "chapter2"):
            try:
                payload = await self._api_get(f"/api/v3/comic/{quote(ref)}/{segment}/{quote(uuid)}")
                break
            except Exception as err:
                last_err = err
        if payload is None:
            raise CopyMangaError(f"copymanga: no page payload for chapter {uuid}: {last_err}")

        results = payload if isinstance(payload, dict) else {}
        node = results.get("chapter") if isinstance(results.get("chapter"), dict) else {}
        contents = [
            url
            for url in (
                _httpify(c.get("url")) for c in (node.get("contents") or []) if isinstance(c, dict)
            )
            if url
        ]
        if not contents:
            if results.get("is_lock") or results.get("is_vip"):
                raise ValueError(
                    "copymanga: chapter is locked/VIP (a `token` is required to read it)"
                )
            raise CopyMangaError(f"copymanga: chapter {uuid} returned no pages")

        urls = _reorder(contents, node.get("words")) if self.page_order == "words" else contents
        width = self.image_width
        pages = [
            Page(index=i, image_url=_set_width(url, width) if width else url, referer=None)
            for i, url in enumerate(urls)
        ]
        logger.info("copymanga: %d page(s) for chapter %s", len(pages), uuid)
        return pages

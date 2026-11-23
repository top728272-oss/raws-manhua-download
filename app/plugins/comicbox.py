"""ComicBox / WuWu (18+) source plugin."""

from __future__ import annotations

import asyncio
import hashlib
import html as _html
import io
import logging
import re
from urllib.parse import quote, urlparse

from Crypto.Cipher import AES
from PIL import Image

from app.config import AppConfig
from app.core.fetcher import BaseFetcher
from app.core.htmlutil import strip_chrome
from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

_RELEVANT_CHARS = re.compile(r"[0-9A-Za-z\u3400-\u9fff]")


def _query_relevant(query: str, title: str, min_coverage: float = 0.75) -> bool:
    """True if a search hit plausibly relates to the query.

    ComicBox's search engine is fuzzy: it reports dozens of "results" for any
    string (a garbage query still says "found 21 results"), so hits must be
    filtered by how much of the query actually appears in the title. Comparing
    normalized character SETS at 75% coverage keeps exact hits and
    traditional/simplified variants (秘密教学 vs 秘密教學 = 3/4) while dropping
    fuzzy junk, including latin garbage that happens to share two letters
    ("zzqqwwxx" vs a title containing "wxloli"). Queries with fewer than two
    distinct characters are accepted as-is.
    """
    q = set(_RELEVANT_CHARS.findall(query))
    if len(q) < 2:
        return True
    return len(q & set(_RELEVANT_CHARS.findall(title))) / len(q) >= min_coverage


AES_KEY = b"aaaaaaaaaaaaaaaa"
AES_IV = b"0123456789aaaaaa"
AVIF_HEADER = bytes([0x00, 0x00, 0x00, 0x20, 0x66, 0x74, 0x79, 0x70, 0x61, 0x76, 0x69, 0x66])
JPEG_HEADER = bytes([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01])
PNG_HEADER = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
GIF_HEADER = bytes([0x47, 0x49, 0x46, 0x38, 0x39, 0x61])
CORP_MAP = {0: 44, 1: 48, 2: 52, 3: 56, 4: 60, 5: 64, 6: 68, 7: 72, 8: 76, 9: 80}

_TAG = re.compile(r"<[^>]+>")


def _clean_text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG.sub("", fragment))).strip()


def _clean_ref(query: str) -> str:
    query = query.strip()
    if query.startswith("http://") or query.startswith("https://") or "/" in query:
        parsed = urlparse(query)
        path = parsed.path.strip("/")
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in ("book", "free-chapter"):
            return parts[1]
        if parts:
            return parts[-1]
    return query


def _unpad(data: bytes) -> bytes:
    """Strip PKCS#7 padding from one decrypted chunk."""
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def decrypt_chunks(chunks: list[bytes]) -> bytes:
    """Decrypt every ``.b_N`` chunk as its OWN AES-CBC stream, then concatenate.

    The CDN does not serve one ciphertext cut in half - each chunk is an
    independent AES-CBC stream carrying its own PKCS#7 padding (verified live:
    every chunk's plaintext ends in a valid padding run). Decrypting the
    *concatenated* ciphertext with a single CBC chain therefore garbles
    everything past the first chunk: the first chunk's padding plus a block of
    noise land in the middle of the image, which either kills PIL outright
    ("broken data stream when reading image file") or - worse - silently
    corrupts the page while still decoding.
    """
    out = bytearray()
    for chunk in chunks:
        out += _unpad(AES.new(AES_KEY, AES.MODE_CBC, AES_IV).decrypt(chunk))
    return bytes(out)


def unscramble_image(plain: bytes) -> bytes:
    """Restore the real file header and undo the vertical block shuffle."""
    decrypted = bytearray(plain)
    if len(decrypted) < 8:
        return bytes(decrypted)

    file_type = decrypted[0]
    scramble_type = decrypted[1]
    book_id = decrypted[2] * 256 + decrypted[3]
    page_number = (decrypted[4] << 24) | (decrypted[5] << 16) | (decrypted[6] << 8) | decrypted[7]

    if file_type == 4:
        decrypted[0 : len(AVIF_HEADER)] = AVIF_HEADER
    elif file_type == 0:
        decrypted[0 : len(JPEG_HEADER)] = JPEG_HEADER
    elif file_type == 1:
        decrypted[0 : len(PNG_HEADER)] = PNG_HEADER
    elif file_type == 3:
        decrypted[0 : len(GIF_HEADER)] = GIF_HEADER

    img = Image.open(io.BytesIO(decrypted))
    if scramble_type != 0:
        out_buf = io.BytesIO()
        img.save(out_buf, format=img.format or "JPEG")
        return out_buf.getvalue()

    md5_hash = hashlib.md5(f"{book_id}{page_number}".encode()).hexdigest()
    s = CORP_MAP[ord(md5_hash[-1]) % 10]
    w, h = img.size
    mod = h % s

    unscrambled = Image.new(img.mode, (w, h))
    for block_index in range(s):
        d = h // s
        dest_y = d * block_index
        src_y = h - d * (block_index + 1) - mod
        if block_index == 0:
            d += mod
        else:
            dest_y += mod

        box = (0, src_y, w, src_y + d)
        slice_img = img.crop(box)
        unscrambled.paste(slice_img, (0, dest_y))

    out_buf = io.BytesIO()
    if unscrambled.mode in ("RGBA", "P"):
        unscrambled = unscrambled.convert("RGB")
    unscrambled.save(out_buf, format="JPEG", quality=95)
    return out_buf.getvalue()


def decrypt_and_unscramble_image(data: bytes) -> bytes:
    """Decrypt a single AES-CBC stream and unscramble it.

    The live CDN splits a page into independently-padded chunks, so the fetcher
    goes through :func:`decrypt_chunks` + :func:`unscramble_image`; this helper
    is kept for the plain single-stream shape.
    """
    return unscramble_image(AES.new(AES_KEY, AES.MODE_CBC, AES_IV).decrypt(data))


class ComicBoxFetcher(BaseFetcher):
    def __init__(self, raw_fetcher: BaseFetcher) -> None:
        self._raw = raw_fetcher

    async def get_json(self, url: str, **kwargs) -> any:
        return await self._raw.get_json(url, **kwargs)

    async def post_json(self, url: str, json: any = None, **kwargs) -> any:
        return await self._raw.post_json(url, json=json, **kwargs)

    async def post_text(self, url: str, data: any = None, **kwargs) -> str:
        return await self._raw.post_text(url, data=data, **kwargs)

    async def get_text(self, url: str, **kwargs) -> str:
        return await self._raw.get_text(url, **kwargs)

    async def get_bytes(self, url: str, **kwargs) -> bytes:
        if any(ext in url for ext in (".jpg", ".jpeg", ".png", ".avif", ".webp")) and (
            "/book/" in url or "upload" in url
        ):
            base_part = re.sub(r"\.(jpg|jpeg|png|avif|webp)(\?.*)?$", "", url, flags=re.I)
            b0_url = f"{base_part}.b_0"
            b1_url = f"{base_part}.b_1"
            try:
                c0, c1 = await asyncio.gather(
                    self._raw.get_bytes(b0_url, sniff=False, **kwargs),
                    self._raw.get_bytes(b1_url, sniff=False, **kwargs),
                )
                return unscramble_image(decrypt_chunks([c0, c1]))
            except Exception as exc:
                logger.warning(
                    "ComicBox chunk decrypt failed for %s: %s; falling back to raw fetch", url, exc
                )

        return await self._raw.get_bytes(url, **kwargs)

    async def aclose(self) -> None:
        await self._raw.aclose()


class ComicBoxPlugin(SourcePlugin):
    id = "comicbox"
    name = "ComicBox / WuWu (18+)"
    category = "18+"
    fetch_mode = FetchMode.HTTP

    def __init__(self, cfg: AppConfig, fetcher: BaseFetcher) -> None:
        super().__init__(cfg, fetcher)
        self.fetcher = ComicBoxFetcher(fetcher)

    @property
    def base_url(self) -> str:
        return self.settings.get("base_url", "https://www.comicbox.xyz").rstrip("/")

    @property
    def backup_urls(self) -> list[str]:
        return self.settings.get("backup_urls", ["https://www.wu55comic.world"])

    @property
    def domains(self) -> list[str]:
        candidates = [self.base_url]
        for u in self.backup_urls:
            clean = u.rstrip("/")
            if clean and clean not in candidates:
                candidates.append(clean)
        return candidates

    async def _impersonated_text(self, url: str) -> str:
        from curl_cffi import requests as curl_requests

        proxy = self.cfg.network.proxy or None
        async with curl_requests.AsyncSession(impersonate="chrome", proxy=proxy) as session:
            response = await session.get(url, timeout=30)
            response.raise_for_status()
            return response.text

    async def _fetch_text_with_fallback(self, path: str) -> str:
        last_exc: Exception | None = None
        for domain in self.domains:
            url = f"{domain}{path}"
            try:
                return await self.fetcher.get_text(url)
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "ComicBox domain %s failed: %s; trying TLS impersonation", domain, exc
                )
            try:
                return await self._impersonated_text(url)
            except Exception as exc:
                last_exc = exc
                logger.warning("ComicBox impersonated fetch failed for %s: %s", domain, exc)
        raise RuntimeError(f"All ComicBox domains failed for {path}: {last_exc}")

    async def search(self, query: str) -> list[SearchResult]:
        ref = _clean_ref(query)
        if (
            query.startswith("http://")
            or query.startswith("https://")
            or (ref.isdigit() and len(ref) >= 4)
        ):
            try:
                html = await self._fetch_text_with_fallback(f"/book/{ref}")
                title_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
                title = _clean_text(title_m.group(1)) if title_m else ref
                cover_m = re.search(
                    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                    html,
                    re.I,
                )
                cover = _html.unescape(cover_m.group(1)) if cover_m else None
                return [SearchResult(source_id=self.id, title=title, ref=ref, cover_url=cover)]
            except Exception as err:
                logger.warning("ComicBox direct ref search failed for %s: %s", ref, err)
                return []

        try:
            html = await self._fetch_text_with_fallback(f"/search?keyword={quote(query)}")
            return self._parse_search(html, query)
        except Exception as err:
            logger.warning("ComicBox keyword search failed for '%s': %s", query, err)
            return []

    def _parse_search(self, html: str, query: str = "") -> list[SearchResult]:
        seen: set[str] = set()
        out: list[SearchResult] = []
        html = strip_chrome(html)
        pattern = re.compile(
            r'<a[^>]+href=["\'](?:https?://[^/"\']*)?/book/(\d+)["\'][^>]*>(.*?)</a>', re.I | re.S
        )

        for match in pattern.finditer(html):
            book_id = match.group(1)
            inner = match.group(2)
            if book_id in seen:
                continue
            seen.add(book_id)

            title_el = re.search(
                r'<div[^>]*class=["\']sp-search-card-title["\'][^>]*>(.*?)</div>',
                inner,
                re.I | re.S,
            )
            desc_el = re.search(
                r'<div[^>]*class=["\']sp-search-card-desc["\'][^>]*>(.*?)</div>', inner, re.I | re.S
            )
            alt_m = re.search(r'alt=["\']([^"\']+)["\']', inner, re.I)
            src_m = re.search(r'data-src=["\']([^"\']+)["\']', inner, re.I) or re.search(
                r'src=["\']([^"\']+)["\']', inner, re.I
            )

            if title_el:
                title = _clean_text(title_el.group(1))
            elif alt_m:
                title = _clean_text(alt_m.group(1))
            else:
                title = book_id

            if query and not _query_relevant(query, title):
                continue

            desc = _clean_text(desc_el.group(1)) if desc_el else None
            cover = _html.unescape(src_m.group(1)) if src_m else None
            if cover and not cover.startswith("http"):
                cover = f"{self.base_url}{cover}"

            out.append(
                SearchResult(
                    source_id=self.id,
                    title=title,
                    ref=book_id,
                    cover_url=cover,
                    extra={"description": desc} if desc else {},
                )
            )

        return out[:30]

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        html = await self._fetch_text_with_fallback(f"/book/{result.ref}")
        title_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
        title = _clean_text(title_m.group(1)) if title_m else result.title

        desc_m = re.search(r'<div[^>]*class=["\']content["\'][^>]*>(.*?)</div>', html, re.I | re.S)
        desc = _clean_text(desc_m.group(1)) if desc_m else result.extra.get("description")

        cover_m = re.search(
            r'id=["\']detail_\d+_cover["\'][^>]*data-src=["\']([^"\']+)["\']', html, re.I
        ) or re.search(
            r'class=["\'][^"\']*cropped[^"\']*["\'][^>]*data-src=["\']([^"\']+)["\']', html, re.I
        )
        if cover_m:
            cover = _html.unescape(cover_m.group(1))
        else:
            og_cover = re.search(
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I
            )
            if og_cover:
                og_url = _html.unescape(og_cover.group(1))
                cover = re.sub(r"/cover\.jpg$", "/cover_pc.jpg", og_url, flags=re.I)
            else:
                cover = result.cover_url

        author_m = re.search(r'<div[^>]*class=["\']author["\'][^>]*>(.*?)</div>', html, re.I | re.S)
        author = _clean_text(author_m.group(1)) if author_m else None

        return MangaDetail(
            source_id=self.id,
            title=title,
            ref=result.ref,
            cover_url=cover,
            description=desc,
            author=author,
        )

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        html = await self._fetch_text_with_fallback(f"/book/{detail.ref}")
        pattern = re.compile(
            r'<a[^>]+href=["\'](/free-chapter/(\d+)[^"\']*)["\'][^>]*>(.*?)</a>', re.I | re.S
        )
        chapters: list[Chapter] = []
        seen: set[str] = set()

        for match in pattern.finditer(html):
            path = match.group(1)
            ch_id = match.group(2)
            if ch_id in seen:
                continue
            seen.add(ch_id)
            ch_title = _clean_text(match.group(3)).replace("▶", "").strip()
            chapters.append(
                Chapter(
                    source_id=self.id,
                    manga_ref=detail.ref,
                    title=ch_title or f"Chapter {ch_id}",
                    ref=ch_id,
                    index=0,
                    extra={"path": path, "sort": int(ch_id)},
                )
            )

        chapters.sort(key=lambda ch: ch.extra["sort"])
        for position, ch in enumerate(chapters, start=1):
            ch.index = position

        return chapters

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        path = chapter.extra.get("path") or f"/free-chapter/{chapter.ref}"
        html = await self._fetch_text_with_fallback(path)
        pattern = re.compile(r'<div[^>]*id=["\'](\d+)["\'][^>]*data-src=["\']([^"\']+)["\']', re.I)
        matches = pattern.findall(html)
        pages: list[Page] = []

        for idx, (_page_id, data_src) in enumerate(matches):
            pages.append(
                Page(
                    index=idx,
                    image_url=data_src,
                    referer=f"{self.base_url}/",
                )
            )

        return pages

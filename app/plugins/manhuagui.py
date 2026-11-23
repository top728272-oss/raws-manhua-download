"""ManhuaGui (www.manhuagui.com) source plugin."""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import quote, urlencode, urljoin, urlparse

from app.core.models import Chapter, FetchMode, MangaDetail, Page, SearchResult
from app.plugins.base import SourcePlugin

logger = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]+>")
_ATTR = re.compile(r"([:\w-]+)\s*=\s*([\"'])(.*?)\2", re.I | re.S)
_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
_META = re.compile(r"<meta\b([^>]*)>", re.I | re.S)
_RESULT = re.compile(
    r"<li\b[^>]*class=[\"'][^\"']*\bcf\b[^\"']*[\"'][^>]*>(.*?)</li>",
    re.I | re.S,
)
_CHAPTER_LIST = re.compile(
    r"<div\b[^>]*id=[\"']chapter-list-\d+[\"'][^>]*>(.*?)</div>", re.I | re.S
)
_CHAPTER_HREF = re.compile(r"^/comic/(\d+)/(\d+)\.html(?:\?.*)?$", re.I)
_READER_FUNCTION = "function(p,a,c,k,e,d)"
_PACK_ARGUMENTS = re.compile(r"',(?P<base>\d+),(?P<count>\d+),'", re.S)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", fragment)).strip()


def _attrs(fragment: str) -> dict[str, str]:
    return {name.lower(): value for name, _, value in _ATTR.findall(fragment)}


def _meta_value(html: str, key: str) -> str | None:
    key = key.lower()
    for match in _META.finditer(html):
        attrs = _attrs(match.group(1))
        if attrs.get("name", "").lower() == key or attrs.get("property", "").lower() == key:
            return attrs.get("content")
    return None


def _clean_ref(value: str) -> str:
    value = value.strip()
    if value.startswith(("http://", "https://", "/")):
        parsed = urlparse(value)
        parts = [part for part in parsed.path.split("/") if part]
        if "comic" in parts:
            pos = parts.index("comic")
            if len(parts) > pos + 1:
                return parts[pos + 1]
        return parts[-1] if parts else value
    return value.rstrip("/")


def _cover_url(base_url: str, value: str | None) -> str | None:
    if not value:
        return None
    return urljoin(base_url + "/", value)


def _token_name(value: int, base: int = 62) -> str:
    prefix = "" if value < base else _token_name(value // base, base)
    value %= base
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    return prefix + (chr(value + 29) if value > 35 else digits[value])


def _decompress_lz_base64(value: str) -> str | None:
    """Decode the LZString decompressFromBase64 variant used by ManhuaGui."""

    if value == "":
        return None
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="

    def get_next(index: int) -> int:
        char = value[index] if index < len(value) else "="
        return alphabet.find(char)

    dictionary: list[str | int] = [0, 1, 2]
    result: list[str] = []
    enlarge_in = 4
    dict_size = 4
    num_bits = 3
    data_val = get_next(0)
    data_position = 32
    data_index = 1

    def read_bits(count: int) -> int:
        nonlocal data_val, data_position, data_index
        bits = 0
        power = 1
        max_power = 1 << count
        while power != max_power:
            resb = data_val & data_position
            data_position >>= 1
            if data_position == 0:
                data_position = 32
                data_val = get_next(data_index)
                data_index += 1
            if resb > 0:
                bits |= power
            power <<= 1
        return bits

    initial = read_bits(2)
    if initial == 0:
        first = chr(read_bits(8))
    elif initial == 1:
        first = chr(read_bits(16))
    else:
        return ""

    dictionary.append(first)
    word = first
    result.append(first)

    while True:
        if data_index > len(value):
            return ""
        code = read_bits(num_bits)
        if code == 0:
            dictionary.append(chr(read_bits(8)))
            code = dict_size
            dict_size += 1
            enlarge_in -= 1
        elif code == 1:
            dictionary.append(chr(read_bits(16)))
            code = dict_size
            dict_size += 1
            enlarge_in -= 1
        elif code == 2:
            return "".join(result)

        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1

        if code < len(dictionary) and dictionary[code]:
            entry = str(dictionary[code])
        elif code == dict_size:
            entry = word + word[0]
        else:
            return None

        result.append(entry)
        dictionary.append(word + entry[0])
        dict_size += 1
        enlarge_in -= 1
        word = entry
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1


def _unpack_reader_script(html: str) -> dict:
    """Statically decode the packed SMH.imgData object from a reader page."""

    function_start = html.find(_READER_FUNCTION)
    if function_start < 0:
        raise ValueError("ManhuaGui reader payload is missing its unpacker")

    pack_start = html.find("}('", function_start)
    if pack_start < 0:
        raise ValueError("ManhuaGui reader payload has no packed data")
    payload_start = pack_start + 3
    pack_match = _PACK_ARGUMENTS.search(html, payload_start)
    if not pack_match:
        raise ValueError("ManhuaGui reader payload has invalid pack arguments")
    payload_end = pack_match.start()
    base = int(pack_match.group("base"))
    token_count = int(pack_match.group("count"))

    dictionary_start = pack_match.end()
    dictionary_end = html.find("'[", dictionary_start)
    if dictionary_end < 0:
        raise ValueError("ManhuaGui reader payload has no compressed dictionary")

    packed_code = html[payload_start:payload_end]
    compressed_dictionary = html[dictionary_start:dictionary_end]
    dictionary_text = _decompress_lz_base64(compressed_dictionary)
    if not dictionary_text:
        raise ValueError("ManhuaGui reader dictionary could not be decompressed")

    dictionary = dictionary_text.split("|")
    unpacked = packed_code
    for index in range(token_count - 1, -1, -1):
        if index >= len(dictionary) or not dictionary[index]:
            continue
        token = re.escape(_token_name(index, base))
        unpacked = re.sub(rf"\b{token}\b", dictionary[index], unpacked)

    object_start = unpacked.find("SMH.imgData(")
    if object_start < 0:
        raise ValueError("ManhuaGui reader payload did not contain SMH.imgData")
    object_start += len("SMH.imgData(")
    try:
        data, _ = json.JSONDecoder().raw_decode(unpacked[object_start:])
    except json.JSONDecodeError as err:
        raise ValueError("ManhuaGui reader SMH.imgData is malformed") from err
    if not isinstance(data, dict):
        raise ValueError("ManhuaGui reader SMH.imgData is not an object")
    return data


class ManhuaGuiPlugin(SourcePlugin):
    id = "manhuagui"
    name = "ManhuaGui"
    category = "General"
    fetch_mode = FetchMode.HTTP

    @property
    def base_url(self) -> str:
        return self.settings.get("base_url", "https://www.manhuagui.com").rstrip("/")

    @property
    def image_host(self) -> str:
        return self.settings.get("image_host", "https://eu1.hamreus.com").rstrip("/")

    async def search(self, query: str) -> list[SearchResult]:
        query = query.strip()
        ref = _clean_ref(query)
        if query.startswith(("http://", "https://", "/comic/")) and ref.isdigit():
            if "manhuagui.com" in query or query.startswith("/comic/"):
                try:
                    html = await self.fetcher.get_text(f"{self.base_url}/comic/{ref}/")
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
                except Exception:
                    return []
            return []

        try:
            html = await self.fetcher.get_text(f"{self.base_url}/s/{quote(query, safe='')}.html")
            return self._parse_search(html)
        except Exception as err:
            logger.warning("manhuagui search failed: %s", err)
            return []

    def _parse_search(self, html: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        seen: set[str] = set()

        blocks = _RESULT.findall(html)
        for block in blocks:
            for attrs_text, inner in _ANCHOR.findall(block):
                attrs = _attrs(attrs_text)
                href = attrs.get("href", "")
                match = re.match(r"^(?:https?://[^/]+)?/comic/(\d+)/?$", href, re.I)
                if not match:
                    continue
                ref = match.group(1)
                if ref in seen:
                    continue
                seen.add(ref)

                image = re.search(r"<img\b([^>]*)>", block, re.I | re.S)
                image_attrs = _attrs(image.group(1)) if image else {}
                title_match = re.search(r"<dt\b[^>]*>\s*<a\b[^>]*>(.*?)</a>", block, re.I | re.S)
                title = _text(title_match.group(1)) if title_match else attrs.get("title", "")
                if not title:
                    title = image_attrs.get("alt", "") or _text(inner) or ref

                author_match = re.search(r"作者[：:]\s*.*?<a\b[^>]*>(.*?)</a>", block, re.I | re.S)
                author = _text(author_match.group(1)) if author_match else None
                results.append(
                    SearchResult(
                        source_id=self.id,
                        title=title,
                        ref=ref,
                        cover_url=_cover_url(
                            self.base_url,
                            image_attrs.get("data-src") or image_attrs.get("src"),
                        ),
                        author=author,
                    )
                )
        return results[:20]

    async def get_detail(self, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        html = await self.fetcher.get_text(f"{self.base_url}/comic/{ref}/")
        return self._parse_detail(html, result)

    def _parse_detail(self, html: str, result: SearchResult) -> MangaDetail:
        ref = _clean_ref(result.ref)
        h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", html, re.I | re.S)
        title = _text(h1.group(1)) if h1 else ""
        if not title:
            title = _meta_value(html, "og:title") or _meta_value(html, "title") or result.title
        title = re.sub(r"\s+-\s*看漫画\s*$", "", title).strip()
        title = re.sub(r"漫画\s*$", "", title).strip() or ref

        author_match = re.search(r"作者[：:]\s*.*?<a\b[^>]*>(.*?)</a>", html, re.I | re.S)
        status_match = re.search(r"状态[：:]\s*.*?<span[^>]*>(.*?)</span>", html, re.I | re.S)
        description = _meta_value(html, "description")
        if not description:
            intro = re.search(
                r"<dd\b[^>]*class=[\"'][^\"']*intro[^\"']*[\"'][^>]*>(.*?)</dd>",
                html,
                re.I | re.S,
            )
            description = _text(intro.group(1)) if intro else None

        cover = _meta_value(html, "og:image") or f"//cf.mhgui.com/cpic/h/{ref}.jpg"
        chapters = self._chapter_links(html, ref)
        return MangaDetail(
            source_id=self.id,
            title=title,
            ref=ref,
            cover_url=_cover_url(self.base_url, cover),
            author=_text(author_match.group(1)) if author_match else result.author,
            description=description.strip() if description else None,
            status=_text(status_match.group(1)) if status_match else None,
            chapter_count=len(chapters) or None,
            extra={"detail_html": html},
        )

    def _chapter_links(self, html: str, manga_ref: str) -> list[tuple[str, str, int | None]]:
        found: dict[str, tuple[str, int | None]] = {}
        for attrs_text, inner in _ANCHOR.findall(html):
            attrs = _attrs(attrs_text)
            href = attrs.get("href", "").split("#", 1)[0]
            match = _CHAPTER_HREF.match(href)
            if not match or match.group(1) != manga_ref:
                continue
            chapter_ref = match.group(2)
            label = attrs.get("title") or _text(inner) or chapter_ref
            page_match = re.search(r"(\d+)\s*p\b", inner, re.I)
            found.setdefault(
                chapter_ref, (label.strip(), int(page_match.group(1)) if page_match else None)
            )
        return [
            (ref, label, pages)
            for ref, (label, pages) in sorted(found.items(), key=lambda item: int(item[0]))
        ]

    async def list_chapters(self, detail: MangaDetail) -> list[Chapter]:
        ref = _clean_ref(detail.ref)
        html = detail.extra.get("detail_html")
        if not html:
            html = await self.fetcher.get_text(f"{self.base_url}/comic/{ref}/")
        raw = self._chapter_links(html, ref)
        return [
            Chapter(
                source_id=self.id,
                manga_ref=ref,
                title=title,
                ref=chapter_ref,
                index=index,
                extra={"href": f"/comic/{ref}/{chapter_ref}.html", "page_count": page_count},
            )
            for index, (chapter_ref, title, page_count) in enumerate(raw, start=1)
        ]

    def _parse_pages(self, html: str) -> list[Page]:
        data = _unpack_reader_script(html)
        files = data.get("files")
        path = data.get("path")
        signature = data.get("sl")
        if not isinstance(files, list) or not files:
            raise ValueError("ManhuaGui reader contained no image files")
        if not isinstance(path, str) or not path:
            raise ValueError("ManhuaGui reader contained no image path")
        if not isinstance(signature, dict) or not signature.get("e") or not signature.get("m"):
            raise ValueError("ManhuaGui reader contained no image signature")
        try:
            if int(signature["e"]) <= int(time.time()):
                raise ValueError("ManhuaGui reader image signature expired")
        except (TypeError, ValueError) as err:
            if "expired" in str(err):
                raise
            raise ValueError("ManhuaGui reader image expiry is malformed") from err

        query = urlencode({"e": signature["e"], "m": signature["m"]})
        pages: list[Page] = []
        for file_name in files:
            if not isinstance(file_name, str) or not file_name:
                raise ValueError("ManhuaGui reader contained a malformed image file")
            image_url = f"{urljoin(self.image_host + '/', path.lstrip('/'))}{file_name}?{query}"
            pages.append(Page(index=len(pages), image_url=image_url, referer=f"{self.base_url}/"))
        return pages

    async def get_pages(self, chapter: Chapter) -> list[Page]:
        href = chapter.extra.get("href") or f"/comic/{chapter.manga_ref}/{chapter.ref}.html"
        html = await self.fetcher.get_text(urljoin(self.base_url + "/", href))
        return self._parse_pages(html)

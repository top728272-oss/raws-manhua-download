"""Output writers: turn downloaded page images into the user's chosen formats.

Supports (user-selectable, multiple at once):
  - FOLDER : raw images on disk   downloads/{source}/{series}/{chapter}/001.webp
  - CBZ    : one .cbz per chapter

Page filenames are `{position + 1:03d}.{ext}`, so a page that never downloaded
leaves a visible hole in the numbering rather than silently shifting every later
page down one.

Alongside the images each chapter gets a manifest (`.manhua-dl.json`, see
``app.core.library``) recording the expected page count and which pages are
missing. That is what lets a re-run skip a chapter that is already complete, and
lets the engine go back for individual missing pages after the run.

Image conversion (WebP -> jpg/png) is honored when config.keep_webp is False.
Per-run timestamped folders are intentionally deferred (see DESIGN.md §6).
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path

from app.config import AppConfig
from app.core.library import (
    STATE_DIR,
    ChapterManifest,
    artifacts_present,
    read_manifest,
    write_manifest,
)
from app.core.models import Chapter, OutputFormat

logger = logging.getLogger(__name__)

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


_PAGE_FILE = re.compile(r"^\d{3,}\.(?:webp|jpe?g|png|gif|bmp|avif)$", re.IGNORECASE)


def safe_name(name: str, fallback: str = "untitled") -> str:
    cleaned = _INVALID.sub("_", (name or "").strip()).rstrip(". ")
    return cleaned or fallback


_NUM_SEPS = r"[\s.、，,：:：\-\u2014_)）」』】〉》］\]（(「『｛<~～=＝]"


_PLAIN_NUM_SEPS = r"[\s、，,：:：\-\u2014_)」』화]"


_CJK_WORD = r"[぀-ヿ㐀-䶿一-鿿가-힯　-〿＀-￯]"
_NUMBER = r"\d{1,5}(?:\.\d{1,2})?"


_GLUED_NUM = r"(?:0\d{1,4}(?:\.\d{1,2})?|[1-9]\d{1,2}(?:\.\d{1,2})?)"


_NUM_END = r"(?=$|" + _NUM_SEPS + r"|" + _CJK_WORD + r")"

_CHAPTER_NUM_PREFIX = re.compile(
    r"^\s*(?:"
    + r"第\s*(?P<cjk>"
    + _NUMBER
    + r")\s*[話话章卷集화]?(?![話话章卷集화])"
    + _NUM_END
    + r"|chapter\s*(?P<en>"
    + _NUMBER
    + r")"
    + _NUM_END
    + r"|ch\.?\s*(?P<ch>"
    + _NUMBER
    + r")"
    + _NUM_END
    + r"|(?P<plain>"
    + _NUMBER
    + r")(?=$|"
    + _PLAIN_NUM_SEPS
    + r")"
    + r"|(?P<glued>"
    + _GLUED_NUM
    + r")(?="
    + _CJK_WORD
    + r")"
    + r")",
    re.IGNORECASE,
)


def _has_leading_chapter_number(title: str) -> bool:
    """True if ``title`` opens with a site-style chapter number.

    Covers the conventions observed across the source plugins: 第N话/話/章
    (eighteenmhx, comicbox, dumanwu, wmh1234, one99mh, manwang),
    第N卷 (manhuagui volumes), Korean화 (hanman18), "Chapter N", "Ch. N" and
    bare leading numbers "01 起点" / "071 - 标题" / "1.5 特别篇" - with or
    without decimals, separated or written straight against the title
    ("085当心姐姐动了胎气"). Content numbers never match: "1秒后末日",
    "1401的真相" (digits glued to text but neither padded nor short),
    "001.webp" (dot not followed by digits).
    """
    return _CHAPTER_NUM_PREFIX.match(title.strip()) is not None


def leading_chapter_number(title: str) -> str | None:
    """Return the site-style chapter number a title opens with, else ``None``.

    The same matcher as :func:`_has_leading_chapter_number`, but returning the
    number itself ("第12話 序" -> ``"12"``, "Ch. 3.5" -> ``"3.5"``,
    "085当心姐姐动了胎气" -> ``"085"``). Used to line chapters up across
    sources during a cross-source fallback, where the two sites name the same
    chapter differently but agree on its number.
    """
    match = _CHAPTER_NUM_PREFIX.match((title or "").strip())
    if not match:
        return None
    return (
        match.group("cjk")
        or match.group("en")
        or match.group("ch")
        or match.group("plain")
        or match.group("glued")
    )


def chapter_name(chapter: Chapter) -> str:
    """Chapter folder/file name; index only titles that carry no number.

    If the site's own title leads with a chapter number ("第1话：诡秘世界",
    "第0话 预告", "第105卷", "1.5 特别篇"), it is trusted verbatim - sites
    number chapters however they like (dumanwu starts at 第0话, so its numbers
    sit one below the reading-order index and a strict agreement check would
    misfire on every chapter). Only numberless titles get the zero-padded
    reading-order index ("诡秘世界" -> "0001 - 诡秘世界"), which keeps
    Explorer sort order stable there. ``Chapter.index`` is the 1-based
    position every source plugin assigns. Mirrors the policy already used by
    extension/plugins/guazimanhua.js.
    """
    index = max(int(chapter.index or 0), 0)
    title = (chapter.title or "").strip()
    if not title:
        return f"chapter_{index:04d}"
    if _has_leading_chapter_number(title):
        return title
    return f"{index:04d} - {title}"


class OutputWriter:
    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._root = cfg.output_path

    def chapter_dir(self, source_id: str, series_title: str, chapter: Chapter) -> Path:
        return (
            self._root
            / safe_name(source_id)
            / safe_name(series_title)
            / safe_name(chapter_name(chapter), fallback=f"chapter_{chapter.index:04d}")
        )

    def manifest_file(self, source_id: str, series_title: str, chapter: Chapter) -> Path:
        """Where this chapter's manifest lives.

        Kept in a parallel `.manhua-dl/` tree rather than inside the chapter
        folder, so a downloaded library contains nothing but the pages.
        """
        return (
            self._root
            / STATE_DIR
            / safe_name(source_id)
            / safe_name(series_title)
            / f"{safe_name(chapter_name(chapter), fallback=f'chapter_{chapter.index:04d}')}.json"
        )

    def read_manifest(
        self, source_id: str, series_title: str, chapter: Chapter
    ) -> ChapterManifest | None:
        return read_manifest(self.manifest_file(source_id, series_title, chapter))

    def completeness(self, source_id: str, series_title: str, chapter: Chapter) -> tuple[bool, str]:
        """``(True, "complete")`` or ``(False, reason)`` for this chapter.

        The reason is one of ``"manifest missing"``, ``"manifest incomplete"`` or
        ``"files missing"``. Callers log it, so a re-download can always be traced
        back to *why* skip-existing did not fire - from the outside an absent
        manifest and a half-finished chapter otherwise look identical.
        """
        manifest = read_manifest(self.manifest_file(source_id, series_title, chapter))
        if manifest is None:
            return False, "manifest missing"
        if not manifest.complete:
            return False, "manifest incomplete"
        chapter_dir = self.chapter_dir(source_id, series_title, chapter)
        if not artifacts_present(chapter_dir, manifest):
            logger.info(
                "Chapter %s has a complete manifest but files are missing; re-downloading",
                chapter.title,
            )
            return False, "files missing"
        return True, "complete"

    def is_complete(self, source_id: str, series_title: str, chapter: Chapter) -> bool:
        """True when this chapter is already fully on disk.

        Requires a manifest that claims completeness **and** every artifact it
        names to still be present, so deleting a page by hand makes the chapter
        downloadable again instead of being silently skipped.
        """
        return self.completeness(source_id, series_title, chapter)[0]

    def _maybe_convert(self, data: bytes, page_index: int) -> tuple[bytes, str]:
        if self._cfg.keep_webp:
            return data, "webp"
        try:
            from PIL import Image

            ext = self._cfg.convert_to.lower()
            fmt = "JPEG" if ext in ("jpg", "jpeg") else ext.upper()
            with Image.open(io.BytesIO(data)) as im:
                if fmt == "JPEG" and im.mode in ("RGBA", "P"):
                    im = im.convert("RGB")
                buf = io.BytesIO()
                im.save(buf, format=fmt)
                return buf.getvalue(), ("jpg" if ext == "jpeg" else ext)
        except Exception as exc:
            logger.warning("Image convert failed (page %d): %s; keeping original.", page_index, exc)
            return data, "webp"

    def _clean_page_files(self, chapter_dir: Path) -> None:
        """Drop previously written page files so a rewrite leaves no strays.

        A re-download that recovers *more* pages than last time would otherwise
        sit next to stale higher-numbered files from the earlier attempt.
        """
        if not chapter_dir.is_dir():
            return
        for path in chapter_dir.iterdir():
            if path.is_file() and _PAGE_FILE.match(path.name):
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning("Could not remove stale page %s: %s", path, exc)

    def write(
        self,
        formats: list[OutputFormat],
        source_id: str,
        series_title: str,
        chapter: Chapter,
        pages: list[tuple[int, bytes]],
        expected_pages: int | None = None,
        fallback_source_id: str | None = None,
    ) -> int:
        """Write `pages` in every requested format; return the pages written.

        `pages` is ``(position, bytes)``, where position is the 0-based index of
        the page within the source's page list - that is what makes a missing
        page leave a hole in the numbering.

        `expected_pages` is the source's total page count; anything absent is
        recorded in the manifest as missing. A chapter with **no pages is never
        written** - no empty folder, no empty .cbz - and 0 is returned so the
        caller reports the chapter as failed instead of pretending it succeeded.
        """
        if not pages:
            logger.error(
                "Refusing to write a 0-image chapter: %s / %s / %s",
                source_id,
                series_title,
                chapter_name(chapter),
            )
            return 0

        chapter_dir = self.chapter_dir(source_id, series_title, chapter)
        prepared = [(idx, *self._maybe_convert(data, idx)) for idx, data in sorted(pages)]
        if not prepared:
            logger.error(
                "Refusing to write a chapter with 0 usable pages: %s / %s / %s",
                source_id,
                series_title,
                chapter_name(chapter),
            )
            return 0

        expected = max(int(expected_pages or len(prepared)), len(prepared))
        saved = [idx for idx, _data, _ext in prepared]
        files: list[str] = []

        if OutputFormat.FOLDER in formats:
            files += self._write_folder(chapter_dir, prepared, clean=True)
        if OutputFormat.CBZ in formats:
            files += self._write_cbz(chapter_dir, chapter, prepared)

        manifest = ChapterManifest(
            source_id=source_id,
            chapter_ref=chapter.ref,
            series=series_title,
            chapter=chapter.title,
            pages=expected,
            saved=saved,
            missing=[i for i in range(expected) if i not in set(saved)],
            files=files,
            fallback_source_id=fallback_source_id,
        )
        write_manifest(self.manifest_file(source_id, series_title, chapter), manifest)
        return len(prepared)

    def add_pages(
        self,
        formats: list[OutputFormat],
        source_id: str,
        series_title: str,
        chapter: Chapter,
        pages: list[tuple[int, bytes]],
        fallback_source_id: str | None = None,
    ) -> int:
        """Merge extra pages into an already-written chapter.

        Used by the engine's end-of-run recovery pass: only the missing pages are
        re-fetched, so they have to slot in beside the ones already on disk
        without disturbing them. Returns the number of pages added.
        """
        if not pages:
            return 0

        chapter_dir = self.chapter_dir(source_id, series_title, chapter)
        prepared = [(idx, *self._maybe_convert(data, idx)) for idx, data in sorted(pages)]
        added: list[str] = []

        if OutputFormat.FOLDER in formats:
            added += self._write_folder(chapter_dir, prepared, clean=False)
        if OutputFormat.CBZ in formats:
            added += self._merge_cbz(chapter_dir, chapter, prepared)

        manifest_file = self.manifest_file(source_id, series_title, chapter)
        manifest = read_manifest(manifest_file)
        if manifest is None:
            manifest = ChapterManifest(
                source_id=source_id,
                chapter_ref=chapter.ref,
                series=series_title,
                chapter=chapter.title,
                pages=max(idx for idx, _d, _e in prepared) + 1,
            )
        manifest.saved = sorted(set(manifest.saved) | {idx for idx, _d, _e in prepared})
        manifest.missing = [i for i in range(manifest.pages) if i not in set(manifest.saved)]
        manifest.files = sorted(set(manifest.files) | set(added))
        if fallback_source_id:
            manifest.fallback_source_id = fallback_source_id
        write_manifest(manifest_file, manifest)

        logger.info(
            "Merged %d recovered page(s) into %s (%d/%d now present)",
            len(prepared),
            chapter_dir,
            len(manifest.saved),
            manifest.pages,
        )
        return len(prepared)

    def _write_folder(
        self, chapter_dir: Path, prepared: list[tuple[int, bytes, str]], clean: bool
    ) -> list[str]:
        if clean:
            self._clean_page_files(chapter_dir)
        chapter_dir.mkdir(parents=True, exist_ok=True)
        names: list[str] = []
        for idx, data, ext in prepared:
            name = f"{idx + 1:03d}.{ext}"
            (chapter_dir / name).write_bytes(data)
            names.append(name)
        logger.info("FOLDER written: %s (%d pages)", chapter_dir, len(prepared))
        return names

    def _write_cbz(
        self, chapter_dir: Path, chapter: Chapter, prepared: list[tuple[int, bytes, str]]
    ) -> list[str]:
        chapter_dir.mkdir(parents=True, exist_ok=True)
        cbz_path = chapter_dir / f"{safe_name(chapter_name(chapter))}.cbz"
        with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_STORED) as zf:
            for idx, data, ext in prepared:
                zf.writestr(f"{idx + 1:03d}.{ext}", data)
        logger.info("CBZ written: %s (%d pages)", cbz_path, len(prepared))
        return [cbz_path.name]

    def _merge_cbz(
        self, chapter_dir: Path, chapter: Chapter, prepared: list[tuple[int, bytes, str]]
    ) -> list[str]:
        """Rewrite the chapter's CBZ with the new pages slotted in."""
        chapter_dir.mkdir(parents=True, exist_ok=True)
        cbz_path = chapter_dir / f"{safe_name(chapter_name(chapter))}.cbz"
        entries: dict[str, bytes] = {}
        if cbz_path.is_file():
            try:
                with zipfile.ZipFile(cbz_path) as zf:
                    for name in zf.namelist():
                        entries[name] = zf.read(name)
            except Exception as exc:
                logger.warning("Could not read existing CBZ %s: %s; rebuilding", cbz_path, exc)
                entries = {}
        for idx, data, ext in prepared:
            entries[f"{idx + 1:03d}.{ext}"] = data
        with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_STORED) as zf:
            for name in sorted(entries):
                zf.writestr(name, entries[name])
        logger.info("CBZ merged: %s (%d entries)", cbz_path, len(entries))
        return [cbz_path.name]

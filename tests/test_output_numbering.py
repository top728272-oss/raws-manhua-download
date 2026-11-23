"""Tests for sort-friendly output naming and per-plugin reading-order normalization."""

from __future__ import annotations

import zipfile
from unittest.mock import AsyncMock

import pytest

from app.config import AppConfig
from app.core.models import Chapter, MangaDetail
from app.core.output import OutputWriter, chapter_name, leading_chapter_number
from app.plugins.comicbox import ComicBoxPlugin
from app.plugins.eighteenmhx import EighteenMHXPlugin
from app.plugins.manhuafree import ManhuaFreePlugin
from app.plugins.wmh1234 import Wmh1234Plugin

# ---------------------------------------------------------------------------
# Output naming
# ---------------------------------------------------------------------------


def test_chapter_name_prefixes_zero_padded_index() -> None:
    ch = Chapter(source_id="x", manga_ref="m", title="青梅竹马", ref="r", index=71)
    assert chapter_name(ch) == "0071 - 青梅竹马"


def test_chapter_name_keeps_numbered_site_title_untouched() -> None:
    # A leading chapter number that agrees with the reading order means the
    # site already names its chapters - keep the title verbatim, no re-index.
    cases = [
        ("第1话：诡秘世界", 1),
        ("第71话 青梅竹马", 71),
        ("第12話 魂環", 12),
        ("01 起点", 1),
        ("037 起始与终结", 37),
        ("071 - 青梅竹马", 71),
        ("第1话 001 苍天啊！天崩开局！", 1),
        ("Chapter 5: 比酒", 5),
    ]
    for title, index in cases:
        ch = Chapter(source_id="x", manga_ref="m", title=title, ref="r", index=index)
        assert chapter_name(ch) == title, title


def test_chapter_name_trusts_site_number_even_when_divergent() -> None:
    # A numbered site title is authoritative — dumanwu numbers from 第0话, so
    # its numbers sit one below reading order; they must not be re-indexed.
    cases = [
        ("第0话 预告", 1),
        ("第1话 进门就这么刺激吗_", 2),
        ("第2话 番外篇", 4),
    ]
    for title, index in cases:
        ch = Chapter(source_id="x", manga_ref="m", title=title, ref="r", index=index)
        assert chapter_name(ch) == title, title


def test_chapter_name_does_not_mangle_non_chapter_numbers() -> None:
    # "1秒" / "1401" glued to text are content, not chapter numbers — the
    # title must still get the index prefix.
    cases = [("1秒后末日", 1), ("1401的真相", 8), ("2019年的夏天", 3), ("100%甜", 2)]
    for title, index in cases:
        ch = Chapter(source_id="x", manga_ref="m", title=title, ref="r", index=index)
        assert chapter_name(ch) == f"{index:04d} - {title}", title


def test_chapter_name_accepts_number_glued_to_the_title() -> None:
    # Some sites never put a separator after the number: "085当心姐姐动了胎气".
    # The number is zero-padded (or short), so it is the site's own chapter
    # counter and must be trusted — prepending the reading-order index here
    # produced the doubled "0086 - 085当心姐姐动了胎气".
    cases = [
        ("085当心姐姐动了胎气", 86),
        ("086比谁更禽兽？来啊！", 87),
        ("087兽兽出击！", 88),
        ("85当心姐姐", 85),
        ("0086兽兽出击", 86),
    ]
    for title, index in cases:
        ch = Chapter(source_id="x", manga_ref="m", title=title, ref="r", index=index)
        assert chapter_name(ch) == title, title


def test_chapter_number_ignores_trailing_site_noise() -> None:
    # 第N话 is an explicit declaration, so a separated trailing run is noise:
    # "第225话 223 愿赌服输" numbers from 225.
    assert leading_chapter_number("第225话 223 愿赌服输") == "225"
    assert leading_chapter_number("第226话 224 准备飞升") == "226"
    assert leading_chapter_number("085当心姐姐动了胎气") == "085"
    assert leading_chapter_number("1401的真相") is None
    assert leading_chapter_number("1秒后末日") is None


def test_chapter_name_leaves_unglued_trailing_number_unclaimed() -> None:
    # Without a separator ("第225话223…") a trailing number could be the site's
    # real counter, so the matcher must not claim 第N话 is authoritative. The
    # title still gets a stable, sortable index prefix instead.
    ch = Chapter(source_id="x", manga_ref="m", title="第225话223愿赌服输", ref="r", index=225)
    assert chapter_name(ch) == "0225 - 第225话223愿赌服输"
    assert leading_chapter_number("第225话223愿赌服输") is None


def test_chapter_name_recognizes_quoted_and_long_numbers() -> None:
    # Separators after 第N话 that the old set missed, and >4-digit chapters.
    for title in ("第225话「引」", "第225话（", "第225话【", "第12345话"):
        ch = Chapter(source_id="x", manga_ref="m", title=title, ref="r", index=225)
        assert chapter_name(ch) == title, title


def test_chapter_name_recognizes_volumes_and_decimals() -> None:
    # manhuagui lists volumes as 第N卷; specials appear as 第1.5话 / 1.5 特别篇.
    # All carry a site number, so all stay verbatim.
    cases = [
        ("第105卷", 1),
        ("第1.5话 特别篇", 1),
        ("1.5 特别篇", 1),
        ("第12.5話", 12),
        ("1화", 1),
    ]
    for title, index in cases:
        ch = Chapter(source_id="x", manga_ref="m", title=title, ref="r", index=index)
        assert chapter_name(ch) == title, title


def test_chapter_name_empty_title_falls_back() -> None:
    ch = Chapter(source_id="x", manga_ref="m", title="", ref="r", index=25)
    assert chapter_name(ch) == "chapter_0025"


def _writer(tmp_path) -> OutputWriter:
    return OutputWriter(AppConfig(output_dir=str(tmp_path), keep_webp=True))


def _chapter(index: int, title: str) -> Chapter:
    return Chapter(source_id="dumanwu", manga_ref="series", title=title, ref="ref", index=index)


def test_write_folder_uses_numbered_name(tmp_path) -> None:
    writer = _writer(tmp_path)
    ch = _chapter(7, "危机四伏")
    writer.write(["folder"], "dumanwu", "系列", ch, [(0, b"img1"), (1, b"img2")])

    chapter_dir = tmp_path / "dumanwu" / "系列" / "0007 - 危机四伏"
    assert chapter_dir.is_dir()
    assert sorted(p.name for p in chapter_dir.iterdir()) == ["001.webp", "002.webp"]


def test_write_folder_keeps_glued_site_number(tmp_path) -> None:
    # End-to-end: the folder written for a site that glues its number to the
    # title must be the site's own name, not a doubled index-prefixed one.
    writer = _writer(tmp_path)
    ch = _chapter(86, "085当心姐姐动了胎气")
    writer.write(["folder"], "one99mh", "姐姐的日常", ch, [(0, b"img1"), (1, b"img2")])

    chapter_dir = tmp_path / "one99mh" / "姐姐的日常" / "085当心姐姐动了胎气"
    assert chapter_dir.is_dir()
    assert sorted(p.name for p in chapter_dir.iterdir()) == ["001.webp", "002.webp"]


def test_write_cbz_uses_numbered_name(tmp_path) -> None:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=(255, 0, 0)).save(buf, format="JPEG")
    jpg_bytes = buf.getvalue()

    writer = _writer(tmp_path)
    ch = _chapter(12, "Some Chapter")
    writer.write(["cbz"], "dumanwu", "系列", ch, [(0, jpg_bytes)])

    # The archive lives inside the chapter's own folder, so a chapter is always
    # exactly one directory.
    cbz = tmp_path / "dumanwu" / "系列" / "0012 - Some Chapter" / "0012 - Some Chapter.cbz"
    assert cbz.is_file()
    with zipfile.ZipFile(cbz) as zf:
        assert zf.namelist() == ["001.webp"]


# ---------------------------------------------------------------------------
# Reading-order normalization per plugin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manhuafree_sorts_by_api_order_field() -> None:
    cfg = AppConfig()
    plugin = ManhuaFreePlugin(cfg, fetcher=None)  # type: ignore[arg-type]
    detail = MangaDetail(
        source_id="manhuafree",
        title="T",
        ref="29882",
        extra={
            "chapters_raw": [
                {"id": "3", "attributes": {"title": "03", "order": 3}},
                {"id": "0", "attributes": {"title": "新作预告", "order": 0}},
                {"id": "1", "attributes": {"title": "01", "order": 1}},
            ]
        },
    )

    chapters = await plugin.list_chapters(detail)
    assert [ch.ref for ch in chapters] == ["0", "1", "3"]
    assert [ch.index for ch in chapters] == [1, 2, 3]
    assert [ch.title for ch in chapters] == ["新作预告", "01", "03"]


@pytest.mark.asyncio
async def test_eighteenmhx_orders_by_href_index_not_data_cs() -> None:
    """Chapter order comes from the href's trailing index, not `data-cs`.

    `data-cs` is an upload counter: on the live site 第1話/第2話/第3話 carry
    35781/35783/35782, so sorting by it swaps chapters 2 and 3. The href tail
    (`/manga/{slug}/{mid}-{cid}-{index}`) is the site's own reading position.
    """
    cfg = AppConfig()
    fetcher = AsyncMock()
    fetcher.get_text.return_value = """
    <div id="allchapterlist">
        <a href="/manga/abc/123-6019-2" data-cs="35782" data-ct="第3话">x</a>
        <a href="/manga/abc/123-5979-0" data-cs="35781" data-ct="第1话">x</a>
        <a href="/manga/abc/123-5999-1" data-cs="35783" data-ct="第2话">x</a>
    </div>
    """
    plugin = EighteenMHXPlugin(cfg, fetcher)
    detail = MangaDetail(source_id="eighteenmhx", title="T", ref="abc", extra={"mid": "123"})

    chapters = await plugin.list_chapters(detail)
    assert [ch.title for ch in chapters] == ["第1话", "第2话", "第3话"]
    assert [ch.extra["c"] for ch in chapters] == ["35781", "35783", "35782"]
    assert [ch.index for ch in chapters] == [1, 2, 3]


def test_wmh1234_sorts_by_chapter_number() -> None:
    cfg = AppConfig()
    plugin = Wmh1234Plugin(cfg, fetcher=None)  # type: ignore[arg-type]

    sample_html = """
    <div class="chapters-list">
        <div class="chapter-item">
            <div class="chapter-info">
                <span class="chapter-number">第2话</span>
                <span class="chapter-title">002 世界是个巨大的传</span>
            </div>
            <div class="chapter-meta">
                <a href="/go/TOKEN2" class="chapter-action">阅读</a>
            </div>
        </div>
        <div class="chapter-item">
            <div class="chapter-info">
                <span class="chapter-number">第1话</span>
                <span class="chapter-title">001 苍天啊！天崩开局！</span>
            </div>
            <div class="chapter-meta">
                <a href="/go/TOKEN1" class="chapter-action">阅读</a>
            </div>
        </div>
    </div>
    """

    chapters = plugin._parse_chapters(sample_html, "50665")
    assert [ch.ref for ch in chapters] == ["TOKEN1", "TOKEN2"]
    assert [ch.index for ch in chapters] == [1, 2]
    assert chapters[0].title == "第1话 001 苍天啊！天崩开局！"


@pytest.mark.asyncio
async def test_comicbox_sorts_by_chapter_id() -> None:
    cfg = AppConfig()
    mock_fetcher = AsyncMock()

    book_html = """
    <html><body><div class="chapter-list">
        <a href="/free-chapter/13412?t=1">▶ 第2話</a>
        <a href="/free-chapter/13411?t=1">▶ 第1話</a>
    </div></body></html>
    """
    mock_fetcher.get_text.return_value = book_html
    plugin = ComicBoxPlugin(cfg, mock_fetcher)
    detail = MangaDetail(source_id="comicbox", title="秘密教學", ref="3751")

    chapters = await plugin.list_chapters(detail)
    assert [ch.ref for ch in chapters] == ["13411", "13412"]
    assert [ch.index for ch in chapters] == [1, 2]
    assert chapters[0].title == "第1話"

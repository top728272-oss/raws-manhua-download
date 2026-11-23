"""Tests for One99MhPlugin (2026-10 epub-store pivot markup)."""

from __future__ import annotations

from app.config import AppConfig
from app.core.fetcher import FetchError, HttpFetcher
from app.core.models import Chapter, SearchResult
from app.plugins.one99mh import One99MhPlugin, _clean_ref, _unpack_epub


def test_clean_ref() -> None:
    assert _clean_ref("wZhzbI8SqXnfeBjN9T9b1w") == "wZhzbI8SqXnfeBjN9T9b1w"
    assert _clean_ref("https://www.199mh.org/comic/wZhzbI8SqXnfeBjN9T9b1w") == "wZhzbI8SqXnfeBjN9T9b1w"
    assert _clean_ref("/comic/wZhzbI8SqXnfeBjN9T9b1w") == "wZhzbI8SqXnfeBjN9T9b1w"
    assert _clean_ref("黄道极日") == "黄道极日"


async def test_one99mh_plugin_registration(tmp_path) -> None:
    cfg = AppConfig(output_dir=str(tmp_path))
    fetcher = HttpFetcher(cfg)
    plugin = One99MhPlugin(cfg, fetcher)

    assert plugin.id == "one99mh"
    assert plugin.name == "199漫画 (one99mh)"
    assert plugin.base_url == "https://www.199mh.org"
    await fetcher.aclose()


def test_parse_search() -> None:
    cfg = AppConfig()
    plugin = One99MhPlugin(cfg, fetcher=None)  # type: ignore[arg-type]

    sample_html = """
    <a href="/comic/1AxnY4_HVSwYLn0gC1WODQ" class="col-6 col-md-2 d-flex flex-column align-items-center">
      <div class="cover"><img src="/upload/cover/a9/98/14620.jpg"></div>
      <div class="title dot">戀愛ジャンキー</div>
      <div class="auth dot">[葉月京]</div>
      <div class="last-vol dot">卷 26</div>
      <div class="last-time">2019-09-03</div></a>
    <a href="/comic/wZhzbI8SqXnfeBjN9T9b1w" class="col-6 col-md-2 d-flex flex-column align-items-center">
      <div class="cover"><img src="/upload/cover/5c/79/17025.png"></div>
      <div class="title dot">恋爱代理</div>
      <div class="auth dot">[赤坂明]</div>
      <div class="last-time">2024-11-18</div></a>
    """

    results = plugin._parse_search(sample_html)
    assert len(results) == 2
    assert results[0].source_id == "one99mh"
    assert results[0].title == "戀愛ジャンキー"
    assert results[0].ref == "1AxnY4_HVSwYLn0gC1WODQ"
    assert results[0].cover_url == "https://www.199mh.org/upload/cover/a9/98/14620.jpg"
    assert results[1].ref == "wZhzbI8SqXnfeBjN9T9b1w"


def test_parse_detail() -> None:
    cfg = AppConfig()
    plugin = One99MhPlugin(cfg, fetcher=None)  # type: ignore[arg-type]

    html = (
        "<h1>恋爱代理</h1>"
        '<img src="/upload/cover/5c/79/17025.png" alt="恋爱代理">'
        "作者 <a>赤坂明(赤坂アカ)</a> 、 <a>西泽5㍉</a> 題材 幽默 , 愛情 , 校園 地區 日本 "
        "進度 連載 收藏 評分"
    )
    res = SearchResult(source_id="one99mh", title="恋爱代理", ref="wZhzbI8SqXnfeBjN9T9b1w")
    detail = plugin._parse_detail(html, res)
    assert detail.title == "恋爱代理"
    assert detail.cover_url == "https://www.199mh.org/upload/cover/5c/79/17025.png"
    assert detail.author == "赤坂明(赤坂アカ)、西泽5㍉"
    assert detail.tags == ["幽默", "愛情", "校園"]
    assert detail.status == "連載"


def test_parse_chapters() -> None:
    cfg = AppConfig()
    plugin = One99MhPlugin(cfg, fetcher=None)  # type: ignore[arg-type]

    sample_html = """
    <input class="form-check-input" type="checkbox" value="465042" id="vol465042">
    <label class="form-check-label" for="vol465042"><span class="vol-item-no">第01话韩翻</span>
    <span class="vol-item-info ext ext-epub">20M</span> <span class="vol-item-info">(44頁)</span></label>
    <input class="form-check-input" type="checkbox" value="465004" id="vol465004">
    <label class="form-check-label" for="vol465004"><span class="vol-item-no">第02话</span>
    <span class="vol-item-info">(25頁)</span></label>
    """

    chapters = plugin._parse_chapters(sample_html, "wZhzbI8SqXnfeBjN9T9b1w")
    assert len(chapters) == 2
    assert chapters[0].index == 1 and chapters[0].ref == "465042"
    assert chapters[0].title == "第01话韩翻"
    assert chapters[0].extra["vol_id"] == "465042"
    assert chapters[0].extra["page_count"] == 44
    assert chapters[1].index == 2 and chapters[1].ref == "465004"


async def test_get_pages_serves_epub_images_through_wrapper() -> None:
    cfg = AppConfig()
    plugin = One99MhPlugin(cfg, fetcher=HttpFetcher(cfg))
    plugin.fetcher._loader = _fake_loader  # type: ignore[attr-defined]

    chapter = Chapter(
        source_id="one99mh",
        manga_ref="wZhzbI8SqXnfeBjN9T9b1w",
        title="第01话",
        ref="465004",
        index=1,
        extra={"vol_id": "465004"},
    )
    pages = await plugin.get_pages(chapter)

    assert [p.image_url for p in pages] == [
        "one99mh-epub://465004/0",
        "one99mh-epub://465004/1",
    ]
    assert await plugin.fetcher.get_bytes(pages[0].image_url) == b"\xff\xd8\xffpage-one"
    assert await plugin.fetcher.get_bytes(pages[1].image_url) == b"\xff\xd8\xffpage-two"
    try:
        await plugin.fetcher.get_bytes("one99mh-epub://465004/9")
    except FetchError:
        pass
    else:
        raise AssertionError("out-of-range page must raise FetchError")


async def _fake_loader(vol_id: str) -> list[bytes]:
    assert vol_id == "465004"
    return [b"\xff\xd8\xffpage-one", b"\xff\xd8\xffpage-two"]


def test_unpack_epub_spine_order() -> None:
    import io
    import zipfile

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        manifest, spine = "", ""
        for n in (1, 2, 3):
            zf.writestr(f"OEBPS/images/p{n:03d}.jpg", png + f"page{n}".encode())
            zf.writestr(
                f"OEBPS/text/ch{n}.xhtml",
                f'<html><body><img src="../images/p{n:03d}.jpg"/></body></html>',
            )
            manifest += f'<item id="p{n}" href="images/p{n:03d}.jpg" media-type="image/jpeg"/>'
            manifest += f'<item id="c{n}" href="text/ch{n}.xhtml" media-type="application/xhtml+xml"/>'
            spine += f'<itemref idref="c{n}"/>'
        zf.writestr(
            "OEBPS/content.opf",
            f'<package xmlns="http://www.idpf.org/2007/opf"><manifest>{manifest}</manifest>'
            f"<spine>{spine}</spine></package>",
        )

    images = _unpack_epub(buf.getvalue())
    assert [img[-5:] for img in images] == [b"page1", b"page2", b"page3"]

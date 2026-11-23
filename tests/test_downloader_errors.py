"""Download-engine error handling: HTTP 5xx fallback and the 0-image rule.

Three guarantees are locked in here:

1. A chapter that ends with **0 images is never written to disk** — no empty
   folder, no empty .cbz.
2. When its own source answers 5xx, the engine retries the chapter on another
   source; if that also fails, the run still warns explicitly at the end.
3. A chapter that is short of pages is **never reported as a success** — the
   end-of-run pass retries the missing pages and falls back for them, and
   anything still absent is listed in the run warning.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.config import AppConfig, DownloadConfig
from app.core.downloader import DownloadEngine
from app.core.fallback import FallbackChapter
from app.core.fetcher import BaseFetcher
from app.core.models import Chapter, DownloadTask, OutputFormat, Page
from app.core.output import OutputWriter


def _status_error(status: int, url: str = "https://img.example/1.webp") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class FakeFetcher(BaseFetcher):
    """`behaviour(url)` returns bytes or raises."""

    def __init__(self, behaviour) -> None:
        self._behaviour = behaviour
        self.calls: list[str] = []

    async def get_bytes(self, url: str, **kwargs) -> bytes:
        self.calls.append(url)
        return self._behaviour(url)


class StubFallback:
    """Minimal FallbackResolver: returns whatever it was handed."""

    def __init__(self, result: FallbackChapter | None) -> None:
        self._result = result
        self.calls: list[tuple[str, str, str]] = []

    async def find_chapter(
        self, manga_title: str, chapter: Chapter, failed_source_id: str
    ) -> FallbackChapter | None:
        self.calls.append((manga_title, chapter.title, failed_source_id))
        return self._result


def _cfg(tmp_path, **download) -> AppConfig:
    options = {"retries": 0, "backoff_seconds": 0, "max_concurrent": 4}
    options.update(download)
    return AppConfig(output_dir=str(tmp_path), download=DownloadConfig(**options))


def _pages(count: int = 3, prefix: str = "https://img.example") -> list[Page]:
    return [
        Page(index=i, image_url=f"{prefix}/{i + 1}.webp", referer="https://site.example/")
        for i in range(count)
    ]


def _task(title: str = "斗破苍穹", chapter_title: str = "第1话") -> DownloadTask:
    chapter = Chapter(
        source_id="broken", manga_ref="ref", title=chapter_title, ref="c1", index=1
    )
    return DownloadTask(
        source_id="broken",
        manga_title=title,
        chapter=chapter,
        formats=[OutputFormat.FOLDER],
    )


async def _run(tmp_path, fetcher, resolver, fallback=None, task=None, formats=None):
    task = task or _task()
    if formats is not None:
        task.formats = formats
    engine = DownloadEngine(
        _cfg(tmp_path), fetcher=fetcher, writer=OutputWriter(_cfg(tmp_path)), fallback=fallback
    )
    summary = await engine.run([task], resolve_pages=resolver)
    return task, summary


# --------------------------------------------------------------------------- #
# 0 images -> never saved
# --------------------------------------------------------------------------- #


async def test_all_pages_5xx_writes_nothing(tmp_path) -> None:
    fetcher = FakeFetcher(lambda url: (_ for _ in ()).throw(_status_error(503, url)))

    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages()

    task, summary = await _run(tmp_path, fetcher, resolver)

    assert task.pages_saved == 0
    assert task.status == "error"
    assert task.server_error is True
    assert "not saved" in (task.error or "")
    assert summary.saved == 0 and len(summary.failed) == 1
    # Nothing at all was created — not even the source folder.
    assert not (tmp_path / "broken").exists()
    assert list(tmp_path.rglob("*")) == []


async def test_zero_images_writes_nothing(tmp_path) -> None:
    fetcher = FakeFetcher(lambda url: (_ for _ in ()).throw(_status_error(502, url)))

    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages()

    await _run(
        tmp_path,
        fetcher,
        resolver,
        formats=[OutputFormat.FOLDER, OutputFormat.CBZ],
    )
    assert list(tmp_path.rglob("*.cbz")) == []
    assert list(tmp_path.rglob("*")) == []


async def test_resolver_returning_no_pages_is_not_saved(tmp_path) -> None:
    fetcher = FakeFetcher(lambda url: b"DATA")

    async def resolver(_chapter: Chapter) -> list[Page]:
        return []

    task, summary = await _run(tmp_path, fetcher, resolver)

    assert task.pages_saved == 0
    assert task.status == "error"
    assert len(summary.failed) == 1
    assert list(tmp_path.rglob("*")) == []


async def test_output_writer_refuses_empty_chapter(tmp_path) -> None:
    writer = OutputWriter(_cfg(tmp_path))
    chapter = Chapter(source_id="s", manga_ref="r", title="第1话", ref="c", index=1)
    formats = [OutputFormat.FOLDER, OutputFormat.CBZ]
    written = writer.write(formats, "s", "系列", chapter, [])
    assert written == 0
    assert list(tmp_path.rglob("*")) == []


# --------------------------------------------------------------------------- #
# 5xx -> fall back to another source
# --------------------------------------------------------------------------- #


async def test_5xx_images_fall_back_to_another_source(tmp_path) -> None:
    def behaviour(url: str) -> bytes:
        if url.startswith("https://img.example"):
            raise _status_error(503, url)
        return b"FALLBACK-DATA"

    fallback_chapter = FallbackChapter(
        source_id="other",
        source_name="other site",
        manga_title="斗破苍穹",
        chapter=Chapter(source_id="other", manga_ref="d", title="第1话", ref="oc1", index=1),
        pages=_pages(3, prefix="https://other.example"),
        similarity=1.0,
        matched_by="chapter number 1",
    )
    stub = StubFallback(fallback_chapter)

    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages()

    task, summary = await _run(
        tmp_path, FakeFetcher(behaviour), resolver, fallback=stub
    )

    assert stub.calls == [("斗破苍穹", "第1话", "broken")]
    assert task.status == "done"
    assert task.pages_saved == 3
    assert task.fallback_source_id == "other"
    assert task.note == "via other site"
    assert summary.saved == 1 and summary.fallback_saved == 1 and summary.ok
    # Files land under the ORIGINAL series folder so the library stays coherent.
    saved = sorted((tmp_path / "broken" / "斗破苍穹" / "第1话").glob("*"))
    assert [p.name for p in saved] == ["001.webp", "002.webp", "003.webp"]
    assert saved[0].read_bytes() == b"FALLBACK-DATA"


async def test_resolve_pages_5xx_falls_back_to_another_source(tmp_path) -> None:
    fallback_chapter = FallbackChapter(
        source_id="other",
        source_name="other site",
        manga_title="斗破苍穹",
        chapter=Chapter(source_id="other", manga_ref="d", title="第1话", ref="oc1", index=1),
        pages=_pages(2, prefix="https://other.example"),
        similarity=1.0,
        matched_by="chapter number 1",
    )
    stub = StubFallback(fallback_chapter)

    async def resolver(_chapter: Chapter) -> list[Page]:
        raise _status_error(500, "https://api.example/chapter/1")

    task, summary = await _run(
        tmp_path, FakeFetcher(lambda url: b"DATA"), resolver, fallback=stub
    )

    assert task.status == "done"
    assert task.pages_saved == 2
    assert task.fallback_source_id == "other"
    assert summary.ok
    assert (tmp_path / "broken" / "斗破苍穹" / "第1话" / "002.webp").exists()


async def test_fallback_is_not_used_when_images_arrive(tmp_path) -> None:
    stub = StubFallback(None)

    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages(2)

    task, summary = await _run(
        tmp_path, FakeFetcher(lambda url: b"DATA"), resolver, fallback=stub
    )

    assert stub.calls == []
    assert task.status == "done" and task.pages_saved == 2
    assert task.fallback_source_id is None
    assert summary.ok


async def test_partial_chapter_is_saved_but_never_called_a_success(tmp_path, caplog) -> None:
    """A page that never arrives must not be silently treated as success."""

    def behaviour(url: str) -> bytes:
        if url.endswith("2.webp"):
            raise _status_error(503, url)
        return b"DATA"

    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages(3)

    with caplog.at_level(logging.WARNING, logger="app.core.downloader"):
        task, summary = await _run(tmp_path, FakeFetcher(behaviour), resolver)

    assert task.pages_saved == 2
    assert task.status == "partial"
    assert task.complete is False
    assert task.pages_label == "2/3 pages"
    assert not summary.ok  # a chapter missing pages is NOT a success
    assert len(summary.failed) == 1
    assert "still missing" in summary.failed[0]
    assert "DOWNLOAD RUN COMPLETE WITH FAILURES" in caplog.text
    # The pages that did arrive are kept, with the hole preserved.
    assert sorted(p.name for p in (tmp_path / "broken" / "斗破苍穹" / "第1话").glob("*")) == [
        "001.webp",
        "003.webp",
    ]


async def test_missing_page_is_recovered_after_the_run(tmp_path) -> None:
    """The end-of-run pass retries only the pages that failed."""
    seen: list[str] = []

    def behaviour(url: str) -> bytes:
        seen.append(url)
        # Fail on the first attempt only; the recovery pass must succeed.
        if url.endswith("2.webp") and seen.count(url) == 1:
            raise _status_error(503, url)
        return b"DATA"

    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages(3)

    task, summary = await _run(tmp_path, FakeFetcher(behaviour), resolver)

    assert task.complete is True
    assert task.status == "done"
    assert task.recovered_pages == 1
    assert task.pages_saved == 3
    assert summary.ok and summary.recovered == 1
    assert sorted(p.name for p in (tmp_path / "broken" / "斗破苍穹" / "第1话").glob("*")) == [
        "001.webp",
        "002.webp",
        "003.webp",
    ]


async def test_missing_page_is_taken_from_another_source(tmp_path) -> None:
    """A page that never arrives locally is fetched from the fallback source."""

    def behaviour(url: str) -> bytes:
        if url.startswith("https://img.example") and url.endswith("2.webp"):
            raise _status_error(503, url)
        return b"OTHER" if url.startswith("https://other.example") else b"ORIGINAL"

    fallback_chapter = FallbackChapter(
        source_id="other",
        source_name="other site",
        manga_title="斗破苍穹",
        chapter=Chapter(source_id="other", manga_ref="d", title="第1话", ref="oc1", index=1),
        pages=_pages(3, prefix="https://other.example"),
        similarity=1.0,
        matched_by="chapter number 1",
    )
    stub = StubFallback(fallback_chapter)

    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages(3)

    task, summary = await _run(
        tmp_path, FakeFetcher(behaviour), resolver, fallback=stub
    )

    assert task.complete is True
    assert task.fallback_source_id == "other"
    assert task.recovered_pages == 1
    assert summary.ok
    chapter_dir = tmp_path / "broken" / "斗破苍穹" / "第1话"
    assert (chapter_dir / "001.webp").read_bytes() == b"ORIGINAL"
    assert (chapter_dir / "002.webp").read_bytes() == b"OTHER"  # the recovered page
    assert (chapter_dir / "003.webp").read_bytes() == b"ORIGINAL"


async def test_html_error_page_is_never_saved_as_a_page(tmp_path) -> None:
    """A 200 that returns an error page is a failure, not a page."""

    def behaviour(url: str) -> bytes:
        if url.endswith("2.webp"):
            return b"<!DOCTYPE html><html><body>404 Not Found</body></html>"
        return b"DATA"

    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages(3)

    task, summary = await _run(tmp_path, FakeFetcher(behaviour), resolver)

    assert task.pages_saved == 2
    assert not task.complete
    assert not summary.ok
    chapter_dir = tmp_path / "broken" / "斗破苍穹" / "第1话"
    assert "002.webp" not in {p.name for p in chapter_dir.glob("*")}


# --------------------------------------------------------------------------- #
# skip-if-complete
# --------------------------------------------------------------------------- #


async def test_complete_chapter_is_skipped_on_a_rerun(tmp_path) -> None:
    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages(2)

    first_fetcher = FakeFetcher(lambda url: b"DATA")
    task1, summary1 = await _run(tmp_path, first_fetcher, resolver)
    assert task1.complete and summary1.saved == 1 and summary1.skipped == 0
    assert len(first_fetcher.calls) == 2

    # Re-running the same chapter must not touch the network at all.
    async def exploding_resolver(_chapter: Chapter) -> list[Page]:
        raise AssertionError("a complete chapter must not even be resolved")

    idle_fetcher = FakeFetcher(lambda url: b"SHOULD-NOT-BE-FETCHED")
    task2, summary2 = await _run(tmp_path, idle_fetcher, exploding_resolver)

    assert task2.skipped is True
    assert task2.status == "skipped"
    assert task2.note == "already downloaded"
    assert task2.pages_saved == 2
    assert idle_fetcher.calls == []
    assert summary2.ok and summary2.skipped == 1 and summary2.saved == 1


async def test_incomplete_chapter_is_not_skipped(tmp_path) -> None:
    def behaviour(url: str) -> bytes:
        if url.endswith("2.webp"):
            raise _status_error(503, url)
        return b"DATA"

    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages(3)

    await _run(tmp_path, FakeFetcher(behaviour), resolver)

    # The chapter is on disk but short of a page, so a rerun must try again.
    calls: list[str] = []

    def second_behaviour(url: str) -> bytes:
        calls.append(url)
        return b"DATA"

    task, summary = await _run(tmp_path, FakeFetcher(second_behaviour), resolver)

    assert task.skipped is False
    assert calls, "an incomplete chapter must be retried"
    assert task.complete is True and summary.ok


async def test_deleting_a_page_makes_the_chapter_download_again(tmp_path) -> None:
    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages(2)

    await _run(tmp_path, FakeFetcher(lambda url: b"DATA"), resolver)
    chapter_dir = tmp_path / "broken" / "斗破苍穹" / "第1话"
    (chapter_dir / "002.webp").unlink()

    fetcher = FakeFetcher(lambda url: b"DATA")
    task, _summary = await _run(tmp_path, fetcher, resolver)

    assert task.skipped is False
    assert len(fetcher.calls) == 2  # re-downloaded
    assert (chapter_dir / "002.webp").exists()


async def test_skip_existing_can_be_disabled(tmp_path) -> None:
    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages(2)

    await _run(tmp_path, FakeFetcher(lambda url: b"DATA"), resolver)

    fetcher = FakeFetcher(lambda url: b"DATA")
    engine = DownloadEngine(
        _cfg(tmp_path, skip_existing=False),
        fetcher=fetcher,
        writer=OutputWriter(_cfg(tmp_path)),
    )
    task = _task()
    await engine.run([task], resolve_pages=resolver)

    assert task.skipped is False
    assert len(fetcher.calls) == 2


async def test_written_chapter_leaves_no_stale_pages_behind(tmp_path) -> None:
    """A re-download with fewer pages must not leave the old higher numbers."""
    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages(4)

    await _run(tmp_path, FakeFetcher(lambda url: b"DATA"), resolver)
    chapter_dir = tmp_path / "broken" / "斗破苍穹" / "第1话"
    assert sorted(p.name for p in chapter_dir.glob("*")) == [
        "001.webp",
        "002.webp",
        "003.webp",
        "004.webp",
    ]

    async def shorter(_chapter: Chapter) -> list[Page]:
        return _pages(2)

    engine = DownloadEngine(
        _cfg(tmp_path, skip_existing=False),
        fetcher=FakeFetcher(lambda url: b"DATA"),
        writer=OutputWriter(_cfg(tmp_path)),
    )
    await engine.run([_task()], resolve_pages=shorter)

    assert sorted(p.name for p in chapter_dir.glob("*")) == ["001.webp", "002.webp"]


# --------------------------------------------------------------------------- #
# explicit end-of-run warning
# --------------------------------------------------------------------------- #


async def test_run_warns_explicitly_when_fallback_also_fails(tmp_path, caplog) -> None:
    fetcher = FakeFetcher(lambda url: (_ for _ in ()).throw(_status_error(503, url)))
    stub = StubFallback(None)  # no other source has this series

    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages()

    with caplog.at_level(logging.WARNING, logger="app.core.downloader"):
        task, summary = await _run(tmp_path, fetcher, resolver, fallback=stub)

    assert stub.calls, "the fallback must have been attempted"
    text = caplog.text
    assert "DOWNLOAD RUN COMPLETE WITH FAILURES" in text
    assert "INCOMPLETE ->" in text
    assert "斗破苍穹 - 第1话" in text
    assert not summary.ok
    assert task.pages_saved == 0
    assert list(tmp_path.rglob("*")) == []


async def test_run_warning_survives_when_no_fallback_is_configured(tmp_path, caplog) -> None:
    fetcher = FakeFetcher(lambda url: (_ for _ in ()).throw(_status_error(504, url)))

    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages(1)

    with caplog.at_level(logging.WARNING, logger="app.core.downloader"):
        _task_obj, summary = await _run(tmp_path, fetcher, resolver)

    assert "DOWNLOAD RUN COMPLETE WITH FAILURES" in caplog.text
    assert len(summary.failed) == 1


async def test_summary_message_mentions_what_was_not_saved(tmp_path) -> None:
    fetcher = FakeFetcher(lambda url: (_ for _ in ()).throw(_status_error(503, url)))

    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages(1)

    _task_obj, summary = await _run(tmp_path, fetcher, resolver)

    message = summary.warning_message()
    assert "still short of pages" in message
    assert summary.detail_lines() == summary.failed


async def test_successful_run_reports_ok(tmp_path) -> None:
    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages(2)

    task, summary = await _run(tmp_path, FakeFetcher(lambda url: b"DATA"), resolver)

    assert summary.ok and summary.saved == 1 and summary.failed == []
    assert "Download complete" in summary.warning_message()
    assert task.pages_saved == 2


async def test_non_5xx_resolution_failure_still_falls_back(tmp_path) -> None:
    """A paywalled/login-gated chapter is just as unusable as a 5xx one."""
    fallback_chapter = FallbackChapter(
        source_id="other",
        source_name="other site",
        manga_title="斗破苍穹",
        chapter=Chapter(source_id="other", manga_ref="d", title="第1话", ref="oc1", index=1),
        pages=_pages(1, prefix="https://other.example"),
        similarity=1.0,
        matched_by="chapter number 1",
    )
    stub = StubFallback(fallback_chapter)

    async def resolver(_chapter: Chapter) -> list[Page]:
        raise ValueError("mkzhan: chapter is paywalled/VIP (code 102/103)")

    task, summary = await _run(
        tmp_path, FakeFetcher(lambda url: b"DATA"), resolver, fallback=stub
    )

    assert task.server_error is False
    assert task.status == "done" and task.pages_saved == 1
    assert summary.ok


async def test_write_failure_is_reported_as_not_saved(tmp_path, monkeypatch) -> None:
    class ExplodingWriter(OutputWriter):
        def write(self, *args, **kwargs) -> int:
            raise OSError("disk full")

    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages(1)

    engine = DownloadEngine(
        _cfg(tmp_path),
        fetcher=FakeFetcher(lambda url: b"DATA"),
        writer=ExplodingWriter(_cfg(tmp_path)),
    )
    task = _task()
    summary = await engine.run([task], resolve_pages=resolver)

    assert task.pages_saved == 0
    assert task.status == "error"
    assert "write failed" in (task.error or "")
    assert len(summary.failed) == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_every_5xx_status_triggers_the_fallback(tmp_path, status: int) -> None:
    fallback_chapter = FallbackChapter(
        source_id="other",
        source_name="other site",
        manga_title="斗破苍穹",
        chapter=Chapter(source_id="other", manga_ref="d", title="第1话", ref="oc1", index=1),
        pages=_pages(1, prefix="https://other.example"),
        similarity=1.0,
        matched_by="chapter number 1",
    )
    stub = StubFallback(fallback_chapter)

    def behaviour(url: str) -> bytes:
        if url.startswith("https://img.example"):
            raise _status_error(status, url)
        return b"DATA"

    async def resolver(_chapter: Chapter) -> list[Page]:
        return _pages(1)

    task, _summary = await _run(tmp_path, FakeFetcher(behaviour), resolver, fallback=stub)

    assert task.pages_saved == 1
    assert task.fallback_source_id == "other"


# --------------------------------------------------------------------------- #
# page numbering is by POSITION, so a plugin cannot shift the output
# --------------------------------------------------------------------------- #


async def test_one_based_plugin_indices_still_produce_001_webp(tmp_path) -> None:
    """Defence in depth: filenames come from page POSITION, not the plugin's index.

    Four plugins used to hand out 1-based indices, which made the writer save the
    first page as 002.webp. The plugins are fixed, and the engine normalises by
    position as well, so a future plugin cannot reintroduce the shift.
    """

    async def resolver(_chapter: Chapter) -> list[Page]:
        return [
            Page(index=1, image_url="https://img.example/1.webp"),
            Page(index=2, image_url="https://img.example/2.webp"),
            Page(index=3, image_url="https://img.example/3.webp"),
        ]

    task, summary = await _run(tmp_path, FakeFetcher(lambda url: b"DATA"), resolver)

    assert task.complete and summary.ok
    assert sorted(p.name for p in (tmp_path / "broken" / "斗破苍穹" / "第1话").glob("*")) == [
        "001.webp",
        "002.webp",
        "003.webp",
    ]

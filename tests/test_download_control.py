"""Pause / stop for a running download.

End to end through the real engine and the real OutputWriter on a temp dir,
because the guarantee that matters is about what lands on disk:

* pausing holds every page fetch that has not started yet (in-flight pages
  still finish), and resuming completes the chapter normally;
* stopping aborts the run, writes nothing for the chapter that was mid-flight,
  and never starts the recovery pass;
* chapters that were already complete before the stop stay on disk and are
  reported as saved, not as failures.
"""

from __future__ import annotations

import asyncio

from app.config import AppConfig, DownloadConfig
from app.core.downloader import DownloadControl, DownloadEngine
from app.core.fetcher import BaseFetcher
from app.core.models import Chapter, DownloadTask, OutputFormat, Page
from app.core.output import OutputWriter

PAGE_DELAY = 0.02


class SlowFetcher(BaseFetcher):
    """Records every request and takes `delay` seconds to answer."""

    def __init__(self, delay: float = PAGE_DELAY) -> None:
        self.delay = delay
        self.calls: list[str] = []

    async def get_bytes(self, url: str, **kwargs) -> bytes:
        self.calls.append(url)
        await asyncio.sleep(self.delay)
        return b"page-bytes"


def _cfg(tmp_path, **download) -> AppConfig:
    options = {
        "retries": 0,
        "backoff_seconds": 0,
        "max_concurrent": 2,
        "batch_size": 5,
        "skip_existing": True,
        "recovery_attempts": 1,
    }
    options.update(download)
    return AppConfig(output_dir=str(tmp_path), download=DownloadConfig(**options))


def _pages(count: int, tag: str = "c") -> list[Page]:
    return [
        Page(index=i, image_url=f"https://img.example/{tag}/{i + 1}.webp", referer="https://s/")
        for i in range(count)
    ]


def _task(title: str = "Series", chapter_title: str = "Ch 1") -> DownloadTask:
    chapter = Chapter(source_id="src", manga_ref="ref", title=chapter_title, ref="c1", index=0)
    return DownloadTask(
        source_id="src", manga_title=title, chapter=chapter, formats=[OutputFormat.FOLDER]
    )


def _resolver(pages: list[Page], calls: list[str] | None = None):
    async def resolve(chapter: Chapter) -> list[Page]:
        if calls is not None:
            calls.append(chapter.title)
        return list(pages)

    return resolve


def test_pause_holds_new_fetches_and_resume_finishes_the_chapter(tmp_path) -> None:
    async def scenario() -> None:
        fetcher = SlowFetcher()
        control = DownloadControl()
        task = _task()
        engine = DownloadEngine(
            _cfg(tmp_path), fetcher, OutputWriter(_cfg(tmp_path)), control=control
        )

        run = asyncio.create_task(engine.run([task], _resolver(_pages(30))))
        await asyncio.sleep(0.08)

        control.pause()
        await asyncio.sleep(0.15)  # let the pages already in flight land
        assert control.paused
        at_pause = len(fetcher.calls)
        assert at_pause < 30, "the chapter must not have finished before the pause"

        await asyncio.sleep(0.3)
        assert len(fetcher.calls) == at_pause, "no new page may start while paused"
        assert not run.done()

        control.resume()
        summary = await asyncio.wait_for(run, timeout=10)

        assert summary.ok and not summary.stopped
        assert summary.saved == 1
        assert task.complete
        assert len(fetcher.calls) == 30

    asyncio.run(scenario())


def test_stop_writes_nothing_for_the_chapter_that_was_in_flight(tmp_path) -> None:
    async def scenario() -> None:
        cfg = _cfg(tmp_path)
        fetcher = SlowFetcher()
        control = DownloadControl()
        task = _task()
        writer = OutputWriter(cfg)
        engine = DownloadEngine(cfg, fetcher, writer, control=control)

        run = asyncio.create_task(engine.run([task], _resolver(_pages(40))))
        await asyncio.sleep(0.08)
        control.stop()
        summary = await asyncio.wait_for(run, timeout=10)

        assert summary.stopped
        assert summary.stopped_chapters == 1
        assert summary.saved == 0
        assert summary.failed == [], "a stop is not a failure"
        assert task.status == "stopped"
        assert not writer.chapter_dir("src", "Series", task.chapter).exists()
        assert len(fetcher.calls) < 40

    asyncio.run(scenario())


def test_stop_skips_the_recovery_pass(tmp_path) -> None:
    """Recovery re-resolves every chapter that saved nothing; a stop must not."""

    async def scenario() -> None:
        calls: list[str] = []
        control = DownloadControl()
        fetcher = SlowFetcher()
        engine = DownloadEngine(
            _cfg(tmp_path), fetcher, OutputWriter(_cfg(tmp_path)), control=control
        )

        run = asyncio.create_task(engine.run([_task()], _resolver(_pages(40), calls)))
        await asyncio.sleep(0.08)
        control.stop()
        await asyncio.wait_for(run, timeout=10)

        assert calls == ["Ch 1"], "the stop must not trigger a second resolution"

    asyncio.run(scenario())


def test_chapters_finished_before_the_stop_stay_saved(tmp_path) -> None:
    async def scenario() -> None:
        cfg = _cfg(tmp_path)
        control = DownloadControl()
        fetcher = SlowFetcher()
        writer = OutputWriter(cfg)
        done_task = _task(chapter_title="Ch 1")
        slow_task = _task(chapter_title="Ch 2")

        pages_by_title = {"Ch 1": _pages(2, tag="a"), "Ch 2": _pages(40, tag="b")}

        async def resolve(chapter: Chapter) -> list[Page]:
            return list(pages_by_title[chapter.title])

        engine = DownloadEngine(cfg, fetcher, writer, control=control)
        run = asyncio.create_task(engine.run([done_task, slow_task], resolve))

        await asyncio.sleep(0.2)  # Ch 1 is two pages: long done by now
        assert done_task.complete
        control.stop()
        summary = await asyncio.wait_for(run, timeout=10)

        assert summary.stopped
        assert summary.saved == 1
        assert summary.stopped_chapters == 1
        assert writer.chapter_dir("src", "Series", done_task.chapter).exists()
        assert not writer.chapter_dir("src", "Series", slow_task.chapter).exists()
        assert "Download stopped: 1/2" in summary.warning_message()

    asyncio.run(scenario())


def test_stop_wins_over_a_pause() -> None:
    async def scenario() -> None:
        control = DownloadControl()
        control.pause()
        waiter = asyncio.create_task(control.checkpoint())
        await asyncio.sleep(0.05)
        assert not waiter.done()

        control.stop()
        assert await asyncio.wait_for(waiter, timeout=2) is False
        assert not control.paused

    asyncio.run(scenario())


def test_queued_chapters_are_marked_stopped_when_the_run_aborts(tmp_path) -> None:
    """Batch 2 must never be attempted, and its tasks must not stay 'queued'."""

    async def scenario() -> None:
        control = DownloadControl()
        tasks = [_task(chapter_title=f"Ch {i + 1}") for i in range(4)]
        engine = DownloadEngine(
            _cfg(tmp_path, batch_size=2),
            SlowFetcher(),
            OutputWriter(_cfg(tmp_path)),
            control=control,
        )

        run = asyncio.create_task(engine.run(tasks, _resolver(_pages(20))))
        await asyncio.sleep(0.05)
        control.stop()
        await asyncio.wait_for(run, timeout=10)

        assert [t.status for t in tasks] == ["stopped"] * 4
        assert not any(t.saved for t in tasks)

    asyncio.run(scenario())


def test_checkpoint_lets_a_live_run_through() -> None:
    async def scenario() -> None:
        assert await DownloadControl().checkpoint() is True

    asyncio.run(scenario())


def test_checkpoint_blocks_while_paused_and_continues_on_resume() -> None:
    async def scenario() -> None:
        control = DownloadControl()
        control.pause()
        waiter = asyncio.create_task(control.checkpoint())
        await asyncio.sleep(0.2)
        assert not waiter.done(), "a paused run must not let a page through"

        control.resume()
        assert await asyncio.wait_for(waiter, timeout=2) is True

    asyncio.run(scenario())

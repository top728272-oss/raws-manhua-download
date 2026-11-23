"""Download engine.

Enforces the two rules we locked in DESIGN.md §4.2:
  1. HARD concurrency cap of `max_concurrent` (default 5) via an asyncio.Semaphore.
  2. Jobs with >5 chapters run in BATCHES of `batch_size` (default 5),
     one batch fully finishing before the next starts.

Page failures are treated as unacceptable, in three escalating stages:

1. **During the run** - every page gets `retries + 1` attempts with backoff, and
   a body that is not really an image (an HTML/JSON error page) counts as a
   failure rather than being saved as a page. A signed URL that has already
   expired (or any other deterministic 4xx) is not retried at all: the CDN
   rejects it every time, so the attempt budget is reserved for transient
   failures.
2. **Recovery pass, after all batches finish** - each chapter that is still
   short of pages *or saved nothing at all* is re-resolved and retried with a
   larger attempt budget (`download.recovery_attempts`). Re-resolving matters
   for sources that hand out signed, expiring image URLs: a stale signature
   batch fails 100% of fetches, but a fresh resolution can mint new URLs.
3. **Cross-source fallback** - any page still missing is requested from another
   source that has the same series (`app/core/fallback.py`), matched by page
   position, and merged into the chapter already on disk.

Page positions, not plugin-supplied indices, decide filenames: page `i` of the
source's list is written as `{i + 1:03d}.{ext}`, so a missing page leaves a
visible hole and a plugin that numbers pages from 1 can never shift the output.

A chapter that already exists complete on disk is skipped outright
(`download.skip_existing`), so re-running a series does not re-download it. A
chapter that downloads 0 images is never written at all, and every chapter still
incomplete when the run ends is reported in the end-of-run warning.

A run can be paused or stopped from the UI through `DownloadControl`. Pausing
holds every not-yet-started page fetch; pages already in flight finish normally.
Stopping aborts the run: unfetched pages are skipped, a chapter that was
mid-flight writes nothing, and the recovery pass never starts.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.config import AppConfig
from app.core.fallback import FallbackChapter, FallbackResolver
from app.core.fetcher import (
    BaseFetcher,
    is_permanent_client_error,
    is_server_error,
    looks_like_text,
)
from app.core.models import Chapter, DownloadTask, Page
from app.core.output import OutputWriter
from app.core.signed import url_expiry

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[DownloadTask], None]
PageResolver = Callable[[Chapter], Awaitable[list[Page]]]


class DownloadControl:
    """Pause / stop gate shared between the UI thread and a running engine.

    `pause()` is cooperative: every page that has not started fetching yet waits
    until `resume()`. `stop()` is final - waiters wake up, report the abort, and
    the engine unwinds without writing the chapters it was working on.

    The gate is a plain `threading.Event` polled from the event loop, so the UI
    thread never has to touch loop-owned objects (`asyncio.Event.set` is not
    thread-safe).
    """

    POLL_SECONDS = 0.1

    def __init__(self) -> None:
        self._pause_gate = threading.Event()
        self._stopped = False

    @property
    def paused(self) -> bool:
        return self._pause_gate.is_set()

    @property
    def stopped(self) -> bool:
        return self._stopped

    def pause(self) -> None:
        if not self._stopped:
            self._pause_gate.set()

    def resume(self) -> None:
        self._pause_gate.clear()

    def stop(self) -> None:
        self._stopped = True
        self._pause_gate.clear()

    async def checkpoint(self) -> bool:
        """Wait out a pause; return False when the run must abort."""
        while self._pause_gate.is_set() and not self._stopped:
            await asyncio.sleep(self.POLL_SECONDS)
        return not self._stopped


@dataclass
class _ImageOutcome:
    """Result of fetching one page: the bytes, and whether a 5xx was seen."""

    data: bytes | None = None
    server_error: bool = False


@dataclass
class RunSummary:
    """Outcome of a whole `DownloadEngine.run()` call."""

    total: int = 0

    saved: int = 0

    skipped: int = 0

    recovered: int = 0

    fallback_saved: int = 0

    stopped: bool = False

    stopped_chapters: int = 0

    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def warning_message(self) -> str:
        bits: list[str] = []
        if self.skipped:
            bits.append(f"{self.skipped} already on disk")
        if self.recovered:
            bits.append(f"{self.recovered} completed by recovery")
        if self.fallback_saved:
            bits.append(f"{self.fallback_saved} using another source")
        extra = f" ({', '.join(bits)})" if bits else ""

        if self.stopped:
            return (
                f"Download stopped: {self.saved}/{self.total} chapter(s) saved, "
                f"{self.stopped_chapters} stopped before finishing{extra}."
            )
        if self.ok:
            return f"Download complete: {self.saved}/{self.total} chapter(s) saved{extra}."
        return (
            f"{len(self.failed)} of {self.total} chapter(s) are still short of pages "
            f"even after retries and the cross-source fallback ({self.saved} complete"
            f"{extra}). See manhua-dl.log for the missing page numbers."
        )

    def detail_lines(self) -> list[str]:
        return list(self.failed)


class DownloadEngine:
    def __init__(
        self,
        cfg: AppConfig,
        fetcher: BaseFetcher,
        writer: OutputWriter,
        on_progress: ProgressCallback | None = None,
        fallback: FallbackResolver | None = None,
        control: DownloadControl | None = None,
    ) -> None:
        self._cfg = cfg
        self._fetcher = fetcher
        self._writer = writer
        self._on_progress = on_progress
        self._fallback = fallback
        self._control = control or DownloadControl()
        self._sem = asyncio.Semaphore(cfg.download.max_concurrent)
        self._skip_existing = bool(getattr(cfg.download, "skip_existing", True))
        self._recovery_attempts = max(int(getattr(cfg.download, "recovery_attempts", 3)), 1)

    @property
    def control(self) -> DownloadControl:
        return self._control

    def _notify(self, task: DownloadTask) -> None:
        if self._on_progress:
            try:
                self._on_progress(task)
            except Exception:
                logger.exception("progress callback raised")

    async def run(self, tasks: list[DownloadTask], resolve_pages: PageResolver) -> RunSummary:
        batch_size = max(1, self._cfg.download.batch_size)
        batches = [tasks[i : i + batch_size] for i in range(0, len(tasks), batch_size)]
        logger.info(
            "Starting download: %d chapter(s) in %d batch(es) of up to %d",
            len(tasks),
            len(batches),
            batch_size,
        )
        for bi, batch in enumerate(batches, start=1):
            if not await self._control.checkpoint():
                logger.info("Download stopped before batch %d/%d", bi, len(batches))
                break
            logger.info("Batch %d/%d (%d chapters)", bi, len(batches), len(batch))
            await asyncio.gather(*(self._download_task(t, resolve_pages) for t in batch))

        if self._control.stopped:
            logger.info("Stop requested - skipping the recovery pass")
            for task in tasks:
                if task.status == "queued":
                    task.status = "stopped"
                    self._notify(task)
        else:
            await self._recover_incomplete(tasks, resolve_pages)

        summary = self._summarise(tasks)
        summary.stopped = self._control.stopped
        self._report(summary)
        return summary

    def _summarise(self, tasks: list[DownloadTask]) -> RunSummary:
        summary = RunSummary(total=len(tasks))
        for task in tasks:
            if task.status == "stopped":
                summary.stopped_chapters += 1
                continue
            if task.saved and task.complete:
                summary.saved += 1
                if task.skipped:
                    summary.skipped += 1
                if task.recovered_pages:
                    summary.recovered += 1
                if task.fallback_source_id:
                    summary.fallback_saved += 1
                continue
            if task.saved:
                reason = (
                    f"{task.pages_saved}/{task.total_pages} pages - "
                    f"{task.missing_pages} page(s) still missing after retries and fallback"
                )
            else:
                reason = task.error or "0 images downloaded"
            summary.failed.append(f"{task.manga_title} - {task.chapter.title}: {reason}")
        return summary

    def _report(self, summary: RunSummary) -> None:
        """Log the explicit end-of-run verdict, after every recovery has run."""
        if summary.stopped:
            logger.info(
                "DOWNLOAD RUN STOPPED BY USER: %d/%d chapter(s) saved, %d stopped "
                "before finishing.",
                summary.saved,
                summary.total,
                summary.stopped_chapters,
            )
            return
        if summary.ok:
            via = ""
            if summary.recovered:
                via = f", {summary.recovered} completed by recovery"
            if summary.skipped:
                via += f", {summary.skipped} already on disk"
            logger.info(
                "Download run complete: %d/%d chapter(s) saved%s.",
                summary.saved,
                summary.total,
                via,
            )
            return
        logger.warning(
            "DOWNLOAD RUN COMPLETE WITH FAILURES: %d of %d chapter(s) are incomplete "
            "even after the recovery pass%s. These chapters are missing pages on disk:",
            len(summary.failed),
            summary.total,
            " (cross-source fallback was available)" if self._fallback is not None else "",
        )
        for line in summary.failed:
            logger.warning("  INCOMPLETE -> %s", line)

    async def _download_task(self, task: DownloadTask, resolve_pages: PageResolver) -> None:
        if not await self._control.checkpoint():
            task.status = "stopped"
            task.note = "stopped before it started"
            self._notify(task)
            return

        if self._skip_existing:
            complete, reason = self._writer.completeness(
                task.source_id, task.manga_title, task.chapter
            )
        else:
            complete, reason = False, "skip_existing disabled"

        if complete:
            manifest = self._writer.read_manifest(task.source_id, task.manga_title, task.chapter)
            pages = manifest.pages if manifest else 0
            task.pages_saved = pages
            task.total_pages = pages
            task.completed_pages = pages
            task.complete = True
            task.skipped = True
            task.status = "skipped"
            task.note = "already downloaded"
            self._notify(task)
            logger.info(
                "Skipping %s - %s: already complete on disk (%d pages)",
                task.manga_title,
                task.chapter.title,
                pages,
            )
            return

        if self._skip_existing:
            logger.info(
                "skip-existing: %s - %s - %s -> re-downloading",
                task.manga_title,
                task.chapter.title,
                reason,
            )

        task.status = "running"
        self._notify(task)

        reason = ""
        pages: list[Page] = []
        try:
            pages = list(await resolve_pages(task.chapter))
        except Exception as exc:
            task.server_error = is_server_error(exc)
            reason = (
                "source returned HTTP 5xx while resolving pages"
                if task.server_error
                else f"page resolution failed: {exc}"
            )
            logger.warning("Chapter %s: %s", task.chapter.title, reason)

        results: dict[int, bytes] = {}
        if pages:
            task.total_pages = len(pages)
            self._notify(task)
            results, saw_5xx, expired = await self._fetch_pages(task, list(enumerate(pages)))
            task.server_error = task.server_error or saw_5xx
            if not results:
                if expired >= len(pages):
                    reason = (
                        f"all {len(pages)} signed image url(s) were already expired "
                        "when fetched (the source served stale signatures)"
                    )
                elif task.server_error:
                    reason = "source returned HTTP 5xx for every image"
                else:
                    reason = "no image could be downloaded"
        else:
            reason = reason or "source listed no pages for this chapter"

        if not results and self._fallback is not None:
            results, reason = await self._try_fallback(task, reason)

        if self._control.stopped:
            task.status = "stopped"
            task.note = f"{len(results)} page(s) fetched, nothing written"
            self._notify(task)
            logger.info(
                "Stopped: %s - %s (%d page(s) fetched, not written)",
                task.manga_title,
                task.chapter.title,
                len(results),
            )
            return

        if not results:
            task.pages_saved = 0
            task.complete = False
            task.missing_pages = task.total_pages
            task.status = "error"
            task.error = f"0 images downloaded - not saved ({reason})"
            self._notify(task)
            logger.error("NOT SAVED: %s - %s: %s", task.manga_title, task.chapter.title, reason)
            return

        try:
            written = self._writer.write(
                task.formats,
                task.source_id,
                task.manga_title,
                task.chapter,
                sorted(results.items()),
                expected_pages=task.total_pages,
                fallback_source_id=task.fallback_source_id,
            )
        except Exception as exc:
            task.pages_saved = 0
            task.complete = False
            task.status = "error"
            task.error = f"write failed - not saved: {exc}"
            self._notify(task)
            logger.exception("Chapter write failed: %s", task.chapter.title)
            return

        if written <= 0:
            task.pages_saved = 0
            task.complete = False
            task.status = "error"
            task.error = "0 images written - not saved"
            self._notify(task)
            logger.error("NOT SAVED: %s - %s", task.manga_title, task.chapter.title)
            return

        self._set_progress_state(task, written)

    def _set_progress_state(self, task: DownloadTask, written: int) -> None:
        """Record how complete the chapter is and tell the UI."""
        task.pages_saved = written
        task.missing_pages = max(task.total_pages - written, 0)
        task.complete = task.missing_pages == 0
        if task.complete:
            task.status = "done"
        else:
            task.status = "partial"
            logger.warning(
                "Chapter %s saved partially (%d/%d pages); recovery will retry after the run",
                task.chapter.title,
                written,
                task.total_pages,
            )
        self._notify(task)
        logger.info(
            "Chapter %s: %d/%d pages%s",
            task.chapter.title,
            written,
            task.total_pages,
            f" [{task.note}]" if task.note else "",
        )

    async def _recover_incomplete(
        self, tasks: list[DownloadTask], resolve_pages: PageResolver
    ) -> None:
        """Second chance for pages that failed during the run.

        Runs once every batch has finished. Chapters that finished short of
        pages get their missing pages retried; chapters that saved nothing at
        all are re-resolved and fetched whole again - the common cause is a
        source that handed out a dead batch of signed URLs, and a fresh
        resolution can mint working ones. Anything still absent is then asked
        from another source. Whatever is recovered is merged into the chapter
        already on disk.
        """
        incomplete = [t for t in tasks if t.saved and not t.complete]
        unwritten = [t for t in tasks if not t.saved]
        if not incomplete and not unwritten:
            return
        logger.info(
            "Recovery pass: %d chapter(s) short of pages, %d saved nothing",
            len(incomplete),
            len(unwritten),
        )
        for task in incomplete:
            if not await self._control.checkpoint():
                return
            try:
                await self._recover_chapter(task, resolve_pages)
            except Exception as exc:
                logger.warning(
                    "Recovery failed for %s - %s: %s",
                    task.manga_title,
                    task.chapter.title,
                    exc,
                )
        for task in unwritten:
            if not await self._control.checkpoint():
                return
            try:
                await self._recover_unwritten(task, resolve_pages)
            except Exception as exc:
                logger.warning(
                    "Recovery failed for %s - %s: %s",
                    task.manga_title,
                    task.chapter.title,
                    exc,
                )

    async def _recover_unwritten(self, task: DownloadTask, resolve_pages: PageResolver) -> None:
        """Re-resolve a chapter that saved 0 pages and fetch it whole again.

        No cross-source step here: the whole-chapter fallback already had its
        chance during the run, and this pass exists for the case where the
        source's own URLs work again (regenerated page cache, transient
        resolution failure).
        """
        try:
            pages = list(await resolve_pages(task.chapter))
        except Exception as exc:
            logger.warning("Recovery: could not re-resolve %s: %s", task.chapter.title, exc)
            return
        if not pages:
            logger.warning(
                "Recovery: %s - %s still lists no pages",
                task.manga_title,
                task.chapter.title,
            )
            return

        logger.warning(
            "Chapter %s - %s: re-resolved %d page(s), fetching the whole chapter again",
            task.manga_title,
            task.chapter.title,
            len(pages),
        )
        task.total_pages = len(pages)
        results, _saw_5xx, expired = await self._fetch_pages(
            task, list(enumerate(pages)), attempts=self._recovery_attempts, count=False
        )
        if not results:
            if expired >= len(pages):
                logger.warning(
                    "Recovery: %s - %s re-resolved to %d url(s) that were expired again; "
                    "re-run the download later, once the source regenerates its page",
                    task.manga_title,
                    task.chapter.title,
                    len(pages),
                )
            return

        written = self._writer.write(
            task.formats,
            task.source_id,
            task.manga_title,
            task.chapter,
            sorted(results.items()),
            expected_pages=len(pages),
            fallback_source_id=task.fallback_source_id,
        )
        if written <= 0:
            return
        task.recovered_pages += written
        task.completed_pages = written
        self._set_progress_state(task, written)
        if task.complete:
            logger.info(
                "Chapter %s - %s: complete after recovery (%d pages)",
                task.manga_title,
                task.chapter.title,
                written,
            )

    async def _recover_chapter(self, task: DownloadTask, resolve_pages: PageResolver) -> None:
        manifest = self._writer.read_manifest(task.source_id, task.manga_title, task.chapter)
        if manifest is None or not manifest.missing:
            return
        missing = list(manifest.missing)
        logger.warning(
            "Chapter %s - %s: retrying %d missing page(s) %s",
            task.manga_title,
            task.chapter.title,
            len(missing),
            missing[:12],
        )

        recovered: dict[int, bytes] = {}

        try:
            pages = list(await resolve_pages(task.chapter))
        except Exception as exc:
            logger.warning("Recovery: could not re-resolve %s: %s", task.chapter.title, exc)
            pages = []
        if pages:
            wanted = [(i, pages[i]) for i in missing if 0 <= i < len(pages)]
            if wanted:
                recovered.update(await self._fetch_wanted(task, wanted, self._recovery_attempts))

        still = [i for i in missing if i not in recovered]
        if still and self._fallback is not None:
            fallback = await self._find_fallback(task)
            if fallback is not None:
                wanted = [(i, fallback.pages[i]) for i in still if i < len(fallback.pages)]
                if wanted:
                    from_other = await self._fetch_wanted(task, wanted, self._recovery_attempts)
                    if from_other:
                        recovered.update(from_other)
                        task.fallback_source_id = fallback.source_id
                        logger.info(
                            "Chapter %s: %d page(s) recovered from %s",
                            task.chapter.title,
                            len(from_other),
                            fallback.source_id,
                        )

        if not recovered:
            logger.warning(
                "Chapter %s - %s: %d page(s) could not be recovered from any source",
                task.manga_title,
                task.chapter.title,
                len(missing),
            )
            return

        self._writer.add_pages(
            task.formats,
            task.source_id,
            task.manga_title,
            task.chapter,
            sorted(recovered.items()),
            fallback_source_id=task.fallback_source_id,
        )
        task.recovered_pages += len(recovered)
        task.completed_pages = min(task.completed_pages + len(recovered), task.total_pages)

        updated = self._writer.read_manifest(task.source_id, task.manga_title, task.chapter)
        written = len(updated.saved) if updated else task.pages_saved + len(recovered)
        self._set_progress_state(task, written)
        if task.complete:
            logger.info(
                "Chapter %s - %s: complete after recovery (%d pages)",
                task.manga_title,
                task.chapter.title,
                written,
            )

    async def _fetch_wanted(
        self, task: DownloadTask, wanted: list[tuple[int, Page]], attempts: int
    ) -> dict[int, bytes]:
        """Fetch an explicit `(position, page)` list without touching progress."""
        results, _saw_5xx, _expired = await self._fetch_pages(
            task, wanted, attempts=attempts, count=False
        )
        return results

    async def _find_fallback(self, task: DownloadTask) -> FallbackChapter | None:
        if self._fallback is None:
            return None
        try:
            return await self._fallback.find_chapter(task.manga_title, task.chapter, task.source_id)
        except Exception as exc:
            logger.warning("Chapter %s: fallback lookup failed: %s", task.chapter.title, exc)
            return None

    async def _try_fallback(self, task: DownloadTask, reason: str) -> tuple[dict[int, bytes], str]:
        """Ask another source for the whole chapter; returns (results, reason)."""
        logger.warning(
            "Chapter %s: %s - looking for this series on another source",
            task.chapter.title,
            reason,
        )
        fallback = await self._find_fallback(task)
        if fallback is None:
            return {}, f"{reason}; no confident match on another source"

        task.fallback_source_id = fallback.source_id
        task.fallback_title = fallback.manga_title
        task.note = f"via {fallback.source_name}"
        task.total_pages = len(fallback.pages)
        task.completed_pages = 0
        self._notify(task)
        logger.info(
            "Chapter %s: recovered from %s ('%s' -> '%s', matched by %s), %d page(s)",
            task.chapter.title,
            fallback.source_id,
            fallback.manga_title,
            fallback.chapter.title,
            fallback.matched_by,
            len(fallback.pages),
        )

        results, saw_5xx, _expired = await self._fetch_pages(task, list(enumerate(fallback.pages)))
        task.server_error = task.server_error or saw_5xx
        if not results:
            return {}, (f"{reason}; fallback source '{fallback.source_id}' also returned 0 images")
        return results, reason

    async def _fetch_pages(
        self,
        task: DownloadTask,
        wanted: list[tuple[int, Page]],
        attempts: int | None = None,
        count: bool = True,
    ) -> tuple[dict[int, bytes], bool, int]:
        """Fetch `(position, page)` pairs concurrently.

        Returns the bytes keyed by **position in the source's page list**, so the
        writer's filenames never depend on how a plugin numbers its pages, plus
        whether any 5xx was seen and how many URLs carried an already-lapsed
        signature. Expired URLs are never requested: the CDN answers 403 to
        every attempt, so requesting them only burns the pacing budget.
        """
        results: dict[int, bytes] = {}
        server_error = False

        now = time.time()
        live: list[tuple[int, Page]] = []
        expired = 0
        for position, page in wanted:
            expiry = url_expiry(page.image_url)
            if expiry is not None and expiry <= now:
                expired += 1
                logger.warning(
                    "page %d: signed url expired %.1fh ago, skipping fetch: %s",
                    position,
                    (now - expiry) / 3600,
                    page.image_url,
                )
                continue
            live.append((position, page))
        if expired:
            logger.warning(
                "Chapter %s: %d of %d signed url(s) were already expired at fetch time",
                task.chapter.title,
                expired,
                len(wanted),
            )

        async def fetch_one(position: int, page: Page) -> None:
            nonlocal server_error
            outcome = await self._fetch_image(page, position, attempts)
            if outcome.server_error:
                server_error = True
            if outcome.data is not None:
                results[position] = outcome.data
            if count and not self._control.stopped:
                task.completed_pages += 1
                self._notify(task)

        await asyncio.gather(*(fetch_one(position, page) for position, page in live))
        return results, server_error, expired

    async def _fetch_image(
        self, page: Page, position: int, attempts: int | None = None
    ) -> _ImageOutcome:
        headers = {"Referer": page.referer} if page.referer else None
        budget = max(attempts or (self._cfg.download.retries + 1), 1)
        outcome = _ImageOutcome()
        async with self._sem:
            if not await self._control.checkpoint():
                return outcome
            for attempt in range(1, budget + 1):
                try:
                    data = await self._fetcher.get_bytes(page.image_url, headers=headers)
                except Exception as exc:
                    if is_server_error(exc):
                        outcome.server_error = True
                    elif is_permanent_client_error(exc):
                        logger.warning(
                            "page %d attempt %d/%d failed with a permanent client "
                            "error, not retrying: %s",
                            position,
                            attempt,
                            budget,
                            exc,
                        )
                        break
                    logger.warning(
                        "page %d attempt %d/%d failed: %s",
                        position,
                        attempt,
                        budget,
                        exc,
                    )
                else:
                    if not data:
                        logger.warning(
                            "page %d attempt %d/%d returned an empty body",
                            position,
                            attempt,
                            budget,
                        )
                    elif looks_like_text(data):
                        logger.warning(
                            "page %d attempt %d/%d returned %d bytes of text, not an image: %s",
                            position,
                            attempt,
                            budget,
                            len(data),
                            page.image_url,
                        )
                    else:
                        outcome.data = data
                        return outcome
                if attempt < budget:
                    await asyncio.sleep(self._cfg.download.backoff_seconds * attempt)
        logger.error("page %d permanently failed: %s", position, page.image_url)
        return outcome

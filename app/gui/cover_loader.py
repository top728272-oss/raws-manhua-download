"""Async cover image loader with disk/memory caching and proxy support.

Covers are the *only* thing cached on disk (``.cache/covers``, bounded - see
``app.core.cache``); chapter page images are never written there.

A cover answered with 403 (CDN geo-block / WAF) is re-fetched once through the
owning plugin's fetcher via ``fetcher_retry``. The main window wires the signal
up; loaders without that wiring just report failure as before.
"""

from __future__ import annotations

import logging
import re

import httpx
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from app.core.cache import cache_dir, cache_key

logger = logging.getLogger(__name__)


_TRANSFORM_SUFFIX_RE = re.compile(r"!(?:cover|thumb|small|large)-\d+(?=\?|$)")


def resolve_referer(url: str, source_id: str | None, source_referers: dict[str, str]) -> str | None:
    """Resolve the Referer header for a cover URL.

    Pure helper (no Qt) so it is unit-testable. ``url`` is currently unused but
    kept in the signature for future per-host overrides.
    """
    if source_id and source_referers.get(source_id):
        return source_referers[source_id]
    return None


class CoverWorkerSignals(QObject):
    finished = Signal(str, bytes)
    failed = Signal(str, int, str)


class CoverWorker(QRunnable):
    def __init__(
        self,
        url: str,
        signals: CoverWorkerSignals,
        proxy: str | None = None,
        user_agent: str | None = None,
        referer: str | None = None,
        source_id: str | None = None,
    ) -> None:
        super().__init__()
        self.url = url
        self.signals = signals
        self._proxy = proxy or None
        self._ua = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        self._referer = referer
        self._source_id = source_id or ""

    def build_headers(self) -> dict[str, str]:
        """Pure helper (no Qt) so it is unit-testable."""
        headers = {"User-Agent": self._ua}
        if self._referer:
            headers["Referer"] = self._referer
        return headers

    def _emit(self, signal, *args) -> None:
        """Emit, ignoring a signal source that was already destroyed.

        Workers outlive the UI during shutdown (or when a card is removed while
        its cover is still loading); PySide raises RuntimeError in that case and
        the traceback is pure noise.
        """
        try:
            signal.emit(*args)
        except RuntimeError:
            pass

    def run(self) -> None:
        headers = self.build_headers()
        try:
            with httpx.Client(
                headers=headers,
                proxy=self._proxy,
                follow_redirects=True,
                timeout=10.0,
            ) as client:
                if "#mimei" in self.url:
                    from app.plugins.mimei import decrypt_image

                    resp = client.get(self.url.split("#", 1)[0])
                    if resp.status_code == 200 and resp.content:
                        self._emit(self.signals.finished, self.url, decrypt_image(resp.content))
                        return
                    self._emit(self.signals.failed, self.url, resp.status_code, self._source_id)
                    return

                if "/break_" in self.url and ("cover_pc" in self.url or "/book/" in self.url):
                    from app.plugins.comicbox import decrypt_and_unscramble_image

                    base_part = re.sub(
                        r"\.(jpg|jpeg|png|avif|webp)(\?.*)?$", "", self.url, flags=re.I
                    )
                    r0 = client.get(f"{base_part}.b_0")
                    r1 = client.get(f"{base_part}.b_1")
                    if (
                        r0.status_code == 200
                        and r1.status_code == 200
                        and r0.content
                        and r1.content
                    ):
                        decrypted = decrypt_and_unscramble_image(r0.content + r1.content)
                        self._emit(self.signals.finished, self.url, decrypted)
                        return

                resp = client.get(self.url)
                if resp.status_code == 200 and resp.content:
                    self._emit(self.signals.finished, self.url, resp.content)
                    return

                retry_url = self._canonical_url()
                if retry_url is not None:
                    resp = client.get(retry_url)
                    if resp.status_code == 200 and resp.content:
                        self._emit(self.signals.finished, self.url, resp.content)
                        return
                self._emit(self.signals.failed, self.url, resp.status_code, self._source_id)
        except Exception as exc:
            logger.debug("Cover load failed for %s: %s", self.url, exc)
            status = getattr(getattr(exc, "response", None), "status_code", 0) or 0
            self._emit(self.signals.failed, self.url, status, self._source_id)

    def _canonical_url(self) -> str | None:
        """Https URL of the same image without the CDN transform suffix, or None
        when the URL is already canonical (a retry would be identical)."""
        url = self.url
        if url.startswith("http://"):
            url = "https://" + url[len("http://") :]
        elif not url.startswith("https://"):
            return None
        stripped = _TRANSFORM_SUFFIX_RE.sub("", url)
        return stripped if stripped != url else None


class CoverLoader(QObject):
    loaded = Signal(str, bytes)
    failed = Signal(str)

    fetcher_retry = Signal(str, str)
    retry_done = Signal(str, bytes)
    retry_dead = Signal(str)

    def __init__(
        self,
        proxy: str | None = None,
        user_agent: str | None = None,
        source_referers: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._proxy = proxy
        self._ua = user_agent
        self._source_referers = source_referers or {}
        self._cache_dir = cache_dir()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._mem_cache: dict[str, bytes] = {}
        self._pending: set[str] = set()
        self._pool = QThreadPool.globalInstance()
        self._retry_enabled = False
        self._retried: set[str] = set()
        self.retry_done.connect(self._on_retry_done)
        self.retry_dead.connect(self._on_retry_dead)

    def enable_fetcher_retry(self) -> None:
        self._retry_enabled = True

    def _get_hash(self, url: str) -> str:
        return cache_key(url)

    def load(self, url: str, source_id: str | None = None) -> None:
        if not url:
            return
        if url in self._mem_cache:
            self.loaded.emit(url, self._mem_cache[url])
            return

        disk_file = self._cache_dir / f"{self._get_hash(url)}.jpg"
        if disk_file.exists():
            try:
                data = disk_file.read_bytes()
                self._mem_cache[url] = data
                self.loaded.emit(url, data)
                return
            except Exception:
                pass

        if url in self._pending:
            return
        self._pending.add(url)

        signals = CoverWorkerSignals()
        signals.finished.connect(self._on_worker_finished)
        signals.failed.connect(self._on_worker_failed)
        worker = CoverWorker(
            url,
            signals,
            proxy=self._proxy,
            user_agent=self._ua,
            referer=resolve_referer(url, source_id, self._source_referers),
            source_id=source_id,
        )
        self._pool.start(worker)

    def _store(self, url: str, data: bytes) -> None:
        self._mem_cache[url] = data
        try:
            disk_file = self._cache_dir / f"{self._get_hash(url)}.jpg"
            disk_file.write_bytes(data)
        except Exception:
            pass

    def _on_worker_finished(self, url: str, data: bytes) -> None:
        self._pending.discard(url)
        self._store(url, data)
        self.loaded.emit(url, data)

    def _on_worker_failed(self, url: str, status: int, source_id: str) -> None:
        self._pending.discard(url)

        if status == 403 and self._retry_enabled and url not in self._retried:
            self._retried.add(url)
            self._pending.add(url)
            self.fetcher_retry.emit(url, source_id)
            return
        self.failed.emit(url)

    def _on_retry_done(self, url: str, data: bytes) -> None:
        self._retried.discard(url)
        self._pending.discard(url)
        self._store(url, data)
        self.loaded.emit(url, data)

    def _on_retry_dead(self, url: str) -> None:
        self._retried.discard(url)
        self._pending.discard(url)
        self.failed.emit(url)

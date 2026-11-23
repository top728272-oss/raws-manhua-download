"""Image cache policy - the GUI cover cache, and the in-memory page cache.

``.cache/covers/`` holds **covers only**. It used to also receive every page
image of every chapter (via ``HttpFetcher.get_bytes``), which is what let it grow
past two million files: the key is ``md5(url)`` *including the query string*, and
many sources serve signed, expiring page URLs, so re-resolving a page minted a new
key - and therefore a new file - for the same image. Page bytes now live in memory
for the duration of a run (``MemoryBytesCache``) and are never written to disk;
the downloaded library is the real store.

Everything here is stdlib + ``app.config``, so both ``app.core.fetcher`` and
``app.gui.cover_loader`` can import it without a layering cycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from app.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

CACHE_DIR = PROJECT_ROOT / ".cache" / "covers"

_STATE_FILE = PROJECT_ROOT / ".cache" / "covers.state.json"

_STATE_MAX_AGE_SECONDS = 12 * 60 * 60


def cache_dir() -> Path:
    """The on-disk cover cache directory."""
    return CACHE_DIR


def cache_key(url: str) -> str:
    """Filename stem for a cached URL.

    The scheme is deliberately unchanged from the original implementation - the
    *whole* URL is hashed, query string included - so existing entries stay valid.
    """
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def cache_file(url: str, directory: Path | None = None) -> Path:
    """Where ``url``'s cached bytes live."""
    return (directory or CACHE_DIR) / f"{cache_key(url)}.jpg"


@dataclass
class PruneResult:
    """What a prune or clear did."""

    files_removed: int = 0
    bytes_freed: int = 0
    files_kept: int = 0
    bytes_after: int = 0

    @property
    def bytes_freed_mb(self) -> float:
        return self.bytes_freed / 1_000_000

    @property
    def bytes_after_mb(self) -> float:
        return self.bytes_after / 1_000_000


def _entries(directory: Path) -> list[tuple[Path, int, float]]:
    """One pass over ``directory``: ``(path, size, mtime)`` per regular file.

    A missing directory is simply empty. Individual stat failures are skipped so
    one locked file can't abort a prune of a million others.
    """
    found: list[tuple[Path, int, float]] = []
    try:
        with os.scandir(directory) as it:
            for entry in it:
                try:
                    if not entry.is_file():
                        continue
                    stat = entry.stat()
                except OSError:
                    continue
                found.append((Path(entry.path), stat.st_size, stat.st_mtime))
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("Could not scan cache directory %s: %s", directory, exc)
        return []
    return found


def size_bytes(directory: Path | None = None) -> int:
    """Total size of the cache in bytes."""
    return sum(size for _, size, _ in _entries(directory or CACHE_DIR))


def count(directory: Path | None = None) -> int:
    """Number of files in the cache."""
    return len(_entries(directory or CACHE_DIR))


def prune(max_bytes: int, directory: Path | None = None) -> PruneResult:
    """Delete least-recently-modified files until the cache fits in ``max_bytes``.

    LRU by mtime: the covers you looked at most recently are the ones kept. One
    ``scandir`` pass collects everything first, so this stays linear even when the
    directory holds millions of entries.
    """
    target = directory or CACHE_DIR
    entries = _entries(target)
    total = sum(size for _, size, _ in entries)

    if not entries or total <= max_bytes:
        result = PruneResult(files_kept=len(entries), bytes_after=total)
        _write_state(result, target)
        return result

    entries.sort(key=lambda item: item[2])

    removed = 0
    freed = 0
    remaining = total
    for path, size, _ in entries:
        if remaining <= max_bytes:
            break
        try:
            path.unlink()
        except OSError as exc:
            logger.debug("Could not evict %s: %s", path, exc)
            continue
        removed += 1
        freed += size
        remaining -= size

    result = PruneResult(
        files_removed=removed,
        bytes_freed=freed,
        files_kept=len(entries) - removed,
        bytes_after=remaining,
    )
    _write_state(result, target)
    return result


def clear(directory: Path | None = None) -> PruneResult:
    """Delete every cached file. Returns what was removed."""
    target = directory or CACHE_DIR
    entries = _entries(target)
    total = sum(size for _, size, _ in entries)

    removed = 0
    freed = 0
    for path, size, _ in entries:
        try:
            path.unlink()
        except OSError as exc:
            logger.debug("Could not delete %s: %s", path, exc)
            continue
        removed += 1
        freed += size

    result = PruneResult(
        files_removed=removed,
        bytes_freed=freed,
        files_kept=len(entries) - removed,
        bytes_after=total - freed,
    )
    _write_state(result, target)
    return result


def maybe_prune(max_bytes: int, directory: Path | None = None) -> PruneResult | None:
    """Prune only when the cache is absent, stale, or known to be over the cap.

    Returns ``None`` when the cheap check decided there was nothing to do - the
    common case, and the reason a launch does not walk a huge directory every time.
    """
    target = directory or CACHE_DIR
    if not target.exists():
        return None
    if _state_fresh(max_bytes, target):
        return None
    return prune(max_bytes, target)


def _state_fresh(max_bytes: int, directory: Path) -> bool:
    """True when a recent prune already recorded this directory as under the cap."""
    try:
        raw = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        if raw.get("directory") != str(directory):
            return False
        if time.time() - float(raw.get("at", 0)) > _STATE_MAX_AGE_SECONDS:
            return False
        return int(raw.get("bytes_after", 0)) <= max_bytes
    except Exception:
        return False


def _write_state(result: PruneResult, directory: Path) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(
            json.dumps(
                {
                    "at": time.time(),
                    "directory": str(directory),
                    "bytes_after": result.bytes_after,
                    "files_kept": result.files_kept,
                }
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("Could not write cache state file: %s", exc)


class MemoryBytesCache:
    """Bounded LRU cache of fetched image bytes, held in memory for one run.

    Bounded by both entry count and total bytes, so a run that touches many large
    pages cannot balloon the process. Deliberately not thread-safe: it is only
    touched from the asyncio event loop, where the synchronous ``get``/``put``
    calls are atomic under the GIL.
    """

    def __init__(self, max_entries: int = 256, max_bytes: int = 64 * 1024 * 1024) -> None:
        self._max_entries = max(int(max_entries), 1)
        self._max_bytes = max(int(max_bytes), 1)
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._bytes = 0

    def get(self, key: str) -> bytes | None:
        data = self._items.get(key)
        if data is None:
            return None
        self._items.move_to_end(key)
        return data

    def put(self, key: str, data: bytes) -> None:
        """Store ``data``; an empty body is never cached."""
        if not data:
            return
        existing = self._items.pop(key, None)
        if existing is not None:
            self._bytes -= len(existing)
        self._items[key] = data
        self._bytes += len(data)
        self._evict()

    def clear(self) -> None:
        self._items.clear()
        self._bytes = 0

    def __len__(self) -> int:
        return len(self._items)

    @property
    def size_bytes(self) -> int:
        return self._bytes

    def _evict(self) -> None:
        while self._items and (
            len(self._items) > self._max_entries or self._bytes > self._max_bytes
        ):
            _, evicted = self._items.popitem(last=False)
            self._bytes -= len(evicted)

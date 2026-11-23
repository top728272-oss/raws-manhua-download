"""Per-chapter on-disk manifest.

Every chapter the writer produces gets a small JSON sidecar recording what the
download was supposed to contain and what actually landed. Manifests live
outside the library so the chapter folders stay exactly as downloaded::

    downloads/.manhua-dl/{source}/{series}/{chapter}.json
    downloads/{source}/{series}/{chapter}/001.webp

The page images alone cannot answer the two questions the engine needs:

* **Is this chapter already complete?** - so a re-run can skip it outright
  instead of downloading every page again (accidentally hitting "Download
  selected" on a whole series must not re-fetch it).
* **Which pages are still missing?** - so the engine can go back for just those
  pages after the run finishes, and ask another source for them if they still
  refuse to download.

The images are the source of truth for *content*; the manifest is the source of
truth for *expectations* (how many pages there should be). A manifest is only
trusted when every artifact it names is still on disk, so deleting a page by
hand correctly makes the chapter look incomplete again.

Deleting `downloads/.manhua-dl/` is safe and simply makes every chapter look
un-downloaded - useful when you *want* a full re-download.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


STATE_DIR = ".manhua-dl"
MANIFEST_VERSION = 1


def ensure_state_dir(root: Path) -> Path:
    """Create (and return) the manifest tree root under an output directory.

    Called at startup so `downloads/.manhua-dl/` always exists and is therefore
    visible and easy to back up. Its absence is otherwise silent - and a missing
    manifest makes every chapter look un-downloaded, so a whole library gets
    re-fetched.
    """
    path = root / STATE_DIR
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Could not create manifest directory %s: %s", path, exc)
    return path


@dataclass
class ChapterManifest:
    """What a chapter download was expected to produce, and what it produced."""

    source_id: str
    chapter_ref: str
    series: str
    chapter: str

    pages: int

    saved: list[int] = field(default_factory=list)

    missing: list[int] = field(default_factory=list)

    files: list[str] = field(default_factory=list)

    fallback_source_id: str | None = None
    version: int = MANIFEST_VERSION
    completed_at: str = ""

    @property
    def complete(self) -> bool:
        """True when every expected page is accounted for."""
        return not self.missing and len(self.saved) == self.pages

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "source_id": self.source_id,
            "chapter_ref": self.chapter_ref,
            "series": self.series,
            "chapter": self.chapter,
            "pages": self.pages,
            "saved": sorted(self.saved),
            "missing": sorted(self.missing),
            "files": list(self.files),
            "fallback_source_id": self.fallback_source_id,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> ChapterManifest | None:
        if not isinstance(data, dict):
            return None
        try:
            pages = int(data.get("pages") or 0)
        except (TypeError, ValueError):
            return None
        if pages <= 0:
            return None

        def indices(key: str) -> list[int]:
            raw = data.get(key) or []
            if not isinstance(raw, list):
                return []
            out: list[int] = []
            for value in raw:
                try:
                    out.append(int(value))
                except (TypeError, ValueError):
                    continue
            return out

        files = data.get("files") or []
        return cls(
            source_id=str(data.get("source_id") or ""),
            chapter_ref=str(data.get("chapter_ref") or ""),
            series=str(data.get("series") or ""),
            chapter=str(data.get("chapter") or ""),
            pages=pages,
            saved=indices("saved"),
            missing=indices("missing"),
            files=[str(f) for f in files] if isinstance(files, list) else [],
            fallback_source_id=data.get("fallback_source_id") or None,
            version=int(data.get("version") or MANIFEST_VERSION),
            completed_at=str(data.get("completed_at") or ""),
        )


def read_manifest(path: Path) -> ChapterManifest | None:
    """Load the manifest at `path`; None when absent or unreadable."""
    if not path.is_file():
        return None
    try:
        return ChapterManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        logger.warning("Unreadable chapter manifest %s: %s", path, exc)
        return None


def write_manifest(path: Path, manifest: ChapterManifest) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Could not write chapter manifest %s: %s", path, exc)


def artifacts_present(chapter_dir: Path, manifest: ChapterManifest) -> bool:
    """True when every file the manifest names still exists on disk."""
    if not manifest.files:
        return False
    return all((chapter_dir / name).is_file() for name in manifest.files)

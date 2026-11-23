"""Chapter manifests: what makes a chapter count as already downloaded."""

from __future__ import annotations

import json

from app.core.library import (
    ChapterManifest,
    artifacts_present,
    read_manifest,
    write_manifest,
)


def _manifest(**kwargs) -> ChapterManifest:
    base = {
        "source_id": "mkzhan",
        "chapter_ref": "c1",
        "series": "系列",
        "chapter": "第1话",
        "pages": 3,
    }
    base.update(kwargs)
    return ChapterManifest(**base)


def test_complete_requires_every_expected_page() -> None:
    assert _manifest(saved=[0, 1, 2]).complete is True
    assert _manifest(saved=[0, 1], missing=[2]).complete is False
    assert _manifest(saved=[0, 1, 2], missing=[2]).complete is False
    assert _manifest(saved=[]).complete is False


def test_round_trip(tmp_path) -> None:
    path = tmp_path / "state" / "chapter.json"
    original = _manifest(
        saved=[0, 2],
        missing=[1],
        files=["001.webp", "003.webp"],
        fallback_source_id="copymanga",
        completed_at="2026-10-01T00:00:00",
    )
    write_manifest(path, original)

    loaded = read_manifest(path)
    assert loaded is not None
    assert loaded == original
    assert json.loads(path.read_text(encoding="utf-8"))["missing"] == [1]


def test_read_manifest_is_forgiving(tmp_path) -> None:
    assert read_manifest(tmp_path / "missing.json") is None

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert read_manifest(bad) is None

    # A manifest without a usable page count is not trusted.
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"pages": 0}), encoding="utf-8")
    assert read_manifest(empty) is None


def test_artifacts_present_checks_the_disk(tmp_path) -> None:
    manifest = _manifest(saved=[0, 1], files=["001.webp", "002.webp"])
    assert artifacts_present(tmp_path, manifest) is False

    (tmp_path / "001.webp").write_bytes(b"a")
    (tmp_path / "002.webp").write_bytes(b"b")
    assert artifacts_present(tmp_path, manifest) is True

    (tmp_path / "002.webp").unlink()
    assert artifacts_present(tmp_path, manifest) is False
    # A manifest naming no files can never vouch for a chapter.
    assert artifacts_present(tmp_path, _manifest(saved=[0])) is False

"""Registry: completed-only dedupe, SQL OR/AND safety, fingerprint."""

from __future__ import annotations

from pathlib import Path

import pytest

from vdl.registry import Registry, SourceType


@pytest.fixture
def reg(tmp_path: Path) -> Registry:
    return Registry(tmp_path / "registry.db")


def test_create_and_complete(reg: Registry) -> None:
    r = reg.create("https://ex.com/a.mp4", source_type=SourceType.DIRECT, title="a")
    assert r.status == "pending"
    done = reg.mark_completed(
        r.id, file_path="/tmp/a.mp4", file_size=100, file_hash="abc"
    )
    assert done.status == "completed"
    assert done.file_hash == "abc"


def test_failed_not_treated_as_downloaded(reg: Registry) -> None:
    r = reg.create("https://ex.com/a.mp4", source_type=SourceType.DIRECT)
    reg.mark_failed(r.id, "boom")
    assert reg.is_downloaded("https://ex.com/a.mp4") is None
    # still findable without completed filter
    found = reg.find_by_url("https://ex.com/a.mp4", completed_only=False)
    assert found is not None
    assert found.status == "failed"


def test_url_or_canonical_requires_completed(reg: Registry) -> None:
    """Regression: WHERE (url OR canonical) AND status=completed parentheses."""
    r = reg.create(
        "https://ex.com/path",
        canonical_url="https://cdn.ex.com/path",
        source_type=SourceType.DIRECT,
    )
    reg.mark_failed(r.id, "nope")
    assert reg.find_by_url("https://ex.com/path", completed_only=True) is None
    assert reg.find_by_url("https://cdn.ex.com/path", completed_only=True) is None

    r2 = reg.create(
        "https://ex.com/path",
        canonical_url="https://cdn.ex.com/path",
        source_type=SourceType.DIRECT,
    )
    reg.mark_completed(r2.id, file_path="/t", file_size=1)
    assert reg.is_downloaded("https://ex.com/path") is not None
    assert reg.is_downloaded("https://cdn.ex.com/path") is not None


def test_fingerprint_priority(reg: Registry) -> None:
    r = reg.create(
        "https://ex.com/old.m3u8",
        source_type=SourceType.M3U8,
        m3u8_fingerprint="fp-same",
    )
    reg.mark_completed(r.id, file_path="/old.mp4", m3u8_fingerprint="fp-same")
    # different URL, same fingerprint → duplicate
    hit = reg.is_downloaded("https://other/new.m3u8", fingerprint="fp-same")
    assert hit is not None
    assert hit.id == r.id


def test_fingerprint_failed_ignored(reg: Registry) -> None:
    r = reg.create(
        "https://ex.com/s.m3u8",
        source_type=SourceType.M3U8,
        m3u8_fingerprint="fp-fail",
    )
    reg.mark_failed(r.id, "ffmpeg broke")
    assert reg.is_downloaded("https://ex.com/s.m3u8", fingerprint="fp-fail") is None


def test_find_by_hash(reg: Registry) -> None:
    r = reg.create("https://ex.com/a.mp4", source_type=SourceType.DIRECT)
    reg.mark_completed(r.id, file_path="/a", file_hash="deadbeef")
    assert reg.find_by_hash("deadbeef") is not None
    assert reg.find_by_hash("nope") is None


def test_update_artifact_and_list_unlimited(reg: Registry) -> None:
    r = reg.create("https://ex.com/a.ts", source_type=SourceType.DIRECT, title="a.ts")
    reg.mark_completed(r.id, file_path="/tmp/a.ts", file_size=10, file_hash="aa")
    done = reg.update_artifact(
        r.id,
        file_path="/tmp/a.mp4",
        file_size=11,
        file_hash="bb",
        content_type="video/mp4",
        title="a.mp4",
        meta_update={"remuxed_from": "/tmp/a.ts"},
    )
    assert done.file_path == "/tmp/a.mp4"
    assert done.file_hash == "bb"
    assert done.meta["remuxed_from"] == "/tmp/a.ts"
    twins = reg.find_all_by_hash("bb")
    assert len(twins) == 1
    assert reg.list_all(limit=None)


def test_list_and_get(reg: Registry) -> None:
    a = reg.create("https://a", source_type=SourceType.DIRECT)
    reg.mark_completed(a.id, file_path="/a")
    b = reg.create("https://b", source_type=SourceType.DIRECT)
    reg.mark_failed(b.id, "x")
    all_rows = reg.list_all(limit=10)
    assert len(all_rows) == 2
    completed = reg.list_all(status="completed")
    assert len(completed) == 1
    assert reg.get(a.id) is not None
    assert reg.get(99999) is None

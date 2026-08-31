"""ts_to_mp4 remux and content-hash dedup jobs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from vdl.config import Config
from vdl.maintenance import (
    SOURCE_HASH_META,
    dedup_groups,
    remux_ts_to_mp4,
    run_dedup,
    run_ts_to_mp4,
    source_hash_of,
)
from vdl.queue import JobKind, JobQueue
from vdl.registry import DownloadRecord, Registry, SourceType
from vdl.util import sha256_file
from vdl.workers import enqueue_startup_maintenance, run_job


def _cfg(tmp_path: Path) -> Config:
    cfg = Config(home=tmp_path)
    cfg.ensure_dirs()
    return cfg


def _complete(reg: Registry, url: str, path: Path, digest: str | None = None):
    rec = reg.create(url, source_type=SourceType.DIRECT, title=path.name)
    size = path.stat().st_size if path.exists() else 0
    return reg.mark_completed(
        rec.id,
        file_path=str(path),
        file_size=size,
        file_hash=digest,
    )


@pytest.fixture
def mpegts(tmp_path: Path) -> Path:
    out = tmp_path / "clip.ts"
    r = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=160x120:rate=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "mpegts",
            str(out),
        ],
        capture_output=True,
    )
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        pytest.skip("ffmpeg mpegts encode failed")
    return out


def test_remux_ts_to_mp4_file(mpegts: Path) -> None:
    dest = remux_ts_to_mp4(mpegts)
    assert dest.suffix == ".mp4"
    assert dest.is_file() and dest.stat().st_size > 0
    assert not mpegts.exists()


def test_run_ts_to_mp4_updates_registry(tmp_path: Path, mpegts: Path) -> None:
    cfg = _cfg(tmp_path)
    src = cfg.download_dir / "a.ts"
    src.write_bytes(mpegts.read_bytes())
    src_hash = sha256_file(src)
    reg = Registry(cfg.db_path)
    rec = _complete(reg, "https://ex.com/a.ts", src, src_hash)
    out = run_ts_to_mp4(cfg, download_id=rec.id)
    assert len(out["converted"]) == 1
    fresh = reg.get(rec.id)
    assert fresh is not None
    assert fresh.file_path and fresh.file_path.endswith(".mp4")
    assert Path(fresh.file_path).is_file()
    assert not src.exists()
    assert fresh.content_type == "video/mp4"
    assert source_hash_of(fresh) == src_hash
    assert fresh.file_hash != src_hash
    assert fresh.file_hash == sha256_file(Path(fresh.file_path))


def test_dedup_collapses_hash_group_and_unlinks_extra(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    payload = b"same-bytes-payload" * 50
    a = cfg.download_dir / "keep.mp4"
    b = cfg.download_dir / "copy.mp4"
    a.write_bytes(payload)
    b.write_bytes(payload)
    digest = "abc123"
    reg = Registry(cfg.db_path)
    keeper = _complete(reg, "https://ex.com/keep.mp4", a, digest)
    extra = _complete(reg, "https://ex.com/copy.mp4", b, digest)
    q = JobQueue(cfg.db_path)
    q.enqueue(JobKind.SCENE_DETECT, priority=40, download_id=extra.id, payload={})

    result = run_dedup(cfg)
    assert len(result["groups"]) == 1
    assert result["groups"][0]["keeper_id"] == keeper.id
    assert extra.id in result["groups"][0]["extra_ids"]
    assert a.is_file()
    assert not b.exists()
    extra_rec = reg.get(extra.id)
    assert extra_rec is not None
    assert extra_rec.status == "completed"
    assert extra_rec.file_path == str(a)
    assert extra_rec.meta.get("dedup_of") == keeper.id
    pending = q.list_jobs(status="pending")
    assert pending == []


def test_dedup_prefers_indexed_keeper(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    payload = b"indexed-bytes" * 40
    older = cfg.download_dir / "old.mp4"
    newer = cfg.download_dir / "new.mp4"
    older.write_bytes(payload)
    newer.write_bytes(payload)
    digest = "deadbeef"
    reg = Registry(cfg.db_path)
    old = _complete(reg, "https://ex.com/old.mp4", older, digest)
    new = _complete(reg, "https://ex.com/new.mp4", newer, digest)
    q = JobQueue(cfg.db_path)
    q.replace_scenes(new.id, [{"start_sec": 0.0, "end_sec": 1.0}])

    result = run_dedup(cfg)
    assert result["groups"][0]["keeper_id"] == new.id
    assert old.id in result["groups"][0]["extra_ids"]
    assert newer.is_file()
    assert not older.exists()


def test_dedup_unlinks_orphan_copy(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    payload = b"orphan-bytes" * 30
    kept = cfg.download_dir / "kept.mp4"
    orphan = cfg.download_dir / "orphan.mp4"
    kept.write_bytes(payload)
    orphan.write_bytes(payload)
    from vdl.util import sha256_file

    digest = sha256_file(kept)
    reg = Registry(cfg.db_path)
    _complete(reg, "https://ex.com/kept.mp4", kept, digest)
    result = run_dedup(cfg)
    assert str(orphan) in result["orphan_unlinked"]
    assert kept.is_file()
    assert not orphan.exists()


def _rec(
    id: int,
    *,
    file_hash: str | None = None,
    source_hash: str | None = None,
    path: str = "/a.mp4",
) -> DownloadRecord:
    meta = {SOURCE_HASH_META: source_hash} if source_hash else {}
    return DownloadRecord(
        id=id,
        url=f"https://ex.com/{id}",
        canonical_url=f"https://ex.com/{id}",
        source_type="direct",
        status="completed",
        file_path=path,
        title=None,
        content_type=None,
        file_size=1,
        file_hash=file_hash,
        parent_page_url=None,
        m3u8_fingerprint=None,
        meta_json=json.dumps(meta) if meta else None,
        created_at="",
        updated_at="",
    )


def test_dedup_groups_source_hash_optional() -> None:
    both_src = [
        _rec(1, file_hash="mp4-a", source_hash="ts-s"),
        _rec(2, file_hash="mp4-b", source_hash="ts-s"),
    ]
    groups = dedup_groups(both_src)
    assert len(groups) == 1
    assert {r.id for r in groups[0]} == {1, 2}

    one_sided = [
        _rec(1, file_hash="mp4-a", source_hash="ts-s"),
        _rec(2, file_hash="ts-s"),  # other side is the raw .ts hash, no meta
    ]
    assert dedup_groups(one_sided) == []

    same_post = [
        _rec(1, file_hash="mp4-a", source_hash="ts-s"),
        _rec(2, file_hash="mp4-a"),
    ]
    groups = dedup_groups(same_post)
    assert len(groups) == 1
    assert {r.id for r in groups[0]} == {1, 2}

    no_meta = [
        _rec(1, file_hash="h"),
        _rec(2, file_hash="h"),
    ]
    groups = dedup_groups(no_meta)
    assert len(groups) == 1


def test_dedup_collapses_same_source_hash_different_mp4(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    a = cfg.download_dir / "a.mp4"
    b = cfg.download_dir / "b.mp4"
    a.write_bytes(b"converted-a" * 20)
    b.write_bytes(b"converted-b" * 20)
    reg = Registry(cfg.db_path)
    keeper = _complete(reg, "https://ex.com/a.ts", a, sha256_file(a))
    extra = _complete(reg, "https://ex.com/b.ts", b, sha256_file(b))
    reg.update_artifact(keeper.id, meta_update={SOURCE_HASH_META: "same-ts"})
    reg.update_artifact(extra.id, meta_update={SOURCE_HASH_META: "same-ts"})

    result = run_dedup(cfg)
    assert len(result["groups"]) == 1
    assert {keeper.id, extra.id} == {
        result["groups"][0]["keeper_id"],
        *result["groups"][0]["extra_ids"],
    }
    # one mp4 remains
    assert a.exists() ^ b.exists()


def test_dedup_one_sided_source_hash_does_not_match_ts_bytes(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    mp4 = cfg.download_dir / "a.mp4"
    ts = cfg.download_dir / "a.ts"
    mp4.write_bytes(b"mp4-bytes" * 20)
    ts.write_bytes(b"ts-origin" * 20)
    reg = Registry(cfg.db_path)
    ts_hash = sha256_file(ts)
    _complete(reg, "https://ex.com/a.mp4", mp4, sha256_file(mp4))
    reg.update_artifact(1, meta_update={SOURCE_HASH_META: ts_hash})
    _complete(reg, "https://ex.com/a.ts", ts, ts_hash)

    result = run_dedup(cfg)
    assert result["groups"] == []
    assert mp4.is_file() and ts.is_file()


def test_dedup_unlinks_leftover_ts_matching_source_hash(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    mp4 = cfg.download_dir / "kept.mp4"
    leftover = cfg.download_dir / "orig.ts"
    mp4.write_bytes(b"mp4-payload" * 30)
    leftover.write_bytes(b"ts-payload" * 30)
    ts_hash = sha256_file(leftover)
    reg = Registry(cfg.db_path)
    rec = _complete(reg, "https://ex.com/kept.mp4", mp4, sha256_file(mp4))
    reg.update_artifact(rec.id, meta_update={SOURCE_HASH_META: ts_hash})
    result = run_dedup(cfg)
    assert str(leftover) in result["orphan_unlinked"]
    assert mp4.is_file()
    assert not leftover.exists()


def test_run_job_dispatches_maintenance(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    q = JobQueue(cfg.db_path)
    jobs = enqueue_startup_maintenance(q)
    remux = q.claim_next()
    assert remux is not None and remux.id == jobs["ts_to_mp4"].id
    result = run_job(cfg, remux)
    q.mark_done(remux.id, result=result)
    dedup = q.claim_next()
    assert dedup is not None and dedup.id == jobs["dedup"].id
    result = run_job(cfg, dedup)
    assert "groups" in result

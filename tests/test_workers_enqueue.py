from __future__ import annotations

from pathlib import Path

from vdl.queue import JobQueue
from vdl.workers import (
    enqueue_dedup,
    enqueue_download,
    enqueue_index_chain,
    enqueue_startup_maintenance,
    enqueue_ts_to_mp4,
)


def test_enqueue_download_and_index_chain(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "r.db")
    j = enqueue_download(q, "https://example.com/a.mp4")
    assert j.kind == "download"
    assert j.priority == 100
    s, c, e = enqueue_index_chain(q, download_id=42)
    assert s.kind == "scene_detect"
    assert c.depends_on_job_id == s.id
    assert e.depends_on_job_id == c.id
    assert c.download_id == 42


def test_enqueue_startup_maintenance_singleton(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "r.db")
    first = enqueue_startup_maintenance(q)
    assert first["ts_to_mp4"].kind == "ts_to_mp4"
    assert first["dedup"].kind == "dedup"
    assert first["ts_to_mp4"].priority == 50
    assert first["dedup"].priority == 45
    assert first["dedup"].depends_on_job_id == first["ts_to_mp4"].id
    second = enqueue_startup_maintenance(q)
    assert second["ts_to_mp4"].id == first["ts_to_mp4"].id
    assert second["dedup"].id == first["dedup"].id


def test_enqueue_ts_to_mp4_per_download_not_sweep(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "r.db")
    sweep = enqueue_ts_to_mp4(q)
    one = enqueue_ts_to_mp4(q, download_id=7)
    assert sweep.id != one.id
    assert one.download_id == 7
    again = enqueue_ts_to_mp4(q, download_id=7)
    assert again.id == one.id
    assert enqueue_dedup(q).kind == "dedup"

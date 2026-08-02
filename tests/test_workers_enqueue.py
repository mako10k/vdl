from __future__ import annotations

from pathlib import Path

from vdl.queue import JobQueue
from vdl.workers import enqueue_download, enqueue_index_chain


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

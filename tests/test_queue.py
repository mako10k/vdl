"""Job queue priority and dependency."""

from __future__ import annotations

from pathlib import Path

from vdl.config import PRIORITY_CLIP, PRIORITY_DOWNLOAD, PRIORITY_EMBED, PRIORITY_SCENE
from vdl.queue import JobKind, JobQueue


def test_download_preferred_over_index(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "r.db")
    emb = q.enqueue(JobKind.EMBED, priority=PRIORITY_EMBED, payload={})
    dl = q.enqueue(JobKind.DOWNLOAD, priority=PRIORITY_DOWNLOAD, payload={"url": "x"})
    claimed = q.claim_next()
    assert claimed is not None
    assert claimed.id == dl.id
    q.mark_done(claimed.id)
    claimed2 = q.claim_next()
    assert claimed2 is not None
    assert claimed2.id == emb.id


def test_dependency_blocks_until_done(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "r.db")
    a = q.enqueue(JobKind.SCENE_DETECT, priority=PRIORITY_SCENE, payload={})
    b = q.enqueue(
        JobKind.CLIP_OPTIMIZE,
        priority=PRIORITY_CLIP,
        depends_on_job_id=a.id,
        payload={},
    )
    # only a is runnable
    c1 = q.claim_next()
    assert c1 is not None and c1.id == a.id
    # b still blocked
    c2 = q.claim_next()
    assert c2 is None
    q.mark_done(a.id)
    c3 = q.claim_next()
    assert c3 is not None and c3.id == b.id


def test_cancel_retry(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "r.db")
    j = q.enqueue(JobKind.DOWNLOAD, priority=100, payload={})
    q.cancel(j.id)
    assert q.get(j.id).status == "cancelled"
    q.retry(j.id)
    assert q.get(j.id).status == "pending"

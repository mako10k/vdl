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


def test_auto_retry_then_fail_cancels_dependents(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "r.db")
    a = q.enqueue(JobKind.SCENE_DETECT, priority=PRIORITY_SCENE, payload={})
    b = q.enqueue(
        JobKind.CLIP_OPTIMIZE,
        priority=PRIORITY_CLIP,
        depends_on_job_id=a.id,
        payload={},
    )
    c = q.enqueue(
        JobKind.EMBED,
        priority=PRIORITY_EMBED,
        depends_on_job_id=b.id,
        payload={},
    )

    # attempt 1 → re-queue
    j1 = q.mark_failed(a.id, "boom1", max_attempts=3)
    assert j1.status == "pending"
    assert j1.attempts == 1
    assert q.get(b.id).status == "pending"

    # attempt 2 → still retry
    j2 = q.mark_failed(a.id, "boom2", max_attempts=3)
    assert j2.status == "pending"
    assert j2.attempts == 2

    # attempt 3 → permanent fail + cascade cancel
    j3 = q.mark_failed(a.id, "boom3", max_attempts=3)
    assert j3.status == "failed"
    assert j3.attempts == 3
    assert q.get(b.id).status == "cancelled"
    assert q.get(c.id).status == "cancelled"
    # dependents no longer claimable
    assert q.claim_next() is None


def test_manual_retry_reopens_cancelled_chain(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "r.db")
    a = q.enqueue(JobKind.SCENE_DETECT, priority=PRIORITY_SCENE, payload={})
    b = q.enqueue(
        JobKind.CLIP_OPTIMIZE,
        priority=PRIORITY_CLIP,
        depends_on_job_id=a.id,
        payload={},
    )
    q.mark_failed(a.id, "x", max_attempts=1)
    assert q.get(a.id).status == "failed"
    assert q.get(b.id).status == "cancelled"
    q.retry(a.id)
    assert q.get(a.id).status == "pending"
    assert q.get(a.id).attempts == 0
    assert q.get(b.id).status == "pending"

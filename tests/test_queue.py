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


def test_retry_moves_behind_waiting_jobs_without_breaking_dependencies(tmp_path):
    q = JobQueue(tmp_path/'r.db')
    a = q.enqueue(JobKind.DOWNLOAD, priority=100)
    b = q.enqueue(JobKind.DOWNLOAD, priority=100)
    child = q.enqueue(JobKind.EMBED, priority=20, depends_on_job_id=a.id)
    assert q.claim_next().id == a.id
    retry = q.mark_failed(a.id, 'timeout', max_attempts=3)
    assert retry.priority < 20
    assert q.claim_next().id == b.id
    q.mark_done(b.id)
    assert q.claim_next().id == a.id
    q.mark_done(a.id)
    assert q.claim_next().id == child.id


def test_attempt_cap_cannot_be_reopened_by_housekeep(tmp_path):
    q = JobQueue(tmp_path/'r.db')
    a = q.enqueue(JobKind.DOWNLOAD, priority=100)
    for _ in range(3):
        q.claim_next()
        q.mark_failed(a.id, 'timeout', max_attempts=3)
    q.housekeep(retry_after_minutes=0, archive_after_minutes=60, max_attempts=3)
    assert q.get(a.id).status == 'failed'
    assert q.get(a.id).attempts == 3


def test_cancelled_job_cannot_be_revived_by_late_attempt_completion(tmp_path):
    q = JobQueue(tmp_path/'r.db')
    a = q.enqueue(JobKind.DOWNLOAD, priority=100)
    q.claim_next()
    q.cancel(a.id)
    assert q.mark_done(a.id, {}).status == 'cancelled'
    assert q.mark_failed(a.id, 'interrupted', max_attempts=3).status == 'cancelled'


def test_processing_conflicts_are_serialized_while_independent_scenes_can_overlap(tmp_path):
    q = JobQueue(tmp_path/'r.db')
    first = q.enqueue(JobKind.CLIP_OPTIMIZE, priority=30, download_id=1)
    second = q.enqueue(JobKind.EMBED, priority=20, download_id=2)
    scene = q.enqueue(JobKind.SCENE_DETECT, priority=10, download_id=3)
    assert q.claim_next().id == first.id
    assert q.claim_next().id == scene.id
    assert q.claim_next() is None
    q.mark_done(first.id)
    assert q.claim_next().id == second.id

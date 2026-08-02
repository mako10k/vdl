"""Queue list display filter + artifact filenames."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from vdl.config import PRIORITY_DOWNLOAD
from vdl.queue import JobKind, JobQueue
from vdl.util import artifact_filename


def test_artifact_filename_generic_index_differs_by_url() -> None:
    a = artifact_filename("https://cdn.a.example/foo/index.m3u8", ext=".mp4")
    b = artifact_filename("https://cdn.b.example/bar/index.m3u8", ext=".mp4")
    assert a != b
    assert a.endswith(".mp4") and b.endswith(".mp4")
    assert "index" not in a.split("_")[0].lower() or "_" in a


def test_artifact_filename_stable_for_same_url() -> None:
    u = "https://cdn.example.com/path/video.mp4?x=1"
    assert artifact_filename(u) == artifact_filename(u)


def test_list_jobs_display_hides_old_done(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "r.db")
    # old done downloads
    old_ids = []
    for i in range(3):
        j = q.enqueue(JobKind.DOWNLOAD, priority=PRIORITY_DOWNLOAD, payload={"u": str(i)})
        claimed = q.claim_next()
        assert claimed
        q.mark_done(claimed.id, {})
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with q._conn() as conn:
            conn.execute(
                "UPDATE jobs SET finished_at=?, updated_at=? WHERE id=?",
                (old, old, claimed.id),
            )
        old_ids.append(claimed.id)

    # recent done
    j_recent = q.enqueue(JobKind.DOWNLOAD, priority=PRIORITY_DOWNLOAD, payload={"u": "new"})
    c = q.claim_next()
    assert c
    q.mark_done(c.id, {})

    # pending
    pend = q.enqueue(JobKind.DOWNLOAD, priority=PRIORITY_DOWNLOAD, payload={"u": "p"})

    rows, meta = q.list_jobs_display(limit=20, done_within_minutes=10)
    ids = {r.id for r in rows}
    assert pend.id in ids
    assert c.id in ids  # recent done
    # old dones fully hidden (no latest-of-kind keep)
    for oid in old_ids:
        assert oid not in ids
    assert meta["filtered"] is True
    assert meta["hidden_done"] >= 2


def test_housekeep_retries_then_archives(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "r.db")
    j = q.enqueue(JobKind.DOWNLOAD, priority=PRIORITY_DOWNLOAD, payload={"u": "x"})
    q.mark_failed(j.id, "frame extract failed at 1s", max_attempts=1)
    assert q.get(j.id).status == "failed"
    # cool-down elapsed via backdate
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with q._conn() as conn:
        conn.execute(
            "UPDATE jobs SET finished_at=?, updated_at=? WHERE id=?",
            (old, old, j.id),
        )
    stats = q.housekeep(
        archive_after_minutes=60,
        retry_after_minutes=5,
        auto_retry=True,
        max_attempts=3,
    )
    assert stats["retried"] == 1
    assert q.get(j.id).status == "pending"
    # housekeep does not wipe attempts to 0 (single extra shot)
    assert q.get(j.id).attempts == 1

    # fail again permanently and age past archive window
    q.mark_failed(j.id, "frame extract failed at 1s", max_attempts=1)
    very_old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    with q._conn() as conn:
        conn.execute(
            "UPDATE jobs SET finished_at=?, updated_at=?, housekeep_retried=1 WHERE id=?",
            (very_old, very_old, j.id),
        )
    stats2 = q.housekeep(archive_after_minutes=60, retry_after_minutes=5, auto_retry=True)
    assert stats2["archived"] >= 1
    assert q.get(j.id).status == "archived"


def test_archive_skips_parent_with_pending_child(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "r.db")
    a = q.enqueue(JobKind.SCENE_DETECT, priority=40, payload={})
    b = q.enqueue(
        JobKind.CLIP_OPTIMIZE,
        priority=30,
        depends_on_job_id=a.id,
        payload={},
    )
    claimed = q.claim_next()
    assert claimed and claimed.id == a.id
    q.mark_done(a.id, {})
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    with q._conn() as conn:
        conn.execute(
            "UPDATE jobs SET finished_at=?, updated_at=? WHERE id=?",
            (old, old, a.id),
        )
    stats = q.housekeep(archive_after_minutes=60, auto_retry=False)
    assert a.id not in stats["archived_ids"]
    assert q.get(a.id).status == "done"
    # child still claimable
    c = q.claim_next()
    assert c is not None and c.id == b.id


def test_claim_after_archived_success_parent(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "r.db")
    a = q.enqueue(JobKind.SCENE_DETECT, priority=40, payload={})
    b = q.enqueue(
        JobKind.CLIP_OPTIMIZE,
        priority=30,
        depends_on_job_id=a.id,
        payload={},
    )
    q.claim_next()
    q.mark_done(a.id, {})
    with q._conn() as conn:
        conn.execute(
            "UPDATE jobs SET status='archived', progress_pct=100, error=NULL WHERE id=?",
            (a.id,),
        )
    c = q.claim_next()
    assert c is not None and c.id == b.id

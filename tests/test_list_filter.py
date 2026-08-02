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
    for i in range(3):
        j = q.enqueue(JobKind.DOWNLOAD, priority=PRIORITY_DOWNLOAD, payload={"u": str(i)})
        claimed = q.claim_next()
        assert claimed
        q.mark_done(claimed.id, {})
        # backdate finished_at
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with q._conn() as conn:
            conn.execute(
                "UPDATE jobs SET finished_at=?, updated_at=? WHERE id=?",
                (old, old, claimed.id),
            )

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
    # old dones hidden except latest-of-kind (which is c, the most recent download done)
    assert meta["filtered"] is True
    assert meta["hidden_done"] >= 2

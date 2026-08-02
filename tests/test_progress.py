"""Job progress labels and ETA helpers."""

from __future__ import annotations

from pathlib import Path

from vdl.queue import Job, JobKind, JobQueue, estimate_eta_sec, format_duration


def _job(**kwargs) -> Job:
    defaults = dict(
        id=1,
        kind="download",
        priority=100,
        status="pending",
        download_id=None,
        depends_on_job_id=None,
        payload_json="{}",
        result_json=None,
        error=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        started_at=None,
        finished_at=None,
        progress_pct=0.0,
        progress_msg=None,
        progress_current=None,
        progress_total=None,
        eta_sec=None,
    )
    defaults.update(kwargs)
    return Job(**defaults)


def test_progress_label_states() -> None:
    pending = _job(status="pending")
    assert "queued" in pending.progress_label()

    running = _job(
        id=2,
        status="running",
        progress_pct=42.5,
        progress_msg="downloading",
        progress_current=4,
        progress_total=10,
        started_at="2026-01-01T00:00:00+00:00",
    )
    label = running.progress_label()
    assert "42.5%" in label
    assert "4/10" in label
    assert "downloading" in label

    done = _job(id=3, status="done", progress_pct=100)
    assert "100%" in done.progress_label()


def test_format_duration() -> None:
    assert format_duration(5) == "5s"
    assert format_duration(65) == "1m05s"
    assert format_duration(3661).startswith("1h")


def test_update_progress_persists(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "r.db")
    j = q.enqueue(JobKind.DOWNLOAD, priority=100, payload={"url": "x"})
    claimed = q.claim_next()
    assert claimed is not None
    q.update_progress(
        claimed.id,
        pct=33.0,
        message="mid",
        current=1,
        total=3,
        started_at=claimed.started_at,
    )
    got = q.get(claimed.id)
    assert got is not None
    assert got.progress_pct == 33.0
    assert got.progress_msg == "mid"
    assert got.progress_current == 1
    assert got.progress_total == 3
    assert "33.0%" in got.progress_label()


def test_estimate_eta() -> None:
    # 50% after 10s → ~10s remaining (approx)
    from datetime import datetime, timedelta, timezone

    from vdl.queue import Job

    started = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    eta = estimate_eta_sec(started, 50.0)
    assert eta is not None
    assert 5 < eta < 20

    # live recompute in progress_label ignores stale eta_sec
    j = Job(
        id=1,
        kind="download",
        priority=100,
        status="running",
        download_id=None,
        depends_on_job_id=None,
        payload_json="{}",
        result_json=None,
        error=None,
        created_at=started,
        updated_at=started,
        started_at=started,
        finished_at=None,
        progress_pct=50.0,
        progress_msg="mid",
        eta_sec=99999.0,  # stale
    )
    label = j.progress_label()
    assert "ETA" in label
    assert "99999" not in label

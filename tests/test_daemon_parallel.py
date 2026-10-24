"""Parallel job execution in the daemon pool."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from vdl.config import Config
from vdl.daemon import run_forever
from vdl.queue import JobKind, JobQueue


def test_parallel_claims_three_independent_jobs(tmp_path: Path, monkeypatch) -> None:
    """Downloads run concurrently within their two dedicated slots."""
    cfg = Config(home=tmp_path / "home")
    cfg.ensure_dirs()
    q = JobQueue(cfg.db_path)
    for i in range(3):
        q.enqueue(
            JobKind.DOWNLOAD,
            priority=100,
            payload={"url": f"http://example.test/{i}", "no_index": True},
        )

    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_run_job(config, job):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.35)
        with lock:
            active -= 1
        return {"ok": True, "id": job.id}

    monkeypatch.setattr("vdl.daemon.run_job", fake_run_job)

    run_forever(
        cfg,
        poll_interval=0.05,
        foreground=False,
        write_pid_file=False,
        idle_sec=0.3,
        workers=3,
    )

    jobs = q.list_jobs(limit=10)
    assert all(j.status == "done" for j in jobs)
    assert max_active == 2


@pytest.mark.parametrize("workers", [1, 3, 4])
@pytest.mark.parametrize("saturated_kind", [JobKind.DOWNLOAD, JobKind.EMBED])
def test_reserved_lanes_and_dependencies(tmp_path, monkeypatch, workers, saturated_kind):
    cfg = Config(home=tmp_path / "home")
    cfg.daemon_auto_reload = False
    q = JobQueue(cfg.db_path)
    download_slots = max(2, workers) - 1
    processing_started = threading.Event()
    download_started = threading.Event()
    lock = threading.Lock()
    active = {"download": 0, "processing": 0}
    maxima = active.copy()
    # Fill either lane with a backlog. Both must still make progress.
    for _ in range(4):
        q.enqueue(saturated_kind, priority=1000)
    parent = q.enqueue(JobKind.DOWNLOAD, priority=100)
    child = q.enqueue(JobKind.DEDUP, priority=5000, depends_on_job_id=parent.id)
    q.enqueue(JobKind.EMBED, priority=20)

    def fake_run_job(config, job):
        lane = "download" if job.kind == "download" else "processing"
        if job.id == child.id:
            assert q.get(parent.id).status == "done"
        with lock:
            active[lane] += 1
            maxima[lane] = max(maxima[lane], active[lane])
        if lane == "download":
            download_started.set()
            assert processing_started.wait(3), "processing starved by downloads"
        else:
            processing_started.set()
            assert download_started.wait(3), "downloads starved by processing"
        time.sleep(0.02)
        with lock:
            active[lane] -= 1
        return {}

    monkeypatch.setattr("vdl.daemon.run_job", fake_run_job)
    run_forever(cfg, workers=workers, poll_interval=0.01,
                foreground=False, idle_sec=0.02)
    assert all(j.status == "done" for j in q.list_jobs(limit=20))
    assert 1 <= maxima["download"] <= download_slots
    assert maxima["processing"] == 1

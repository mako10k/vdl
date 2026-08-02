"""Parallel job execution in the daemon pool."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from vdl.config import Config
from vdl.daemon import run_forever
from vdl.queue import JobKind, JobQueue


def test_parallel_claims_three_independent_jobs(tmp_path: Path, monkeypatch) -> None:
    """Three independent downloads should run concurrently (active <= workers)."""
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
    started = threading.Event()

    def fake_run_job(config, job):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active >= 2:
                started.set()
        time.sleep(0.35)
        with lock:
            active -= 1
        return {"ok": True, "id": job.id}

    monkeypatch.setattr("vdl.daemon.run_job", fake_run_job)

    t0 = time.time()
    run_forever(
        cfg,
        poll_interval=0.05,
        foreground=False,
        write_pid_file=False,
        idle_sec=0.3,
        workers=3,
    )
    elapsed = time.time() - t0

    jobs = q.list_jobs(limit=10)
    assert all(j.status == "done" for j in jobs)
    # sequential would be ~1.05s; parallel ~0.35s + overhead
    assert max_active >= 2, f"expected concurrency, max_active={max_active}"
    assert elapsed < 1.2

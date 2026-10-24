"""Daemon idle auto-stop and ensure_daemon_running."""

from __future__ import annotations

import time
from pathlib import Path

from vdl.config import Config
from vdl.daemon import daemon_status, ensure_daemon_running, run_forever


def test_run_forever_idle_exit(tmp_path: Path) -> None:
    cfg = Config(home=tmp_path / "home")
    cfg.ensure_dirs()
    t0 = time.time()
    run_forever(
        cfg,
        poll_interval=0.05,
        foreground=False,
        write_pid_file=False,
        idle_sec=0.2,
    )
    elapsed = time.time() - t0
    assert elapsed < 2.0
    assert elapsed >= 0.15


def test_ensure_daemon_starts_and_idle_stops(tmp_path: Path) -> None:
    cfg = Config(home=tmp_path / "home")
    cfg.daemon_idle_sec = 0.5
    cfg.ensure_dirs()
    pid, started = ensure_daemon_running(cfg, idle_sec=0.5)
    assert started
    assert pid > 0
    deadline = time.time() + 5
    while time.time() < deadline:
        if not daemon_status(cfg)["running"]:
            break
        time.sleep(0.1)
    assert not daemon_status(cfg)["running"]


def test_ensure_daemon_idempotent(tmp_path: Path) -> None:
    cfg = Config(home=tmp_path / "home")
    cfg.daemon_idle_sec = 30
    cfg.ensure_dirs()
    pid1, started1 = ensure_daemon_running(cfg, idle_sec=30)
    pid2, started2 = ensure_daemon_running(cfg, idle_sec=30)
    assert started1
    assert not started2
    assert pid1 == pid2
    # stop via SIGTERM through stop_daemon
    from vdl.daemon import stop_daemon

    stop_daemon(cfg)
    assert not daemon_status(cfg)["running"]


def test_second_daemon_cannot_reclaim_a_live_workers_jobs(tmp_path):
    import subprocess
    import sys
    cfg = Config(home=tmp_path)
    cfg.ensure_dirs()
    from vdl.daemon import read_pid
    cmd = [sys.executable, '-m', 'vdl', '--home', str(tmp_path), 'daemon', 'run', '--pid', '--idle', '10']
    first = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if read_pid(cfg.daemon_pid_path):
                break
            time.sleep(.05)
        assert read_pid(cfg.daemon_pid_path) == first.pid
        second = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        assert second.returncode != 0
        assert 'worker lock held' in second.stderr
        assert read_pid(cfg.daemon_pid_path) == first.pid
    finally:
        first.terminate()
        first.wait(timeout=5)

"""
Supervisor process: keeps the job worker daemon alive while work remains.

- Starts worker when queue has pending/running jobs and worker is down
- Restarts worker after crash if work remains
- Idle-exits after SUPERVISOR_IDLE_SEC with empty queue and no worker
- Does not steal job execution (worker still owns the pool / reload)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

from vdl.config import Config
from vdl.daemon import (
    daemon_status,
    ensure_daemon_running,
    pid_is_running,
    read_pid,
    remove_pid,
    stop_daemon,
    write_pid,
)
from vdl.queue import JobQueue


class SupervisorState:
    def __init__(self) -> None:
        self.stopping = False


def _setup_logging(log_path: Path | None, *, foreground: bool) -> logging.Logger:
    logger = logging.getLogger("vdl.supervisor")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    if foreground:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


def supervisor_status(config: Config) -> dict:
    pid = read_pid(config.supervisor_pid_path)
    running = bool(pid and pid_is_running(pid))
    worker = daemon_status(config)
    q = JobQueue(config.db_path)
    return {
        "running": running,
        "pid": pid if running else None,
        "pid_file": str(config.supervisor_pid_path),
        "log_file": str(config.supervisor_log_path),
        "idle_sec": getattr(config, "supervisor_idle_sec", 120),
        "worker": worker,
        "queue": q.counts(),
    }


def _queue_needs_worker(queue: JobQueue) -> bool:
    counts = queue.counts()
    return (counts.get("pending", 0) + counts.get("running", 0)) > 0


def run_forever(
    config: Config,
    *,
    poll_interval: float = 1.0,
    foreground: bool = True,
    write_pid_file: bool = False,
    idle_sec: float | None = None,
) -> None:
    config.ensure_dirs()
    if idle_sec is None:
        idle_sec = float(getattr(config, "supervisor_idle_sec", 120))

    logger = _setup_logging(
        config.supervisor_log_path if (not foreground or write_pid_file) else None,
        foreground=foreground,
    )
    state = SupervisorState()
    queue = JobQueue(config.db_path)

    def _handle_sig(_signum: int, _frame: object) -> None:
        logger.info("stop requested")
        state.stopping = True

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    if write_pid_file:
        write_pid(config.supervisor_pid_path, os.getpid())

    logger.info(
        "supervisor started home=%s idle_sec=%s poll=%.1fs",
        config.home,
        idle_sec if idle_sec and idle_sec > 0 else "disabled",
        poll_interval,
    )

    idle_since: float | None = None
    last_worker_pid: int | None = None
    restart_count = 0

    try:
        while not state.stopping:
            needs = _queue_needs_worker(queue)
            wstat = daemon_status(config)
            worker_up = bool(wstat["running"])
            worker_pid = wstat.get("pid")

            if worker_up and worker_pid:
                if last_worker_pid and worker_pid != last_worker_pid:
                    logger.info(
                        "worker pid changed %s → %s (reload or restart)",
                        last_worker_pid,
                        worker_pid,
                    )
                last_worker_pid = int(worker_pid)
            elif last_worker_pid and not worker_up:
                logger.warning("worker pid=%s gone", last_worker_pid)
                last_worker_pid = None

            if needs and not worker_up:
                try:
                    pid, started = ensure_daemon_running(config)
                    if started:
                        restart_count += 1
                        logger.info(
                            "started worker pid=%s (restart_count=%s) queue=%s",
                            pid,
                            restart_count,
                            queue.counts(),
                        )
                    else:
                        logger.info("worker already up pid=%s", pid)
                    last_worker_pid = pid
                    idle_since = None
                except Exception as e:
                    logger.exception("failed to start worker: %s", e)

            if not needs and not worker_up:
                now = time.time()
                if idle_sec and idle_sec > 0:
                    if idle_since is None:
                        idle_since = now
                        logger.info(
                            "queue empty and worker down; idle timer (%.0fs)",
                            idle_sec,
                        )
                    elif (now - idle_since) >= idle_sec:
                        logger.info(
                            "supervisor idle timeout (%.0fs); shutting down",
                            idle_sec,
                        )
                        break
            else:
                idle_since = None

            time.sleep(max(0.2, poll_interval))
    finally:
        if write_pid_file:
            remove_pid(config.supervisor_pid_path)
        logger.info("supervisor stopped restarts=%s", restart_count)


def start_background(config: Config, *, idle_sec: float | None = None) -> int:
    import subprocess

    st = supervisor_status(config)
    if st["running"]:
        raise RuntimeError(f"supervisor already running pid={st['pid']}")

    config.ensure_dirs()
    if idle_sec is None:
        idle_sec = float(getattr(config, "supervisor_idle_sec", 120))

    cmd = [
        sys.executable,
        "-m",
        "vdl",
        "--home",
        str(config.home),
        "supervisor",
        "run",
        "--pid",
        "--idle",
        str(idle_sec),
    ]
    logf = open(config.supervisor_log_path, "a", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=logf,
        start_new_session=True,
        close_fds=True,
    )
    for _ in range(50):
        time.sleep(0.1)
        pid = read_pid(config.supervisor_pid_path)
        if pid and pid_is_running(pid):
            return pid
    return proc.pid


def ensure_supervisor_running(
    config: Config, *, idle_sec: float | None = None
) -> tuple[int, bool]:
    """
    Ensure supervisor is running (preferred entry for CLI auto-start).
    Returns (pid, started_now).
    """
    st = supervisor_status(config)
    if st["running"] and st["pid"] is not None:
        return int(st["pid"]), False
    if st["pid"] and not st["running"]:
        remove_pid(config.supervisor_pid_path)
    try:
        pid = start_background(config, idle_sec=idle_sec)
        return pid, True
    except RuntimeError:
        st2 = supervisor_status(config)
        if st2["running"] and st2["pid"] is not None:
            return int(st2["pid"]), False
        raise


def stop_supervisor(
    config: Config, *, timeout: float = 30.0, stop_worker: bool = False
) -> bool:
    """Stop supervisor; optionally also stop the worker daemon."""
    if stop_worker:
        try:
            stop_daemon(config, timeout=timeout)
        except Exception:
            pass
    pid = read_pid(config.supervisor_pid_path)
    if not pid or not pid_is_running(pid):
        remove_pid(config.supervisor_pid_path)
        return False
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_is_running(pid):
            remove_pid(config.supervisor_pid_path)
            return True
        time.sleep(0.2)
    os.kill(pid, signal.SIGKILL)
    remove_pid(config.supervisor_pid_path)
    return True


def ensure_runtime(config: Config) -> tuple[str, int, bool]:
    """
    Preferred auto-start for CLI:
    - If supervisor enabled: start supervisor (which starts worker as needed)
    - Else: start worker daemon directly
    Returns (role, pid, started_now) where role is 'supervisor' or 'worker'.
    """
    if getattr(config, "supervisor_enabled", True):
        pid, started = ensure_supervisor_running(config)
        return "supervisor", pid, started
    from vdl.daemon import ensure_daemon_running

    pid, started = ensure_daemon_running(config)
    return "worker", pid, started

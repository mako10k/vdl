"""Background worker: download-first queue, auto-start, idle auto-stop."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

from vdl.config import DAEMON_IDLE_SEC, Config
from vdl.queue import JobQueue
from vdl.workers import run_job


class DaemonState:
    def __init__(self) -> None:
        self.stopping = False


def _setup_logging(log_path: Path | None, *, foreground: bool) -> logging.Logger:
    logger = logging.getLogger("vdl.daemon")
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


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text().strip())
    except ValueError:
        return None


def write_pid(pid_path: Path, pid: int) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid))


def remove_pid(pid_path: Path) -> None:
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass


def daemon_status(config: Config) -> dict:
    pid = read_pid(config.daemon_pid_path)
    running = bool(pid and pid_is_running(pid))
    q = JobQueue(config.db_path)
    return {
        "running": running,
        "pid": pid if running else None,
        "pid_file": str(config.daemon_pid_path),
        "log_file": str(config.daemon_log_path),
        "queue": q.counts(),
        "idle_sec": getattr(config, "daemon_idle_sec", DAEMON_IDLE_SEC),
    }


def _queue_is_idle(queue: JobQueue) -> bool:
    """True when nothing is pending or running (blocked deps still count as pending)."""
    counts = queue.counts()
    return counts.get("pending", 0) == 0 and counts.get("running", 0) == 0


def run_forever(
    config: Config,
    *,
    poll_interval: float = 1.0,
    foreground: bool = True,
    write_pid_file: bool = False,
    idle_sec: float | None = None,
) -> None:
    """
    Worker loop.
    If idle_sec > 0 and the queue stays empty (no pending/running) for that long,
    exit gracefully. Default comes from config / VDL_DAEMON_IDLE_SEC.
    idle_sec=0 or None with explicit 0 means never auto-stop.
    """
    config.ensure_dirs()
    if idle_sec is None:
        idle_sec = float(getattr(config, "daemon_idle_sec", DAEMON_IDLE_SEC))
    # Background daemons always use idle stop unless explicitly 0.
    # Foreground without --pid defaults to no idle stop unless --idle is set by CLI.
    logger = _setup_logging(
        config.daemon_log_path if (not foreground or write_pid_file) else None,
        foreground=foreground,
    )
    state = DaemonState()
    queue = JobQueue(config.db_path)

    def _handle_sig(_signum: int, _frame: object) -> None:
        logger.info("stop requested")
        state.stopping = True

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    if write_pid_file:
        write_pid(config.daemon_pid_path, os.getpid())

    logger.info(
        "daemon started home=%s idle_sec=%s",
        config.home,
        idle_sec if idle_sec and idle_sec > 0 else "disabled",
    )
    idle_since: float | None = None
    try:
        while not state.stopping:
            job = queue.claim_next()
            if job:
                idle_since = None
                logger.info(
                    "job start id=%s kind=%s priority=%s download_id=%s",
                    job.id,
                    job.kind,
                    job.priority,
                    job.download_id,
                )
                try:
                    result = run_job(config, job)
                    queue.mark_done(job.id, result)
                    logger.info("job done id=%s kind=%s", job.id, job.kind)
                except Exception as e:
                    queue.mark_failed(job.id, str(e))
                    logger.exception("job failed id=%s: %s", job.id, e)
                continue

            # No claimable job right now
            now = time.time()
            if idle_sec and idle_sec > 0 and _queue_is_idle(queue):
                if idle_since is None:
                    idle_since = now
                    logger.info("queue empty; idle timer started (%.0fs)", idle_sec)
                elif (now - idle_since) >= idle_sec:
                    logger.info(
                        "idle timeout reached (%.0fs); shutting down", idle_sec
                    )
                    break
            else:
                # Work still pending (e.g. waiting on dependency) — don't idle-exit
                idle_since = None

            time.sleep(poll_interval)
    finally:
        if write_pid_file:
            remove_pid(config.daemon_pid_path)
        logger.info("daemon stopped")


def start_background(
    config: Config,
    *,
    idle_sec: float | None = None,
) -> int:
    """Spawn `vdl daemon run --pid` in background; return child pid."""
    import subprocess

    st = daemon_status(config)
    if st["running"]:
        raise RuntimeError(f"daemon already running pid={st['pid']}")

    config.ensure_dirs()
    if idle_sec is None:
        idle_sec = float(getattr(config, "daemon_idle_sec", DAEMON_IDLE_SEC))

    cmd = [
        sys.executable,
        "-m",
        "vdl",
        "--home",
        str(config.home),
        "daemon",
        "run",
        "--pid",
        "--idle",
        str(idle_sec),
    ]
    logf = open(config.daemon_log_path, "a", encoding="utf-8")
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
        pid = read_pid(config.daemon_pid_path)
        if pid and pid_is_running(pid):
            return pid
    return proc.pid


def ensure_daemon_running(
    config: Config,
    *,
    idle_sec: float | None = None,
) -> tuple[int, bool]:
    """
    Ensure a background daemon is running.
    Returns (pid, started_now).
    """
    st = daemon_status(config)
    if st["running"] and st["pid"] is not None:
        return int(st["pid"]), False
    # Stale pid file
    if st["pid"] and not st["running"]:
        remove_pid(config.daemon_pid_path)
    try:
        pid = start_background(config, idle_sec=idle_sec)
        return pid, True
    except RuntimeError:
        # Race: another process started it
        st2 = daemon_status(config)
        if st2["running"] and st2["pid"] is not None:
            return int(st2["pid"]), False
        raise


def stop_daemon(config: Config, *, timeout: float = 30.0) -> bool:
    pid = read_pid(config.daemon_pid_path)
    if not pid or not pid_is_running(pid):
        remove_pid(config.daemon_pid_path)
        return False
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_is_running(pid):
            remove_pid(config.daemon_pid_path)
            return True
        time.sleep(0.2)
    os.kill(pid, signal.SIGKILL)
    remove_pid(config.daemon_pid_path)
    return True

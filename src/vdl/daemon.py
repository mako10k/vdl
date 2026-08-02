"""Background worker: always prefer download jobs over indexing."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

from vdl.config import Config
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
    }


def run_forever(
    config: Config,
    *,
    poll_interval: float = 1.0,
    foreground: bool = True,
    write_pid_file: bool = False,
) -> None:
    config.ensure_dirs()
    logger = _setup_logging(
        config.daemon_log_path if not foreground or write_pid_file else None,
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

    logger.info("daemon started home=%s", config.home)
    try:
        while not state.stopping:
            job = queue.claim_next()
            if not job:
                time.sleep(poll_interval)
                continue
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
    finally:
        if write_pid_file:
            remove_pid(config.daemon_pid_path)
        logger.info("daemon stopped")


def start_background(config: Config) -> int:
    """Spawn `vdl daemon run --pid` in background; return child pid."""
    import subprocess

    st = daemon_status(config)
    if st["running"]:
        raise RuntimeError(f"daemon already running pid={st['pid']}")

    config.ensure_dirs()
    # Use same interpreter module entry
    cmd = [
        sys.executable,
        "-m",
        "vdl",
        "--home",
        str(config.home),
        "daemon",
        "run",
        "--pid",
    ]
    # detach
    logf = open(config.daemon_log_path, "a", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=logf,
        start_new_session=True,
        close_fds=True,
    )
    # wait briefly for pid file
    for _ in range(50):
        time.sleep(0.1)
        pid = read_pid(config.daemon_pid_path)
        if pid and pid_is_running(pid):
            return pid
    return proc.pid


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

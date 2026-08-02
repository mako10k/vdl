"""Background worker: download-first queue, parallel jobs, auto-start, idle stop."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from vdl.config import DAEMON_IDLE_SEC, DAEMON_WORKERS, Config
from vdl.queue import Job, JobQueue
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
        "workers": getattr(config, "daemon_workers", DAEMON_WORKERS),
    }


def _queue_is_idle(queue: JobQueue) -> bool:
    """True when nothing is pending or running (blocked deps still count as pending)."""
    counts = queue.counts()
    return counts.get("pending", 0) == 0 and counts.get("running", 0) == 0


def _execute_job(config: Config, job: Job) -> tuple[int, str, str | None]:
    """
    Run one job in a worker thread. Each thread uses its own JobQueue connection.
    Returns (job_id, status, error_or_none).
    """
    queue = JobQueue(config.db_path)
    try:
        result = run_job(config, job)
        queue.mark_done(job.id, result)
        return job.id, "done", None
    except Exception as e:
        queue.mark_failed(job.id, str(e))
        return job.id, "failed", str(e)


def run_forever(
    config: Config,
    *,
    poll_interval: float = 1.0,
    foreground: bool = True,
    write_pid_file: bool = False,
    idle_sec: float | None = None,
    workers: int | None = None,
) -> None:
    """
    Worker loop with a thread pool.
    Claims up to `workers` runnable jobs in parallel (default 3).
    Priority ordering is preserved by claim_next (highest priority first).
    """
    config.ensure_dirs()
    if idle_sec is None:
        idle_sec = float(getattr(config, "daemon_idle_sec", DAEMON_IDLE_SEC))
    max_workers = int(workers if workers is not None else getattr(config, "daemon_workers", DAEMON_WORKERS))
    max_workers = max(1, max_workers)

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
        "daemon started home=%s workers=%s idle_sec=%s",
        config.home,
        max_workers,
        idle_sec if idle_sec and idle_sec > 0 else "disabled",
    )

    idle_since: float | None = None
    futures: dict[Future[Any], Job] = {}

    def _drain_done() -> None:
        nonlocal idle_since
        finished = [f for f in list(futures) if f.done()]
        for fut in finished:
            job = futures.pop(fut)
            try:
                jid, status, err = fut.result()
                if status == "done":
                    logger.info("job done id=%s kind=%s", jid, job.kind)
                else:
                    logger.error("job failed id=%s kind=%s: %s", jid, job.kind, err)
            except Exception as e:
                logger.exception("job future error id=%s: %s", job.id, e)
                try:
                    JobQueue(config.db_path).mark_failed(job.id, str(e))
                except Exception:
                    pass

    try:
        with ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="vdl-job"
        ) as pool:
            while not state.stopping:
                _drain_done()

                # Fill free slots with highest-priority claimable jobs
                claimed_any = False
                while len(futures) < max_workers and not state.stopping:
                    job = queue.claim_next()
                    if not job:
                        break
                    claimed_any = True
                    idle_since = None
                    logger.info(
                        "job start id=%s kind=%s priority=%s download_id=%s "
                        "(active=%s/%s)",
                        job.id,
                        job.kind,
                        job.priority,
                        job.download_id,
                        len(futures) + 1,
                        max_workers,
                    )
                    fut = pool.submit(_execute_job, config, job)
                    futures[fut] = job

                if futures:
                    # Wait for at least one job to finish (or poll timeout)
                    wait(
                        list(futures.keys()),
                        timeout=poll_interval,
                        return_when=FIRST_COMPLETED,
                    )
                    continue

                # No active work and nothing claimable
                if claimed_any:
                    continue

                now = time.time()
                if idle_sec and idle_sec > 0 and _queue_is_idle(queue):
                    if idle_since is None:
                        idle_since = now
                        logger.info(
                            "queue empty; idle timer started (%.0fs)", idle_sec
                        )
                    elif (now - idle_since) >= idle_sec:
                        logger.info(
                            "idle timeout reached (%.0fs); shutting down", idle_sec
                        )
                        break
                else:
                    idle_since = None

                time.sleep(poll_interval)

            # Graceful: finish in-flight jobs
            if futures:
                logger.info(
                    "waiting for %s in-flight job(s) before stop", len(futures)
                )
                wait(list(futures.keys()), timeout=3600)
                _drain_done()
    finally:
        if write_pid_file:
            remove_pid(config.daemon_pid_path)
        logger.info("daemon stopped")


def start_background(
    config: Config,
    *,
    idle_sec: float | None = None,
    workers: int | None = None,
) -> int:
    """Spawn `vdl daemon run --pid` in background; return child pid."""
    import subprocess

    st = daemon_status(config)
    if st["running"]:
        raise RuntimeError(f"daemon already running pid={st['pid']}")

    config.ensure_dirs()
    if idle_sec is None:
        idle_sec = float(getattr(config, "daemon_idle_sec", DAEMON_IDLE_SEC))
    if workers is None:
        workers = int(getattr(config, "daemon_workers", DAEMON_WORKERS))

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
        "--workers",
        str(max(1, workers)),
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
    workers: int | None = None,
) -> tuple[int, bool]:
    """
    Ensure a background daemon is running.
    Returns (pid, started_now).
    """
    st = daemon_status(config)
    if st["running"] and st["pid"] is not None:
        return int(st["pid"]), False
    if st["pid"] and not st["running"]:
        remove_pid(config.daemon_pid_path)
    try:
        pid = start_background(config, idle_sec=idle_sec, workers=workers)
        return pid, True
    except RuntimeError:
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

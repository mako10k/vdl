"""Background worker: download-first queue, parallel jobs, auto-start, idle stop, self-reload."""

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
from vdl.queue import Job, JobKind, JobQueue
from vdl.reload import ensure_reload_if_stale, package_fingerprint, reexec_daemon
from vdl.workers import DaemonReloadRequested, run_job
from vdl.execution import run_isolated, recover_attempts


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
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists() and stat.read_text().split(") ", 1)[1].split()[0] == "Z":
            return False
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
    reload = q.find_active_reload()
    return {
        "running": running,
        "pid": pid if running else None,
        "pid_file": str(config.daemon_pid_path),
        "log_file": str(config.daemon_log_path),
        "queue": q.counts(),
        "idle_sec": getattr(config, "daemon_idle_sec", DAEMON_IDLE_SEC),
        "workers": getattr(config, "daemon_workers", DAEMON_WORKERS),
        "auto_reload": getattr(config, "daemon_auto_reload", True),
        "reload_job_id": reload.id if reload else None,
        "code_fingerprint": package_fingerprint(),
    }


def _queue_is_idle(queue: JobQueue) -> bool:
    """True when nothing is pending or running (blocked deps still count as pending)."""
    counts = queue.counts()
    return counts.get("pending", 0) == 0 and counts.get("running", 0) == 0


def _execute_job(config: Config, job: Job) -> tuple[int, str, str | None, dict | None]:
    """
    Run one job in a worker thread.
    Returns (job_id, status, error_or_none, result_or_none).
    status is done | failed | retry | reload.
    """
    queue = JobQueue(config.db_path)
    max_attempts = int(getattr(config, "job_max_attempts", 3) or 3)
    try:
        result = run_isolated(config, job)
        completed = queue.mark_done(job.id, result)
        return job.id, completed.status, None, result
    except DaemonReloadRequested as e:
        # Should only happen on main thread; treat as special.
        return job.id, "reload", None, e.result
    except Exception as e:
        updated = queue.mark_failed(
            job.id,
            str(e),
            max_attempts=max_attempts,
            cancel_dependents=True,
        )
        if updated.status == "pending":
            return job.id, "retry", str(e), None
        return job.id, "failed", str(e), None


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
    On package changes, enqueue a reload gate and drain bounded attempts
    before re-exec.
    """
    config.ensure_dirs()
    if idle_sec is None:
        idle_sec = float(getattr(config, "daemon_idle_sec", DAEMON_IDLE_SEC))
    max_workers = int(
        workers if workers is not None else getattr(config, "daemon_workers", DAEMON_WORKERS)
    )
    max_workers = max(2, max_workers)
    download_workers = max_workers - 1
    processing_kinds = {
        kind.value for kind in JobKind
        if kind not in {JobKind.DOWNLOAD, JobKind.DAEMON_RELOAD}
    }

    logger = _setup_logging(
        config.daemon_log_path if (not foreground or write_pid_file) else None,
        foreground=foreground,
    )
    state = DaemonState()
    queue = JobQueue(config.db_path)

    for job_id in recover_attempts(config):
        job = queue.get(job_id)
        if job and job.status == 'running':
            queue.mark_failed(job_id, 'attempt interrupted by daemon restart',
                              max_attempts=config.job_max_attempts)

    # Recover jobs left "running" by previous process death / re-exec.
    reclaimed = queue.reclaim_stale_running()
    if reclaimed:
        logger.info("reclaimed %s stale running job(s)", reclaimed)

    started_fp = package_fingerprint()

    def _handle_sig(_signum: int, _frame: object) -> None:
        logger.info("stop requested")
        state.stopping = True

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    if write_pid_file:
        write_pid(config.daemon_pid_path, os.getpid())

    logger.info(
        "daemon started home=%s workers=%s idle_sec=%s fingerprint=%s auto_reload=%s",
        config.home,
        max_workers,
        idle_sec if idle_sec and idle_sec > 0 else "disabled",
        started_fp,
        getattr(config, "daemon_auto_reload", True),
    )

    idle_since: float | None = None
    futures: dict[Future[Any], Job] = {}

    def _drain_done() -> None:
        finished = [f for f in list(futures) if f.done()]
        for fut in finished:
            job = futures.pop(fut)
            try:
                jid, status, err, _result = fut.result()
                if status == "done":
                    logger.info("job done id=%s kind=%s", jid, job.kind)
                elif status == "retry":
                    logger.warning(
                        "job auto-retry id=%s kind=%s: %s", jid, job.kind, err
                    )
                elif status == "reload":
                    logger.warning(
                        "reload raised from worker unexpectedly id=%s", jid
                    )
                else:
                    logger.error(
                        "job failed (final) id=%s kind=%s: %s", jid, job.kind, err
                    )
            except Exception as e:
                logger.exception("job future error id=%s: %s", job.id, e)
                try:
                    JobQueue(config.db_path).mark_failed(job.id, str(e))
                except Exception:
                    pass

    def _maybe_schedule_reload() -> None:
        """Enqueue a gate while busy, then drain active work before re-exec."""
        ensure_reload_if_stale(
            config, queue, started_fingerprint=started_fp, logger=logger
        )

    def _perform_reload(job: Job) -> None:
        logger.info("executing daemon_reload id=%s — re-exec", job.id)
        queue.update_progress(job.id, pct=50.0, message="re-exec")
        try:
            result = run_job(config, job)
            # run_job raises DaemonReloadRequested
            queue.mark_done(job.id, result)
        except DaemonReloadRequested as e:
            queue.mark_done(job.id, e.result)
            logger.info(
                "daemon_reload id=%s done; re-exec workers=%s idle=%s",
                job.id,
                max_workers,
                idle_sec,
            )
            # PID file removed so child can write; child will re-write.
            if write_pid_file:
                remove_pid(config.daemon_pid_path)
            reexec_daemon(config, idle_sec=float(idle_sec or 0), workers=max_workers)
        # unreachable if exec succeeds

    try:
        with ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="vdl-job"
        ) as pool:
            last_eta_refresh = 0.0
            last_housekeep = 0.0
            while not state.stopping:
                _drain_done()

                # Refresh ETA for running jobs every ~2s (for list/watch readers)
                now_m = time.time()
                if now_m - last_eta_refresh >= 2.0:
                    try:
                        queue.refresh_running_etas()
                    except Exception:
                        pass
                    last_eta_refresh = now_m

                # Housekeep every ~30s: retry recoverable failures, archive old terminals
                if now_m - last_housekeep >= 30.0:
                    try:
                        hk = queue.housekeep(
                            archive_after_minutes=float(
                                getattr(config, "job_archive_minutes", 60) or 60
                            ),
                            retry_after_minutes=float(
                                getattr(config, "job_housekeep_retry_after_min", 5)
                                or 5
                            ),
                            auto_retry=bool(
                                getattr(config, "job_housekeep_auto_retry", True)
                            ),
                            max_attempts=int(
                                getattr(config, "job_max_attempts", 3) or 3
                            ),
                        )
                        if (
                            hk.get("retried")
                            or hk.get("archived")
                            or hk.get("orphans_cancelled")
                        ):
                            logger.info(
                                "housekeep retried=%s archived=%s orphans=%s",
                                hk.get("retried"),
                                hk.get("archived"),
                                hk.get("orphans_cancelled"),
                            )
                    except Exception:
                        logger.exception("housekeep failed")
                    last_housekeep = now_m

                # Job boundary: consider self-update gate
                _maybe_schedule_reload()

                # Fill free slots — but if reload is active, only claim that
                # (and only when no other work is in-flight).
                reload_gate = queue.find_active_reload()
                if reload_gate and futures:
                    # Wait for in-flight user jobs to finish before reload.
                    wait(
                        list(futures.keys()),
                        timeout=poll_interval,
                        return_when=FIRST_COMPLETED,
                    )
                    continue

                claimed_any = False

                while len(futures) < max_workers and not state.stopping:
                    # When reload pending, claim only after idle workers
                    if reload_gate and futures:
                        break
                    downloads_active = sum(
                        j.kind == JobKind.DOWNLOAD.value for j in futures.values()
                    )
                    job = None
                    if downloads_active < download_workers:
                        job = queue.claim_next(kinds={JobKind.DOWNLOAD.value}, allow_reload=not futures)
                    if job is None:
                        job = queue.claim_next(kinds=processing_kinds, allow_reload=not futures)
                    if not job:
                        break
                    claimed_any = True
                    idle_since = None

                    if job.kind == JobKind.DAEMON_RELOAD.value:
                        # Main-thread only; drops parallelism to 0 for the gate.
                        logger.info(
                            "job start id=%s kind=daemon_reload (self-update gate)",
                            job.id,
                        )
                        _perform_reload(job)
                        # if reexec failed, continue loop
                        break

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

                    # After claiming one, re-check reload gate (should not claim
                    # more if a reload was just injected — only inject at boundary)
                    reload_gate = queue.find_active_reload()
                    if reload_gate:
                        break

                if futures:
                    wait(
                        list(futures.keys()),
                        timeout=poll_interval,
                        return_when=FIRST_COMPLETED,
                    )
                    continue

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
        str(max(2, workers)),
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

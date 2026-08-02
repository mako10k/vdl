"""Daemon self-update: code fingerprint + re-exec at job boundaries."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from vdl import __version__
from vdl.config import PRIORITY_DAEMON_RELOAD, Config
from vdl.queue import JobKind, JobQueue


def package_fingerprint() -> str:
    """
    Fingerprint of installed vdl package code.
    Changes when .py files under the package are newer (or version changes).
    """
    import vdl

    root = Path(vdl.__file__).resolve().parent
    max_mtime = 0.0
    try:
        for p in root.rglob("*.py"):
            try:
                max_mtime = max(max_mtime, p.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    return f"{__version__}:{int(max_mtime)}"


def ensure_reload_if_stale(
    config: Config,
    queue: JobQueue,
    *,
    started_fingerprint: str,
    logger=None,
) -> bool:
    """
    If package code changed since daemon start, enqueue a daemon_reload gate
    that all other pending work implicitly depends on.
    Returns True if a reload gate is now active.
    """
    if not getattr(config, "daemon_auto_reload", True):
        return queue.find_active_reload() is not None

    current = package_fingerprint()
    if current == started_fingerprint:
        return queue.find_active_reload() is not None

    job = queue.ensure_daemon_reload_gate(
        reason="package fingerprint changed",
        fingerprint=current,
        priority=PRIORITY_DAEMON_RELOAD,
    )
    if logger:
        logger.info(
            "daemon_reload gate id=%s (was %s → now %s)",
            job.id,
            started_fingerprint,
            current,
        )
    return True


def reexec_daemon(config: Config, *, idle_sec: float, workers: int) -> None:
    """Replace current process with a fresh daemon (loads new code)."""
    # Prefer the same interpreter + module entry used to launch us.
    argv = [
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
    # Ensure cwd-independent; env inherits (VDL_* etc.)
    os.execv(sys.executable, argv)

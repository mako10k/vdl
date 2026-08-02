"""Paths and runtime configuration."""

from __future__ import annotations

import os
from pathlib import Path

# Default root: ~/.vdl  (override with VDL_HOME)
DEFAULT_HOME = Path(os.environ.get("VDL_HOME", Path.home() / ".vdl"))

DOWNLOAD_DIR_NAME = "downloads"
DB_NAME = "registry.db"
TEMP_DIR_NAME = "tmp"
CHROMA_DIR_NAME = "chroma"
KEYFRAMES_DIR_NAME = "keyframes"
DAEMON_PID_NAME = "daemon.pid"
DAEMON_LOG_NAME = "daemon.log"
SUPERVISOR_PID_NAME = "supervisor.pid"
SUPERVISOR_LOG_NAME = "supervisor.log"

# Job priorities (higher first)
PRIORITY_DOWNLOAD = 100
PRIORITY_SCENE = 40
PRIORITY_CLIP = 30
PRIORITY_EMBED = 20
# Higher than any user job — claimed first at job boundaries.
PRIORITY_DAEMON_RELOAD = 10_000

# Models / thresholds (env override)
SCENE_THRESHOLD = float(os.environ.get("VDL_SCENE_THRESHOLD", "0.3"))
CLIP_MODEL = os.environ.get("VDL_CLIP_MODEL", "ViT-B-32")
CLIP_PRETRAINED = os.environ.get("VDL_CLIP_PRETRAINED", "openai")
OLLAMA_BASE = os.environ.get("VDL_OLLAMA_BASE", "http://127.0.0.1:11434")
OLLAMA_EMBED_MODEL = os.environ.get("VDL_OLLAMA_EMBED_MODEL", "nomic-embed-text")
FRAMES_PER_SCENE = int(os.environ.get("VDL_FRAMES_PER_SCENE", "5"))
# Background daemon exits after this many idle seconds with empty queue (0 = never).
DAEMON_IDLE_SEC = float(os.environ.get("VDL_DAEMON_IDLE_SEC", "60"))
# Max concurrent jobs in the daemon worker pool.
DAEMON_WORKERS = int(os.environ.get("VDL_DAEMON_WORKERS", "3"))
# Auto-enqueue daemon_reload when package code mtime changes (job-boundary only).
DAEMON_AUTO_RELOAD = os.environ.get("VDL_DAEMON_AUTO_RELOAD", "1").strip() not in {
    "0",
    "false",
    "no",
    "off",
}
# Supervisor exits after this many seconds with empty queue + no worker (0 = never).
SUPERVISOR_IDLE_SEC = float(os.environ.get("VDL_SUPERVISOR_IDLE_SEC", "120"))
# Use supervisor to keep the worker alive (default on).
SUPERVISOR_ENABLED = os.environ.get("VDL_SUPERVISOR", "1").strip() not in {
    "0",
    "false",
    "no",
    "off",
}
# Queue list: show done jobs only if recent (minutes) or latest of each kind.
LIST_DONE_MINUTES = float(os.environ.get("VDL_LIST_DONE_MINUTES", "10"))
LIST_LIMIT = int(os.environ.get("VDL_LIST_LIMIT", "30"))


class Config:
    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home) if home else DEFAULT_HOME
        self.download_dir = self.home / DOWNLOAD_DIR_NAME
        self.db_path = self.home / DB_NAME
        self.temp_dir = self.home / TEMP_DIR_NAME
        self.chroma_dir = self.home / CHROMA_DIR_NAME
        self.keyframes_dir = self.home / KEYFRAMES_DIR_NAME
        self.daemon_pid_path = self.home / DAEMON_PID_NAME
        self.daemon_log_path = self.home / DAEMON_LOG_NAME
        self.supervisor_pid_path = self.home / SUPERVISOR_PID_NAME
        self.supervisor_log_path = self.home / SUPERVISOR_LOG_NAME
        self.daemon_idle_sec = DAEMON_IDLE_SEC
        self.daemon_workers = max(1, DAEMON_WORKERS)
        self.daemon_auto_reload = DAEMON_AUTO_RELOAD
        self.supervisor_idle_sec = SUPERVISOR_IDLE_SEC
        self.supervisor_enabled = SUPERVISOR_ENABLED
        self.list_done_minutes = LIST_DONE_MINUTES
        self.list_limit = max(1, LIST_LIMIT)

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.chroma_dir.mkdir(parents=True, exist_ok=True)
        self.keyframes_dir.mkdir(parents=True, exist_ok=True)

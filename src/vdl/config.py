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
# After download, before index: remux leftover MPEG-TS, then collapse dupes.
PRIORITY_TS_TO_MP4 = 50
PRIORITY_DEDUP = 45
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
# Chat/generate model for query translation (CLIP is English-centric).
# Prefer an installed chat model; override with VDL_OLLAMA_CHAT_MODEL.
OLLAMA_CHAT_MODEL = os.environ.get("VDL_OLLAMA_CHAT_MODEL", "gemma4")
# Auto-translate non-English queries before CLIP text search (1=on).
SEARCH_TRANSLATE = os.environ.get("VDL_SEARCH_TRANSLATE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
# Diversity: max hits per URL host in aggregated results (0 = no cap).
SEARCH_MAX_PER_DOMAIN = max(0, int(os.environ.get("VDL_SEARCH_MAX_PER_DOMAIN", "2")))
FRAMES_PER_SCENE = int(os.environ.get("VDL_FRAMES_PER_SCENE", "5"))
# Background daemon exits after this many idle seconds with empty queue (0 = never).
DAEMON_IDLE_SEC = float(os.environ.get("VDL_DAEMON_IDLE_SEC", "60"))
# Total worker slots: one processing slot, remainder download-only (minimum 2).
DAEMON_WORKERS = int(os.environ.get("VDL_DAEMON_WORKERS", "4"))
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
# Max attempts per job (1 try + retries). Default 3 = up to 2 automatic retries.
JOB_MAX_ATTEMPTS = max(1, int(os.environ.get("VDL_JOB_MAX_ATTEMPTS", "3")))
# Terminal jobs (done/failed/cancelled) older than this are archived (minutes).
JOB_ARCHIVE_MINUTES = float(os.environ.get("VDL_JOB_ARCHIVE_MINUTES", "60"))
# How long failed/cancelled stay visible in the default list (minutes).
LIST_FAILED_MINUTES = float(os.environ.get("VDL_LIST_FAILED_MINUTES", "30"))
LIST_CANCELLED_MINUTES = float(os.environ.get("VDL_LIST_CANCELLED_MINUTES", "10"))
# Cool-down before housekeep auto-retries a failed job (minutes).
JOB_HOUSEKEEP_RETRY_AFTER_MIN = float(
    os.environ.get("VDL_JOB_HOUSEKEEP_RETRY_AFTER_MIN", "5")
)
# Auto-retry retryable failures once before archive (1=on).
JOB_HOUSEKEEP_AUTO_RETRY = os.environ.get(
    "VDL_JOB_HOUSEKEEP_AUTO_RETRY", "1"
).strip().lower() not in {"0", "false", "no", "off"}


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
        self.daemon_workers = max(2, DAEMON_WORKERS)
        self.daemon_auto_reload = DAEMON_AUTO_RELOAD
        self.supervisor_idle_sec = SUPERVISOR_IDLE_SEC
        self.supervisor_enabled = SUPERVISOR_ENABLED
        self.list_done_minutes = LIST_DONE_MINUTES
        self.list_limit = max(1, LIST_LIMIT)
        self.list_failed_minutes = float(
            os.environ.get("VDL_LIST_FAILED_MINUTES", str(LIST_FAILED_MINUTES))
        )
        self.list_cancelled_minutes = float(
            os.environ.get("VDL_LIST_CANCELLED_MINUTES", str(LIST_CANCELLED_MINUTES))
        )
        self.job_max_attempts = max(
            1, int(os.environ.get("VDL_JOB_MAX_ATTEMPTS", str(JOB_MAX_ATTEMPTS)))
        )
        self.job_archive_minutes = float(
            os.environ.get("VDL_JOB_ARCHIVE_MINUTES", str(JOB_ARCHIVE_MINUTES))
        )
        self.job_housekeep_retry_after_min = float(
            os.environ.get(
                "VDL_JOB_HOUSEKEEP_RETRY_AFTER_MIN", str(JOB_HOUSEKEEP_RETRY_AFTER_MIN)
            )
        )
        self.job_housekeep_auto_retry = os.environ.get(
            "VDL_JOB_HOUSEKEEP_AUTO_RETRY",
            "1" if JOB_HOUSEKEEP_AUTO_RETRY else "0",
        ).strip().lower() not in {"0", "false", "no", "off"}

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.chroma_dir.mkdir(parents=True, exist_ok=True)
        self.keyframes_dir.mkdir(parents=True, exist_ok=True)

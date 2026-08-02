"""Paths and runtime configuration."""

from __future__ import annotations

import os
from pathlib import Path

# Default root: ~/.vdl  (override with VDL_HOME)
DEFAULT_HOME = Path(os.environ.get("VDL_HOME", Path.home() / ".vdl"))

DOWNLOAD_DIR_NAME = "downloads"
DB_NAME = "registry.db"
TEMP_DIR_NAME = "tmp"


class Config:
    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home) if home else DEFAULT_HOME
        self.download_dir = self.home / DOWNLOAD_DIR_NAME
        self.db_path = self.home / DB_NAME
        self.temp_dir = self.home / TEMP_DIR_NAME

    def ensure_dirs(self) -> None:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.home.mkdir(parents=True, exist_ok=True)

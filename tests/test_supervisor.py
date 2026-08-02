"""Supervisor keeps worker alive when queue has work."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from vdl.config import Config
from vdl.supervisor import ensure_runtime, supervisor_status


def test_ensure_runtime_uses_supervisor(tmp_path: Path) -> None:
    cfg = Config(home=tmp_path / "home")
    cfg.ensure_dirs()
    cfg.supervisor_enabled = True
    with patch("vdl.supervisor.ensure_supervisor_running", return_value=(99, True)) as m:
        role, pid, started = ensure_runtime(cfg)
        assert role == "supervisor"
        assert pid == 99
        assert started
        m.assert_called_once()


def test_ensure_runtime_direct_worker(tmp_path: Path) -> None:
    cfg = Config(home=tmp_path / "home")
    cfg.ensure_dirs()
    cfg.supervisor_enabled = False
    with patch("vdl.daemon.ensure_daemon_running", return_value=(77, True)):
        role, pid, started = ensure_runtime(cfg)
        assert role == "worker"
        assert pid == 77


def test_supervisor_status_shape(tmp_path: Path) -> None:
    cfg = Config(home=tmp_path / "home")
    cfg.ensure_dirs()
    st = supervisor_status(cfg)
    assert st["running"] is False
    assert "queue" in st
    assert "worker" in st

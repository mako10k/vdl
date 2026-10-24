"""daemon_reload gate: implicit dependency + claim blocking."""

from __future__ import annotations

from pathlib import Path

from vdl.config import PRIORITY_DAEMON_RELOAD, PRIORITY_DOWNLOAD, Config
from vdl.queue import JobKind, JobQueue
from vdl.reload import ensure_reload_if_stale, package_fingerprint


def test_ensure_gate_attaches_root_jobs(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "r.db")
    a = q.enqueue(JobKind.DOWNLOAD, priority=PRIORITY_DOWNLOAD, payload={"url": "a"})
    b = q.enqueue(
        JobKind.SCENE_DETECT,
        priority=40,
        download_id=1,
        depends_on_job_id=a.id,
        payload={},
    )
    gate = q.ensure_daemon_reload_gate(
        reason="test", fingerprint="fp1", priority=PRIORITY_DAEMON_RELOAD
    )
    assert gate.kind == JobKind.DAEMON_RELOAD.value
    a2 = q.get(a.id)
    b2 = q.get(b.id)
    assert a2 is not None and a2.depends_on_job_id == gate.id
    # chain child keeps parent dep
    assert b2 is not None and b2.depends_on_job_id == a.id


def test_claim_only_reload_while_gate_pending(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "r.db")
    q.enqueue(JobKind.DOWNLOAD, priority=PRIORITY_DOWNLOAD, payload={"url": "x"})
    gate = q.ensure_daemon_reload_gate(
        reason="test", fingerprint="fp", priority=PRIORITY_DAEMON_RELOAD
    )
    claimed = q.claim_next(kinds={JobKind.EMBED.value})
    assert claimed is not None
    assert claimed.id == gate.id
    assert claimed.kind == JobKind.DAEMON_RELOAD.value
    # while running reload, nothing else claimable
    assert q.claim_next(kinds={JobKind.DOWNLOAD.value}) is None


def test_after_reload_done_other_jobs_run(tmp_path: Path) -> None:
    q = JobQueue(tmp_path / "r.db")
    dl = q.enqueue(JobKind.DOWNLOAD, priority=PRIORITY_DOWNLOAD, payload={"url": "x"})
    gate = q.ensure_daemon_reload_gate(
        reason="test", fingerprint="fp", priority=PRIORITY_DAEMON_RELOAD
    )
    c1 = q.claim_next()
    assert c1 is not None and c1.id == gate.id
    q.mark_done(gate.id, {"action": "reexec"})
    c2 = q.claim_next()
    assert c2 is not None and c2.id == dl.id


def test_ensure_reload_if_stale(tmp_path: Path) -> None:
    cfg = Config(home=tmp_path / "h")
    cfg.ensure_dirs()
    cfg.daemon_auto_reload = True
    q = JobQueue(cfg.db_path)
    # different fingerprint forces gate
    active = ensure_reload_if_stale(
        cfg, q, started_fingerprint="old:0", logger=None
    )
    assert active
    gate = q.find_active_reload()
    assert gate is not None
    # same fingerprint, no second gate
    fp = package_fingerprint()
    q.mark_done(gate.id, {})
    active2 = ensure_reload_if_stale(cfg, q, started_fingerprint=fp, logger=None)
    assert not active2

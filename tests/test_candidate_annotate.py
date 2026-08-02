"""Candidate annotation: past / queued are not default-selected."""

from __future__ import annotations

from pathlib import Path

from vdl.config import Config
from vdl.queue import JobQueue
from vdl.registry import Registry, SourceType
from vdl.scraper import MediaCandidate
from vdl.service import DownloadService
from vdl.workers import enqueue_download


def test_annotate_past_and_queued_unchecked(tmp_path: Path) -> None:
    home = tmp_path / "vdl"
    cfg = Config(home=home)
    cfg.ensure_dirs()
    reg = Registry(cfg.db_path)
    reg.create(
        "https://cdn.example.com/old.mp4",
        source_type=SourceType.DIRECT,
        title="old",
    )
    # mark completed
    rec = reg.find_by_url("https://cdn.example.com/old.mp4", completed_only=False)
    assert rec
    reg.mark_completed(rec.id, file_path=str(home / "old.mp4"), file_size=1)

    q = JobQueue(cfg.db_path)
    enqueue_download(q, "https://cdn.example.com/queued.mp4")

    cands = [
        MediaCandidate("https://cdn.example.com/old.mp4", "video", "anchor"),
        MediaCandidate("https://cdn.example.com/queued.mp4", "video", "anchor"),
        MediaCandidate("https://cdn.example.com/new.mp4", "video", "anchor"),
        MediaCandidate("https://cdn.example.com/old.mp4#dup", "video", "page_text"),
    ]
    svc = DownloadService(cfg)
    rows = svc.annotate_candidates(cands)
    by_url = {r.candidate.url.split("#")[0]: r for r in rows}
    # dedupe fragment
    assert len(rows) == 3

    assert by_url["https://cdn.example.com/old.mp4"].completed
    assert not by_url["https://cdn.example.com/old.mp4"].default_selected

    assert by_url["https://cdn.example.com/queued.mp4"].queued
    assert not by_url["https://cdn.example.com/queued.mp4"].default_selected

    assert by_url["https://cdn.example.com/new.mp4"].default_selected
    assert by_url["https://cdn.example.com/new.mp4"].state_label == "new"

"""
FastAPI backend for vdl web UI.

Run:
  uvicorn api.main:app --reload --port 8000
  # or: python -m api.main
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from vdl.config import Config
from vdl.daemon import daemon_status
from vdl.queue import JobQueue
from vdl.registry import Registry
from vdl.search import SearchService
from vdl.service import DownloadService
from vdl.supervisor import ensure_runtime, supervisor_status
from vdl.workers import enqueue_download, enqueue_index_chain

app = FastAPI(title="vdl API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("VDL_CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_config() -> Config:
    home = os.environ.get("VDL_HOME")
    cfg = Config(home=Path(home) if home else None)
    cfg.ensure_dirs()
    return cfg


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------


class EnqueueBody(BaseModel):
    url: str
    quality: str = "best"
    force: bool = False
    no_index: bool = False
    headers: dict[str, str] = Field(default_factory=dict)


class EnqueueManyBody(BaseModel):
    urls: list[str]
    quality: str = "best"
    force: bool = False
    no_index: bool = False
    headers: dict[str, str] = Field(default_factory=dict)


class CandidatesBody(BaseModel):
    url: str
    headers: dict[str, str] = Field(default_factory=dict)


class TextSearchBody(BaseModel):
    query: str
    limit: int = 8
    by_scene: bool = False


class SimilarSearchBody(BaseModel):
    download_id: int
    limit: int = 8
    modality: str = "clip"
    scene_index: int | None = None
    by_scene: bool = False


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/info")
def info() -> dict[str, Any]:
    cfg = get_config()
    return {
        "home": str(cfg.home),
        "downloads": str(cfg.download_dir),
        "registry": str(cfg.db_path),
        "chroma": str(cfg.chroma_dir),
        "list_limit": cfg.list_limit,
        "list_done_minutes": cfg.list_done_minutes,
        "daemon_workers": cfg.daemon_workers,
        "supervisor_enabled": cfg.supervisor_enabled,
    }


@app.get("/api/status")
def status() -> dict[str, Any]:
    cfg = get_config()
    return {
        "supervisor": supervisor_status(cfg),
        "worker": daemon_status(cfg),
        "queue_counts": JobQueue(cfg.db_path).counts(),
    }


@app.get("/api/queue")
def queue_list(
    status: Optional[str] = None,
    kind: Optional[str] = None,
    limit: Optional[int] = None,
    done_within: Optional[float] = None,
    all_done: bool = False,
) -> dict[str, Any]:
    cfg = get_config()
    q = JobQueue(cfg.db_path)
    lim = int(limit if limit is not None else cfg.list_limit)
    mins = float(done_within if done_within is not None else cfg.list_done_minutes)
    rows, meta = q.list_jobs_display(
        status=status,
        kind=kind,
        limit=lim,
        done_within_minutes=mins,
        include_all_done=all_done or (status == "done"),
    )
    return {
        "counts": q.counts(),
        "meta": meta,
        "jobs": [_job_dict(j) for j in rows],
    }


@app.get("/api/queue/{job_id}")
def queue_show(job_id: int) -> dict[str, Any]:
    cfg = get_config()
    j = JobQueue(cfg.db_path).get(job_id)
    if not j:
        raise HTTPException(404, f"job {job_id} not found")
    out = _job_dict(j)
    if j.download_id:
        rec = Registry(cfg.db_path).get(j.download_id)
        if rec:
            out["registry"] = _download_dict(rec)
    return out


@app.post("/api/queue/{job_id}/retry")
def queue_retry(job_id: int) -> dict[str, Any]:
    cfg = get_config()
    q = JobQueue(cfg.db_path)
    j = q.retry(job_id)
    if not j:
        raise HTTPException(404, f"job {job_id} not found")
    ensure_runtime(cfg)
    return _job_dict(j)


@app.post("/api/queue/{job_id}/cancel")
def queue_cancel(job_id: int) -> dict[str, Any]:
    cfg = get_config()
    j = JobQueue(cfg.db_path).cancel(job_id)
    if not j:
        raise HTTPException(404, f"job {job_id} not found")
    return _job_dict(j)


@app.post("/api/enqueue")
def enqueue(body: EnqueueBody) -> dict[str, Any]:
    cfg = get_config()
    q = JobQueue(cfg.db_path)
    job = enqueue_download(
        q,
        body.url.strip(),
        quality=body.quality,
        force=body.force,
        headers=body.headers,
        no_index=body.no_index,
    )
    role, pid, started = ensure_runtime(cfg)
    return {
        "job": _job_dict(job),
        "runtime": {"role": role, "pid": pid, "started": started},
    }


@app.post("/api/enqueue/batch")
def enqueue_batch(body: EnqueueManyBody) -> dict[str, Any]:
    cfg = get_config()
    q = JobQueue(cfg.db_path)
    jobs = []
    for u in body.urls:
        u = u.strip()
        if not u:
            continue
        job = enqueue_download(
            q,
            u,
            quality=body.quality,
            force=body.force,
            headers=body.headers,
            no_index=body.no_index,
        )
        jobs.append(_job_dict(job))
    role, pid, started = ensure_runtime(cfg)
    return {
        "jobs": jobs,
        "runtime": {"role": role, "pid": pid, "started": started},
    }


@app.post("/api/candidates")
def candidates(body: CandidatesBody) -> dict[str, Any]:
    cfg = get_config()
    with DownloadService(cfg, headers=body.headers) as svc:
        try:
            page_url, rows = svc.fetch_html_candidates(body.url.strip())
        except Exception as e:
            raise HTTPException(400, str(e)) from e
    items = []
    for i, (c, existing) in enumerate(rows, start=1):
        items.append(
            {
                "index": i,
                "url": c.url,
                "kind": c.kind,
                "source": c.source,
                "title": c.title,
                "mime": c.mime,
                "downloaded": existing is not None,
                "download_id": getattr(existing, "id", None) if existing else None,
                "file_path": getattr(existing, "file_path", None) if existing else None,
            }
        )
    return {"page_url": page_url, "count": len(items), "candidates": items}


@app.get("/api/registry")
def registry_list(
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    cfg = get_config()
    reg = Registry(cfg.db_path)
    q = JobQueue(cfg.db_path)
    rows = reg.list_all(status=status, limit=limit)
    out = []
    for r in rows:
        d = _download_dict(r)
        if r.status == "completed":
            d["index"] = q.index_status_for_download(r.id)
        out.append(d)
    return {"downloads": out}


@app.get("/api/registry/{download_id}")
def registry_show(download_id: int) -> dict[str, Any]:
    cfg = get_config()
    reg = Registry(cfg.db_path)
    q = JobQueue(cfg.db_path)
    rec = reg.get(download_id)
    if not rec:
        raise HTTPException(404, f"download {download_id} not found")
    d = _download_dict(rec)
    d["scenes"] = q.list_scenes(download_id)
    d["embeddings"] = q.list_embeddings(download_id)
    if rec.status == "completed":
        d["index"] = q.index_status_for_download(download_id)
    return d


@app.post("/api/registry/{download_id}/reindex")
def registry_reindex(download_id: int) -> dict[str, Any]:
    cfg = get_config()
    rec = Registry(cfg.db_path).get(download_id)
    if not rec or rec.status != "completed":
        raise HTTPException(400, "download must be completed")
    q = JobQueue(cfg.db_path)
    s, c, e = enqueue_index_chain(q, download_id)
    ensure_runtime(cfg)
    return {
        "jobs": {"scene": s.id, "clip": c.id, "embed": e.id},
        "download_id": download_id,
    }


@app.post("/api/search/text")
def search_text(body: TextSearchBody) -> dict[str, Any]:
    cfg = get_config()
    try:
        hits = SearchService(cfg).search_text(
            body.query, limit=body.limit, by_download=not body.by_scene
        )
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    return {"hits": [_hit_dict(h) for h in hits]}


@app.post("/api/search/similar")
def search_similar(body: SimilarSearchBody) -> dict[str, Any]:
    cfg = get_config()
    try:
        hits = SearchService(cfg).search_similar(
            body.download_id,
            scene_index=body.scene_index,
            limit=body.limit,
            modality=body.modality,  # type: ignore[arg-type]
            by_download=not body.by_scene,
        )
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    return {"hits": [_hit_dict(h) for h in hits]}


@app.get("/api/files/{download_id}")
def file_download(download_id: int):
    """Stream registered media file if it exists on disk."""
    cfg = get_config()
    rec = Registry(cfg.db_path).get(download_id)
    if not rec or not rec.file_path:
        raise HTTPException(404, "file not found")
    path = Path(rec.file_path)
    if not path.is_file():
        raise HTTPException(404, f"missing on disk: {path}")
    return FileResponse(path, filename=path.name)


# ---------------------------------------------------------------------------
# serializers
# ---------------------------------------------------------------------------


def _job_dict(j) -> dict[str, Any]:
    return {
        "id": j.id,
        "kind": j.kind,
        "priority": j.priority,
        "status": j.status,
        "download_id": j.download_id,
        "depends_on_job_id": j.depends_on_job_id,
        "progress_pct": j.progress_pct,
        "progress_msg": j.progress_msg,
        "progress_current": j.progress_current,
        "progress_total": j.progress_total,
        "eta_sec": j.eta_sec,
        "progress_label": j.progress_label(),
        "payload": j.payload,
        "result": j.result,
        "error": j.error,
        "created_at": j.created_at,
        "updated_at": j.updated_at,
        "started_at": j.started_at,
        "finished_at": j.finished_at,
    }


def _download_dict(r) -> dict[str, Any]:
    return {
        "id": r.id,
        "url": r.url,
        "canonical_url": r.canonical_url,
        "source_type": r.source_type,
        "status": r.status,
        "file_path": r.file_path,
        "title": r.title,
        "content_type": r.content_type,
        "file_size": r.file_size,
        "file_hash": r.file_hash,
        "m3u8_fingerprint": r.m3u8_fingerprint,
        "error": r.error,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    }


def _hit_dict(h) -> dict[str, Any]:
    return {
        "rank": h.rank,
        "download_id": h.download_id,
        "scene_id": h.scene_id,
        "scene_index": h.scene_index,
        "score": h.score,
        "distance": h.distance,
        "modality": h.modality,
        "title": h.title,
        "path": h.path,
        "url": h.url,
        "document": h.document,
        "start_sec": h.start_sec,
        "end_sec": h.end_sec,
    }


def main() -> None:
    import uvicorn

    # Default 18765 — avoid common busy ports (8000/5173/4000/…).
    uvicorn.run(
        "api.main:app",
        host=os.environ.get("VDL_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("VDL_API_PORT", "18765")),
        reload=os.environ.get("VDL_API_RELOAD", "0") == "1",
    )


if __name__ == "__main__":
    main()

"""
FastAPI backend for vdl web UI.

Run:
  uvicorn api.main:app --reload --port 8000
  # or: python -m api.main
"""

from __future__ import annotations

import os
import secrets
import threading
import time
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


class BridgeItem(BaseModel):
    url: str
    kind: str = "video"
    source: str = "extension"
    title: str | None = None
    mime: str | None = None


class BridgePushBody(BaseModel):
    """Chrome extension → Web UI handoff payload."""

    page_url: str | None = None
    items: list[BridgeItem] = Field(default_factory=list)
    tab: str = "candidates"
    note: str | None = None


# In-memory bridge for extension ↔ Web UI (short TTL).
_BRIDGE: dict[str, dict[str, Any]] = {}
_BRIDGE_LOCK = threading.Lock()
_BRIDGE_TTL_SEC = int(os.environ.get("VDL_BRIDGE_TTL_SEC", "900"))


def _bridge_purge_locked() -> None:
    now = time.time()
    dead = [k for k, v in _BRIDGE.items() if float(v.get("expires", 0)) < now]
    for k in dead:
        del _BRIDGE[k]


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
    q = JobQueue(cfg.db_path)
    return {
        "supervisor": supervisor_status(cfg),
        "worker": daemon_status(cfg),
        "queue_counts": q.counts(include_archived=False),
        "queue_counts_all": q.counts(include_archived=True),
    }


@app.post("/api/queue/housekeep")
def queue_housekeep() -> dict[str, Any]:
    cfg = get_config()
    q = JobQueue(cfg.db_path)
    return q.housekeep(
        archive_after_minutes=cfg.job_archive_minutes,
        retry_after_minutes=cfg.job_housekeep_retry_after_min,
        auto_retry=cfg.job_housekeep_auto_retry,
        max_attempts=cfg.job_max_attempts,
    )


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
        failed_within_minutes=cfg.list_failed_minutes,
        cancelled_within_minutes=cfg.list_cancelled_minutes,
        include_all_done=all_done
        or (status in ("done", "failed", "cancelled", "archived") if status else False),
    )
    return {
        "counts": q.counts(include_archived=False),
        "counts_all": q.counts(include_archived=True),
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


def _reject_if_html_page(url: str, headers: dict[str, str] | None = None) -> None:
    """Refuse to queue bare HTML pages as download jobs (use /api/candidates)."""
    import httpx

    from vdl.classifier import ResourceKind, ResourceProbe

    try:
        with httpx.Client(follow_redirects=True, timeout=20.0) as client:
            kind = ResourceProbe(client, timeout=20.0).probe(
                url, extra_headers=headers or None
            ).kind
    except Exception:
        # Probe failed — allow enqueue; worker will classify again
        return
    if kind == ResourceKind.HTML:
        raise HTTPException(
            400,
            detail={
                "message": (
                    "URL looks like an HTML page; use POST /api/candidates "
                    "then POST /api/enqueue/batch for selected media URLs"
                ),
                "kind": kind.value,
                "hint": "candidates",
            },
        )


@app.post("/api/enqueue")
def enqueue(body: EnqueueBody) -> dict[str, Any]:
    cfg = get_config()
    url = body.url.strip()
    _reject_if_html_page(url, body.headers)
    q = JobQueue(cfg.db_path)
    job = enqueue_download(
        q,
        url,
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
    skipped_html: list[str] = []
    for u in body.urls:
        u = u.strip()
        if not u:
            continue
        try:
            _reject_if_html_page(u, body.headers)
        except HTTPException:
            skipped_html.append(u)
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
    if not jobs and skipped_html:
        raise HTTPException(
            400,
            detail={
                "message": "all URLs look like HTML pages; use /api/candidates first",
                "skipped_html": skipped_html[:20],
                "hint": "candidates",
            },
        )
    role, pid, started = ensure_runtime(cfg)
    return {
        "jobs": jobs,
        "skipped_html": skipped_html,
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
    for i, row in enumerate(rows, start=1):
        c = row.candidate
        items.append(
            {
                "index": i,
                "url": c.url,
                "kind": c.kind,
                "source": c.source,
                "title": c.title,
                "mime": c.mime,
                "downloaded": row.completed,
                "known": row.known,
                "queued": row.queued,
                "state": row.state_label,
                "selected_by_default": row.default_selected,
                "download_id": row.download_id,
                "file_path": getattr(row.record, "file_path", None)
                if row.record
                else None,
                "registry_status": row.registry_status,
            }
        )
    return {"page_url": page_url, "count": len(items), "candidates": items}


@app.post("/api/bridge/push")
def bridge_push(body: BridgePushBody) -> dict[str, Any]:
    """Store media candidates from the extension; Web UI pulls by token."""
    items_in = [i for i in body.items if (i.url or "").strip()]
    if not items_in and not body.page_url:
        raise HTTPException(400, "items or page_url required")

    # Dedupe by URL (keep first)
    seen: set[str] = set()
    uniq: list[BridgeItem] = []
    for it in items_in:
        u = it.url.strip()
        if u in seen:
            continue
        seen.add(u)
        uniq.append(it.model_copy(update={"url": u}))

    from vdl.scraper import MediaCandidate, classify_media_url
    from vdl.service import DownloadService

    cfg = get_config()
    # Drop non-media (e.g. .jpg even with type=video/*) and re-classify
    fake: list[MediaCandidate] = []
    seen_norm: set[str] = set()
    for it in uniq:
        kind = classify_media_url(it.url, mime=it.mime)
        if kind is None and it.kind == "m3u8":
            # content-type HLS without extension: allow only with mpegurl mime
            kind = classify_media_url(
                it.url,
                mime=it.mime or "application/vnd.apple.mpegurl",
            )
        if kind is None:
            continue
        from vdl.util import normalize_url as _nu

        key = _nu(it.url)
        if key in seen_norm:
            continue
        seen_norm.add(key)
        fake.append(
            MediaCandidate(
                url=it.url,
                kind=kind,
                source=it.source or "extension",
                title=it.title,
                mime=it.mime,
            )
        )

    svc = DownloadService(cfg)
    rows = svc.annotate_candidates(fake)
    candidates: list[dict[str, Any]] = []
    for i, row in enumerate(rows, start=1):
        c = row.candidate
        candidates.append(
            {
                "index": i,
                "url": c.url,
                "kind": c.kind,
                "source": c.source,
                "title": c.title,
                "mime": c.mime,
                "downloaded": row.completed,
                "known": row.known,
                "queued": row.queued,
                "state": row.state_label,
                "selected_by_default": row.default_selected,
                "download_id": row.download_id,
                "file_path": getattr(row.record, "file_path", None)
                if row.record
                else None,
                "registry_status": row.registry_status,
            }
        )

    token = secrets.token_urlsafe(12)
    tab = (body.tab or "candidates").strip() or "candidates"
    payload = {
        "token": token,
        "page_url": body.page_url,
        "candidates": candidates,
        "count": len(candidates),
        "tab": tab,
        "note": body.note,
        "created_at": time.time(),
        "expires": time.time() + _BRIDGE_TTL_SEC,
        "from": "extension",
    }
    with _BRIDGE_LOCK:
        _bridge_purge_locked()
        _BRIDGE[token] = payload

    return {
        "token": token,
        "count": len(candidates),
        "tab": tab,
        "ttl_sec": _BRIDGE_TTL_SEC,
        "ui_query": f"vdl_bridge={token}&tab={tab}",
    }


@app.get("/api/bridge/pull")
def bridge_pull(
    token: str = Query(..., min_length=4),
    consume: bool = Query(False, description="Delete after read"),
) -> dict[str, Any]:
    """Web UI fetches extension-pushed candidates."""
    with _BRIDGE_LOCK:
        _bridge_purge_locked()
        payload = _BRIDGE.get(token)
        if not payload:
            raise HTTPException(404, "bridge token not found or expired")
        if consume:
            del _BRIDGE[token]
        else:
            # refresh TTL on read so UI can re-mount once
            payload["expires"] = time.time() + _BRIDGE_TTL_SEC
        out = dict(payload)
    out.pop("expires", None)
    return out


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
            d["preview_url"] = _preview_url_for_download(cfg, q, r.id)
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
    scenes = q.list_scenes(download_id)
    d["scenes"] = [_scene_dict(s, download_id, cfg=cfg) for s in scenes]
    d["embeddings"] = q.list_embeddings(download_id)
    if rec.status == "completed":
        d["index"] = q.index_status_for_download(download_id)
    d["preview_url"] = _preview_url_for_download(cfg, q, download_id)
    return d


@app.get("/api/registry/{download_id}/preview")
def registry_preview(download_id: int):
    """First available keyframe for a download (thumbnail)."""
    cfg = get_config()
    path = _resolve_preview_path(cfg, download_id, scene_index=None)
    return FileResponse(path, media_type="image/jpeg", filename=path.name)


@app.get("/api/registry/{download_id}/scenes/{scene_index}/keyframe")
def registry_scene_keyframe(download_id: int, scene_index: int):
    """Keyframe JPEG for a specific scene."""
    cfg = get_config()
    path = _resolve_preview_path(cfg, download_id, scene_index=scene_index)
    return FileResponse(path, media_type="image/jpeg", filename=path.name)


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
    return {"hits": [_hit_dict(h, with_preview=True) for h in hits]}


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
    return {"hits": [_hit_dict(h, with_preview=True) for h in hits]}


@app.get("/api/files/{download_id}")
def file_download(download_id: int):
    """Stream registered media file if it exists on disk (supports Range for seeking)."""
    cfg = get_config()
    rec = Registry(cfg.db_path).get(download_id)
    if not rec or not rec.file_path:
        raise HTTPException(404, "file not found")
    path = Path(rec.file_path)
    if not path.is_file():
        raise HTTPException(404, f"missing on disk: {path}")
    suffix = path.suffix.lower()
    media = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".mov": "video/quicktime",
        ".m4v": "video/mp4",
        ".ts": "video/mp2t",
    }.get(suffix)
    return FileResponse(path, filename=path.name, media_type=media)


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
        "attempts": getattr(j, "attempts", 0) or 0,
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


def _hit_dict(h, *, with_preview: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
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
    if with_preview and h.download_id is not None and h.download_id >= 0:
        if h.scene_index is not None:
            d["preview_url"] = (
                f"/api/registry/{h.download_id}/scenes/{h.scene_index}/keyframe"
            )
        else:
            d["preview_url"] = f"/api/registry/{h.download_id}/preview"
    return d


def _scene_dict(
    s: dict[str, Any], download_id: int, *, cfg: Config | None = None
) -> dict[str, Any]:
    idx = s.get("scene_index")
    out = dict(s)
    if idx is not None:
        out["keyframe_url"] = f"/api/registry/{download_id}/scenes/{idx}/keyframe"
    has = bool(s.get("keyframe_path") and Path(str(s["keyframe_path"])).is_file())
    if not has and cfg is not None and idx is not None:
        cand = cfg.keyframes_dir / f"dl_{download_id}" / f"scene{int(idx):04d}_key.jpg"
        has = cand.is_file()
    out["has_keyframe"] = has
    return out


def _preview_url_for_download(cfg: Config, q: JobQueue, download_id: int) -> str | None:
    try:
        path = _resolve_preview_path(cfg, download_id, scene_index=None)
    except HTTPException:
        return None
    if path and path.is_file():
        return f"/api/registry/{download_id}/preview"
    return None


def _resolve_preview_path(
    cfg: Config,
    download_id: int,
    *,
    scene_index: int | None,
) -> Path:
    """Resolve a keyframe path under VDL home (safe path check)."""
    q = JobQueue(cfg.db_path)
    rec = Registry(cfg.db_path).get(download_id)
    if not rec:
        raise HTTPException(404, f"download {download_id} not found")

    scenes = q.list_scenes(download_id)
    path: Path | None = None

    if scene_index is not None:
        for s in scenes:
            if s.get("scene_index") == scene_index:
                kp = s.get("keyframe_path")
                if kp and Path(kp).is_file():
                    path = Path(kp)
                break
        if path is None:
            # fallback conventional path (clip job may leave files without DB path)
            cand = cfg.keyframes_dir / f"dl_{download_id}" / f"scene{scene_index:04d}_key.jpg"
            if cand.is_file():
                path = cand
    else:
        # first scene with keyframe on disk
        for s in sorted(scenes, key=lambda x: x.get("scene_index") or 0):
            kp = s.get("keyframe_path")
            if kp and Path(kp).is_file():
                path = Path(kp)
                break
        if path is None:
            ddir = cfg.keyframes_dir / f"dl_{download_id}"
            if ddir.is_dir():
                keys = sorted(ddir.glob("scene*_key.jpg"))
                if keys:
                    path = keys[0]

    if path is None or not path.is_file():
        raise HTTPException(404, "keyframe not found (reindex may be required)")

    # Security: must be under VDL home (keyframes or downloads)
    try:
        resolved = path.resolve()
        home = cfg.home.resolve()
        resolved.relative_to(home)
    except Exception as e:
        raise HTTPException(403, "path outside VDL_HOME") from e
    return resolved


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

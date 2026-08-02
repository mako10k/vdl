"""Job executors for download + index pipeline stages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vdl.config import (
    PRIORITY_CLIP,
    PRIORITY_EMBED,
    PRIORITY_SCENE,
    Config,
)
from vdl.index.clip_opt import select_keyframe_for_scene
from vdl.index.embed import embed_scene_text
from vdl.index.scene import detect_scenes
from vdl.queue import Job, JobKind, JobQueue
from vdl.registry import Registry
from vdl.service import DownloadService
from vdl.vectorstore import VectorStore


def enqueue_download(
    queue: JobQueue,
    url: str,
    *,
    quality: str = "best",
    force: bool = False,
    headers: dict[str, str] | None = None,
    auto_yes: bool = False,
    select_all: bool = False,
    timeout: float = 60.0,
    no_index: bool = False,
    priority: int | None = None,
) -> Job:
    from vdl.config import PRIORITY_DOWNLOAD

    return queue.enqueue(
        JobKind.DOWNLOAD,
        priority=priority if priority is not None else PRIORITY_DOWNLOAD,
        payload={
            "url": url,
            "quality": quality,
            "force": force,
            "headers": headers or {},
            "auto_yes": auto_yes,
            "select_all": select_all,
            "timeout": timeout,
            "no_index": no_index,
        },
    )


def enqueue_index_chain(
    queue: JobQueue,
    download_id: int,
    *,
    after_job_id: int | None = None,
) -> tuple[Job, Job, Job]:
    """scene → clip → embed chain."""
    scene = queue.enqueue(
        JobKind.SCENE_DETECT,
        priority=PRIORITY_SCENE,
        download_id=download_id,
        depends_on_job_id=after_job_id,
        payload={"download_id": download_id},
    )
    clip = queue.enqueue(
        JobKind.CLIP_OPTIMIZE,
        priority=PRIORITY_CLIP,
        download_id=download_id,
        depends_on_job_id=scene.id,
        payload={"download_id": download_id},
    )
    emb = queue.enqueue(
        JobKind.EMBED,
        priority=PRIORITY_EMBED,
        download_id=download_id,
        depends_on_job_id=clip.id,
        payload={"download_id": download_id},
    )
    return scene, clip, emb


def run_job(config: Config, job: Job) -> dict[str, Any]:
    queue = JobQueue(config.db_path)
    if job.kind == JobKind.DOWNLOAD.value:
        return _run_download(config, queue, job)
    if job.kind == JobKind.SCENE_DETECT.value:
        return _run_scene(config, queue, job)
    if job.kind == JobKind.CLIP_OPTIMIZE.value:
        return _run_clip(config, queue, job)
    if job.kind == JobKind.EMBED.value:
        return _run_embed(config, queue, job)
    raise ValueError(f"unknown job kind: {job.kind}")


def _run_download(config: Config, queue: JobQueue, job: Job) -> dict[str, Any]:
    p = job.payload
    url = p["url"]
    with DownloadService(
        config,
        quality=p.get("quality", "best"),
        force=bool(p.get("force")),
        headers=p.get("headers") or {},
        timeout=float(p.get("timeout", 60.0)),
    ) as svc:
        results = svc.process_url(
            url,
            select_all=bool(p.get("select_all")),
            auto_yes=bool(p.get("auto_yes", True)),
        )

    downloaded_ids: list[int] = []
    for r in results:
        if r.record_id and r.status in ("downloaded", "skipped"):
            downloaded_ids.append(r.record_id)

    if not p.get("no_index"):
        reg = Registry(config.db_path)
        for did in downloaded_ids:
            rec = reg.get(did)
            if rec and rec.status == "completed" and rec.file_path:
                enqueue_index_chain(queue, did)

    return {
        "results": [
            {
                "status": r.status,
                "message": r.message,
                "record_id": r.record_id,
                "path": str(r.path) if r.path else None,
            }
            for r in results
        ],
        "download_ids": downloaded_ids,
    }


def _require_download(reg: Registry, download_id: int) -> Any:
    rec = reg.get(download_id)
    if not rec or rec.status != "completed" or not rec.file_path:
        raise RuntimeError(f"download_id={download_id} is not completed with a file")
    path = Path(rec.file_path)
    if not path.exists():
        raise FileNotFoundError(rec.file_path)
    return rec


def _run_scene(config: Config, queue: JobQueue, job: Job) -> dict[str, Any]:
    download_id = int(job.payload.get("download_id") or job.download_id)
    reg = Registry(config.db_path)
    rec = _require_download(reg, download_id)
    scenes = detect_scenes(Path(rec.file_path))
    ids = queue.replace_scenes(download_id, scenes)
    return {"download_id": download_id, "scene_count": len(ids), "scene_ids": ids}


def _run_clip(config: Config, queue: JobQueue, job: Job) -> dict[str, Any]:
    download_id = int(job.payload.get("download_id") or job.download_id)
    reg = Registry(config.db_path)
    rec = _require_download(reg, download_id)
    scenes = queue.list_scenes(download_id)
    if not scenes:
        raise RuntimeError("no scenes; run scene_detect first")

    work = config.keyframes_dir / f"dl_{download_id}"
    work.mkdir(parents=True, exist_ok=True)
    store = VectorStore(config.chroma_dir)
    # clear previous embeddings for re-run of clip stage (text re-added later)
    queue.clear_embeddings(download_id)
    store.delete_for_download(download_id)

    keys = []
    for s in scenes:
        result = select_keyframe_for_scene(
            Path(rec.file_path),
            s["start_sec"],
            s["end_sec"],
            work_dir=work,
            scene_index=s["scene_index"],
        )
        queue.update_scene_keyframe(
            s["id"],
            result["keyframe_path"],
            meta={
                "time_sec": result["time_sec"],
                "centroid_sim": result.get("centroid_sim"),
                "candidates": result.get("candidates"),
            },
        )
        emb = result["clip_embedding"]
        doc = f"clip keyframe download={download_id} scene={s['scene_index']}"
        cid = store.upsert(
            embedding=emb,
            document=doc,
            modality="clip",
            metadata={
                "download_id": download_id,
                "scene_id": s["id"],
                "scene_index": s["scene_index"],
                "modality": "clip",
                "model": result["model"],
            },
        )
        queue.add_embedding(
            download_id=download_id,
            scene_id=s["id"],
            modality="clip",
            model=result["model"],
            chroma_id=cid,
            dim=len(emb),
        )
        keys.append(
            {
                "scene_id": s["id"],
                "keyframe_path": result["keyframe_path"],
                "chroma_id": cid,
            }
        )
    return {"download_id": download_id, "keyframes": keys}


def _run_embed(config: Config, queue: JobQueue, job: Job) -> dict[str, Any]:
    download_id = int(job.payload.get("download_id") or job.download_id)
    reg = Registry(config.db_path)
    rec = _require_download(reg, download_id)
    scenes = queue.list_scenes(download_id)
    if not scenes:
        raise RuntimeError("no scenes; run scene_detect first")

    store = VectorStore(config.chroma_dir)
    items = []
    for s in scenes:
        emb, doc, model = embed_scene_text(
            title=rec.title,
            url=rec.url,
            scene_index=s["scene_index"],
            start_sec=s["start_sec"],
            end_sec=s["end_sec"],
            download_id=download_id,
        )
        cid = store.upsert(
            embedding=emb,
            document=doc,
            modality="text",
            metadata={
                "download_id": download_id,
                "scene_id": s["id"],
                "scene_index": s["scene_index"],
                "modality": "text",
                "model": model,
                "url": rec.url or "",
                "title": rec.title or "",
            },
        )
        queue.add_embedding(
            download_id=download_id,
            scene_id=s["id"],
            modality="text",
            model=model,
            chroma_id=cid,
            dim=len(emb),
        )
        items.append({"scene_id": s["id"], "chroma_id": cid, "model": model})
    return {"download_id": download_id, "embeddings": items}

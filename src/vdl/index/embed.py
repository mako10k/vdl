"""Local LLM / Ollama text embeddings for semantic scene index."""

from __future__ import annotations

from typing import Any

import httpx

from vdl.config import OLLAMA_BASE, OLLAMA_EMBED_MODEL


def ollama_embed(
    text: str,
    *,
    model: str = OLLAMA_EMBED_MODEL,
    base_url: str = OLLAMA_BASE,
    timeout: float = 120.0,
) -> list[float]:
    """Call Ollama /api/embeddings (or /api/embed)."""
    base = base_url.rstrip("/")
    payload = {"model": model, "prompt": text}
    with httpx.Client(timeout=timeout) as client:
        # try modern /api/embed first
        r = client.post(f"{base}/api/embed", json={"model": model, "input": text})
        if r.status_code == 404:
            r = client.post(f"{base}/api/embeddings", json=payload)
        r.raise_for_status()
        data = r.json()
        if "embedding" in data:
            return list(data["embedding"])
        if "embeddings" in data and data["embeddings"]:
            return list(data["embeddings"][0])
        raise RuntimeError(f"unexpected ollama embed response keys: {list(data)}")


def scene_document(
    *,
    title: str | None,
    url: str | None,
    scene_index: int,
    start_sec: float,
    end_sec: float,
    download_id: int,
) -> str:
    t = title or "video"
    u = url or ""
    return (
        f"title: {t}\n"
        f"url: {u}\n"
        f"download_id: {download_id}\n"
        f"scene: {scene_index}\n"
        f"time: {start_sec:.2f}-{end_sec:.2f}s\n"
        f"description: scene {scene_index} of {t} from {start_sec:.1f}s to {end_sec:.1f}s"
    )


def embed_scene_text(
    *,
    title: str | None,
    url: str | None,
    scene_index: int,
    start_sec: float,
    end_sec: float,
    download_id: int,
    model: str = OLLAMA_EMBED_MODEL,
) -> tuple[list[float], str, str]:
    """Return (embedding, document, model_name)."""
    doc = scene_document(
        title=title,
        url=url,
        scene_index=scene_index,
        start_sec=start_sec,
        end_sec=end_sec,
        download_id=download_id,
    )
    emb = ollama_embed(doc, model=model)
    return emb, doc, model

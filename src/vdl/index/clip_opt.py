"""CLIP-based keyframe selection per scene (centroid nearest)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import numpy as np

from vdl.config import (
    CLIP_MODEL,
    CLIP_PRETRAINED,
    FRAMES_PER_SCENE,
)
from vdl.index.scene import extract_frame


_clip_bundle: dict[str, Any] | None = None
_clip_lock = threading.Lock()


def _load_clip() -> tuple[Any, Any, Any, str]:
    """Return (model, preprocess, tokenizer, device). Cached (thread-safe)."""
    global _clip_bundle
    if _clip_bundle is not None:
        return (
            _clip_bundle["model"],
            _clip_bundle["preprocess"],
            _clip_bundle["tokenizer"],
            _clip_bundle["device"],
        )
    with _clip_lock:
        if _clip_bundle is not None:
            return (
                _clip_bundle["model"],
                _clip_bundle["preprocess"],
                _clip_bundle["tokenizer"],
                _clip_bundle["device"],
            )
        import open_clip
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"
        model, _, preprocess = open_clip.create_model_and_transforms(
            CLIP_MODEL, pretrained=CLIP_PRETRAINED
        )
        tokenizer = open_clip.get_tokenizer(CLIP_MODEL)
        model = model.to(device).eval()
        _clip_bundle = {
            "model": model,
            "preprocess": preprocess,
            "tokenizer": tokenizer,
            "device": device,
        }
        return model, preprocess, tokenizer, device


def embed_image_paths(paths: list[Path]) -> np.ndarray:
    """Return L2-normalized CLIP image embeddings shape (N, D)."""
    import torch
    from PIL import Image

    model, preprocess, _, device = _load_clip()
    tensors = []
    for p in paths:
        img = Image.open(p).convert("RGB")
        tensors.append(preprocess(img))
    batch = torch.stack(tensors).to(device)
    # Serialize inference; concurrent CUDA/CLIP calls are fragile.
    with _clip_lock, torch.no_grad():
        feats = model.encode_image(batch)
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.detach().cpu().numpy()


def embed_text_clip(text: str, *, prompts: list[str] | None = None) -> list[float]:
    """
    L2-normalized CLIP text embedding for open-vocabulary visual search.

    Averages several prompt templates so short queries (incl. non-English tokens)
    still land near the image embedding manifold used for keyframes.
    """
    import torch

    text = (text or "").strip()
    if not text:
        raise ValueError("empty text")
    templates = prompts or [
        text,
        f"a photo of {text}",
        f"a video scene of {text}",
        f"a still frame showing {text}",
    ]
    model, _, tokenizer, device = _load_clip()
    tokens = tokenizer(templates).to(device)
    with _clip_lock, torch.no_grad():
        feats = model.encode_text(tokens)
        feats = feats / feats.norm(dim=-1, keepdim=True)
        avg = feats.mean(dim=0)
        avg = avg / avg.norm()
    return [float(x) for x in avg.detach().cpu().tolist()]


def select_keyframe_for_scene(
    video_path: Path,
    start_sec: float,
    end_sec: float,
    *,
    work_dir: Path,
    scene_index: int,
    n_candidates: int = FRAMES_PER_SCENE,
) -> dict[str, Any]:
    """
    Sample candidate frames in [start, end), embed with CLIP,
    pick frame nearest to mean embedding (centroid).
    """
    video_path = Path(video_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    duration = max(end_sec - start_sec, 0.1)
    n = max(1, n_candidates)
    times = [start_sec + duration * (i + 0.5) / n for i in range(n)]

    paths: list[Path] = []
    for i, t in enumerate(times):
        out = work_dir / f"scene{scene_index:04d}_cand{i:02d}.jpg"
        try:
            extract_frame(video_path, t, out)
            paths.append(out)
        except Exception:
            continue

    if not paths:
        # last resort: midpoint
        mid = (start_sec + end_sec) / 2
        out = work_dir / f"scene{scene_index:04d}_key.jpg"
        extract_frame(video_path, mid, out)
        emb = embed_image_paths([out])[0]
        return {
            "keyframe_path": str(out),
            "time_sec": mid,
            "clip_embedding": emb.tolist(),
            "model": f"{CLIP_MODEL}/{CLIP_PRETRAINED}",
            "candidates": 1,
        }

    embs = embed_image_paths(paths)
    centroid = embs.mean(axis=0)
    centroid = centroid / (np.linalg.norm(centroid) + 1e-12)
    sims = embs @ centroid
    best = int(np.argmax(sims))
    key_path = work_dir / f"scene{scene_index:04d}_key.jpg"
    # copy best candidate as key
    if not key_path.exists():
        from vdl.execution import track_partial
        track_partial(key_path)
    key_path.write_bytes(paths[best].read_bytes())
    return {
        "keyframe_path": str(key_path),
        "time_sec": times[best] if best < len(times) else times[0],
        "clip_embedding": embs[best].tolist(),
        "model": f"{CLIP_MODEL}/{CLIP_PRETRAINED}",
        "candidates": len(paths),
        "centroid_sim": float(sims[best]),
    }

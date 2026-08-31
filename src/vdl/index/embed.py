"""Local LLM / Ollama text embeddings and chat helpers."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

import httpx

from vdl.config import (
    OLLAMA_BASE,
    OLLAMA_CHAT_MODEL,
    OLLAMA_EMBED_MODEL,
)

# CJK + kana — CLIP text tower is English-biased
_NON_LATIN_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\u0400-\u04ff]"
)


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


def _list_ollama_models(base_url: str = OLLAMA_BASE, timeout: float = 10.0) -> list[str]:
    base = base_url.rstrip("/")
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(f"{base}/api/tags")
            r.raise_for_status()
            data = r.json()
            return [str(m.get("name") or "") for m in data.get("models") or [] if m.get("name")]
    except Exception:
        return []


def resolve_chat_model(
    preferred: str | None = None,
    *,
    base_url: str = OLLAMA_BASE,
) -> str:
    """
    Pick a usable Ollama chat model.
    Skips pure embedding tags; prefers preferred/env if installed.
    """
    preferred = (preferred or OLLAMA_CHAT_MODEL or "").strip()
    names = _list_ollama_models(base_url)
    if not names:
        return preferred or "llama3.2"

    def is_embed(n: str) -> bool:
        low = n.lower()
        return "embed" in low or "nomic" in low

    chat_names = [n for n in names if not is_embed(n)]
    pool = chat_names or names

    if preferred:
        # exact or prefix match (gemma4 → gemma4:latest)
        for n in pool:
            if n == preferred or n.startswith(preferred + ":") or preferred.startswith(n.split(":")[0]):
                return n
    return pool[0]


def ollama_generate(
    prompt: str,
    *,
    model: str | None = None,
    base_url: str = OLLAMA_BASE,
    timeout: float = 60.0,
    system: str | None = None,
) -> str:
    """
    Call Ollama generate/chat for short text tasks (e.g. query translation).
    Tries /api/chat then /api/generate. Resolves model name against installed tags.
    """
    base = base_url.rstrip("/")
    use_model = resolve_chat_model(model or OLLAMA_CHAT_MODEL, base_url=base_url)
    with httpx.Client(timeout=timeout) as client:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        r = client.post(
            f"{base}/api/chat",
            json={"model": use_model, "messages": messages, "stream": False},
        )
        if r.status_code == 404:
            full = (system + "\n\n" + prompt) if system else prompt
            r = client.post(
                f"{base}/api/generate",
                json={"model": use_model, "prompt": full, "stream": False},
            )
        r.raise_for_status()
        data = r.json()
        if "message" in data and isinstance(data["message"], dict):
            return str(data["message"].get("content") or "").strip()
        if "response" in data:
            return str(data["response"] or "").strip()
        raise RuntimeError(f"unexpected ollama generate keys: {list(data)}")


def needs_clip_translation(query: str) -> bool:
    """True when query likely benefits from English rewrite for CLIP."""
    q = (query or "").strip()
    if not q:
        return False
    if _NON_LATIN_RE.search(q):
        return True
    # mostly non-ascii letters
    letters = [c for c in q if c.isalpha()]
    if letters:
        latin = sum(1 for c in letters if "a" <= c.lower() <= "z")
        if latin / len(letters) < 0.5:
            return True
    return False


@lru_cache(maxsize=256)
def translate_query_for_clip(query: str, model: str = "", base_url: str = "") -> str:
    """
    Translate / rewrite a user query into a short English CLIP phrase.
    Falls back to the original query on any Ollama error.

    ``model`` / ``base_url`` empty → use config defaults (cache-friendly).
    """
    q = (query or "").strip()
    if not q:
        return q
    if not needs_clip_translation(q):
        return q
    use_base = base_url or OLLAMA_BASE
    use_model = model or OLLAMA_CHAT_MODEL
    system = (
        "You are a translator for CLIP visual search. "
        "Convert the user query into a short English phrase describing what to see "
        "in a video frame (nouns, places, objects, actions). "
        "Reply with English words only — no Japanese, no quotes, no preamble."
    )
    prompt = (
        f"Translate to English for image search.\n"
        f"Input: {q}\n"
        f"Output (English only, 2-8 words):"
    )
    try:
        out = ollama_generate(
            prompt, model=use_model, base_url=use_base, system=system, timeout=90.0
        )
    except Exception:
        return q
    # keep first non-empty line, strip quotes/markdown/labels
    line = ""
    for raw_line in (out or "").splitlines():
        s = raw_line.strip().strip("\"'`* ")
        if not s:
            continue
        s = re.sub(r"^(english|translation|phrase|output)\s*[:：]\s*", "", s, flags=re.I)
        if s:
            line = s
            break
    if not line:
        return q
    # If model echoed Japanese, try to extract trailing Latin phrase
    if _NON_LATIN_RE.search(line):
        m = re.search(r"[A-Za-z][A-Za-z0-9 ,\-']{2,}", line)
        if m:
            line = m.group(0).strip()
        else:
            return q
    if len(line) > 200:
        line = line[:200].rsplit(" ", 1)[0]
    return line or q


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

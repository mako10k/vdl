"""Text semantic search and registry-target similar search."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from vdl.config import (
    OLLAMA_BASE,
    OLLAMA_CHAT_MODEL,
    SEARCH_MAX_PER_DOMAIN,
    SEARCH_TRANSLATE,
    Config,
)
from vdl.index.clip_opt import embed_text_clip
from vdl.index.embed import (
    needs_clip_translation,
    ollama_embed,
    translate_query_for_clip,
)
from vdl.queue import JobQueue
from vdl.registry import Registry
from vdl.vectorstore import VectorStore

# clip = visual open-vocab (CLIP text→keyframe index)
# text = metadata docs in Ollama space (titles/urls; weak for content words)
IndexKind = Literal["clip", "text"]
Modality = Literal["text", "clip"]


@dataclass
class SearchHit:
    rank: int
    download_id: int
    scene_id: int | None
    scene_index: int | None
    score: float  # higher is better (1 - distance for cosine)
    distance: float | None
    modality: str
    title: str | None
    path: str | None
    url: str | None
    document: str
    chroma_id: str
    start_sec: float | None = None
    end_sec: float | None = None


@dataclass
class SearchOutcome:
    """Hits plus query rewriting / diversity meta for UI."""

    hits: list[SearchHit]
    query: str
    query_used: str
    translated: bool = False
    modality: str = "clip"
    notes: list[str] = field(default_factory=list)


def _distance_to_score(distance: float | None) -> float:
    """Chroma cosine space: distance lower is better; map to 0..1-ish score."""
    if distance is None:
        return 0.0
    return max(0.0, 1.0 - float(distance))


def _host_key(url: str | None, title: str | None = None) -> str:
    """Registrable-ish host for diversity (full host, port stripped)."""
    if url:
        try:
            host = (urlparse(url).netloc or "").lower()
            if "@" in host:
                host = host.rsplit("@", 1)[-1]
            if ":" in host:
                host = host.split(":", 1)[0]
            if host.startswith("www."):
                host = host[4:]
            if host:
                return host
        except Exception:
            pass
    if title:
        stem = title.split("_")[0].split(".")[0]
        return stem[:24].lower() if stem else "unknown"
    return "unknown"


def _enrich_hit(
    raw: dict[str, Any],
    *,
    rank: int,
    modality: str,
    reg: Registry,
    queue: JobQueue,
) -> SearchHit:
    meta = raw.get("metadata") or {}
    did = meta.get("download_id")
    try:
        did_i = int(did) if did is not None else -1
    except (TypeError, ValueError):
        did_i = -1
    sid = meta.get("scene_id")
    try:
        sid_i = int(sid) if sid is not None else None
    except (TypeError, ValueError):
        sid_i = None
    sidx = meta.get("scene_index")
    try:
        sidx_i = int(sidx) if sidx is not None else None
    except (TypeError, ValueError):
        sidx_i = None

    rec = reg.get(did_i) if did_i >= 0 else None
    start_sec = end_sec = None
    if sid_i is not None and did_i >= 0:
        for s in queue.list_scenes(did_i):
            if s["id"] == sid_i:
                start_sec = s.get("start_sec")
                end_sec = s.get("end_sec")
                if sidx_i is None:
                    sidx_i = s.get("scene_index")
                break

    dist = raw.get("distance")
    return SearchHit(
        rank=rank,
        download_id=did_i,
        scene_id=sid_i,
        scene_index=sidx_i,
        score=_distance_to_score(dist if isinstance(dist, (int, float)) else None),
        distance=float(dist) if isinstance(dist, (int, float)) else None,
        modality=modality,
        title=(rec.title if rec else None) or meta.get("title"),
        path=rec.file_path if rec else None,
        url=(rec.url if rec else None) or meta.get("url"),
        document=raw.get("document") or "",
        chroma_id=raw.get("chroma_id") or "",
        start_sec=start_sec,
        end_sec=end_sec,
    )


def _dedupe_raw_hits(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate chroma rows for same download_id + scene_index."""
    seen: set[tuple[Any, Any]] = set()
    out: list[dict[str, Any]] = []
    for r in raw:
        meta = r.get("metadata") or {}
        key = (meta.get("download_id"), meta.get("scene_index"), meta.get("scene_id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _rerank_copy(hits: list[SearchHit], limit: int) -> list[SearchHit]:
    out: list[SearchHit] = []
    for i, h in enumerate(hits[:limit], start=1):
        out.append(
            SearchHit(
                rank=i,
                download_id=h.download_id,
                scene_id=h.scene_id,
                scene_index=h.scene_index,
                score=h.score,
                distance=h.distance,
                modality=h.modality,
                title=h.title,
                path=h.path,
                url=h.url,
                document=h.document,
                chroma_id=h.chroma_id,
                start_sec=h.start_sec,
                end_sec=h.end_sec,
            )
        )
    return out


def _aggregate_by_download(
    hits: list[SearchHit],
    *,
    exclude_download_id: int | None = None,
    limit: int = 10,
) -> list[SearchHit]:
    """Keep best-scoring scene per download_id."""
    best: dict[int, SearchHit] = {}
    for h in hits:
        if h.download_id < 0:
            continue
        if exclude_download_id is not None and h.download_id == exclude_download_id:
            continue
        prev = best.get(h.download_id)
        if prev is None or h.score > prev.score:
            best[h.download_id] = h
    ordered = sorted(best.values(), key=lambda x: x.score, reverse=True)
    return _rerank_copy(ordered, limit)


def _diversify_hits(
    hits: list[SearchHit],
    *,
    limit: int,
    max_per_domain: int = 2,
) -> list[SearchHit]:
    """
    Score-ordered greedy pick with a per-host cap so one CDN/domain
    cannot fill the entire result list.
    """
    if max_per_domain <= 0 or limit <= 0:
        return _rerank_copy(hits, limit)
    ordered = sorted(hits, key=lambda x: x.score, reverse=True)
    picked: list[SearchHit] = []
    domain_n: dict[str, int] = {}
    deferred: list[SearchHit] = []

    for h in ordered:
        host = _host_key(h.url, h.title)
        if domain_n.get(host, 0) >= max_per_domain:
            deferred.append(h)
            continue
        domain_n[host] = domain_n.get(host, 0) + 1
        picked.append(h)
        if len(picked) >= limit:
            return _rerank_copy(picked, limit)

    # Fill remainder: still prefer hosts under cap, then relax
    for h in deferred:
        if len(picked) >= limit:
            break
        host = _host_key(h.url, h.title)
        if domain_n.get(host, 0) < max_per_domain:
            domain_n[host] = domain_n.get(host, 0) + 1
            picked.append(h)
    if len(picked) < limit:
        have = {id(x) for x in picked}
        for h in ordered:
            if len(picked) >= limit:
                break
            if id(h) in have:
                continue
            picked.append(h)
    return _rerank_copy(picked, limit)


class SearchService:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.config.ensure_dirs()
        self.store = VectorStore(self.config.chroma_dir)
        self.registry = Registry(self.config.db_path)
        self.queue = JobQueue(self.config.db_path)

    def search_text(
        self,
        query: str,
        *,
        limit: int = 10,
        modality: Modality | IndexKind = "clip",
        by_download: bool = True,
        min_score: float | None = None,
        translate: bool | None = None,
        max_per_domain: int | None = None,
    ) -> list[SearchHit]:
        """Return hits only (compat). Prefer search_text_ex for meta."""
        return self.search_text_ex(
            query,
            limit=limit,
            modality=modality,
            by_download=by_download,
            min_score=min_score,
            translate=translate,
            max_per_domain=max_per_domain,
        ).hits

    def search_text_ex(
        self,
        query: str,
        *,
        limit: int = 10,
        modality: Modality | IndexKind = "clip",
        by_download: bool = True,
        min_score: float | None = None,
        translate: bool | None = None,
        max_per_domain: int | None = None,
    ) -> SearchOutcome:
        """
        Text search over the scene index.

        Default **modality=clip**: CLIP text → keyframe index (visual).
        Japanese / non-Latin queries are rewritten to English via Ollama when
        ``translate`` is enabled (default VDL_SEARCH_TRANSLATE=1).

        modality=text uses Ollama embeddings against metadata documents.
        """
        query = (query or "").strip()
        if not query:
            return SearchOutcome(hits=[], query="", query_used="", modality="clip")

        if modality in ("clip", "visual"):
            index: IndexKind = "clip"
        elif modality in ("text", "metadata"):
            index = "text"
        else:
            index = "clip"

        do_tr = SEARCH_TRANSLATE if translate is None else bool(translate)
        query_used = query
        translated = False
        notes: list[str] = []

        if index == "clip":
            if do_tr and needs_clip_translation(query):
                try:
                    en = translate_query_for_clip(
                        query,
                        model=OLLAMA_CHAT_MODEL,
                        base_url=OLLAMA_BASE,
                    )
                    if en and en.strip() and en.strip() != query:
                        query_used = en.strip()
                        translated = True
                        notes.append(f"translated for CLIP: {query_used!r}")
                except Exception as e:
                    notes.append(f"translate skipped: {e}")
            emb = embed_text_clip(query_used)
            # If we translated, also blend original in prompts via embed already multi-template
            store_mod = "clip"
            hit_mod = "clip"
        else:
            emb = ollama_embed(query)
            store_mod = "text"
            hit_mod = "text"

        n = max(limit * 16, 48) if by_download else max(limit * 4, limit)
        raw = self.store.query(emb, n_results=n, modality=store_mod)
        raw = _dedupe_raw_hits(raw)
        hits = [
            _enrich_hit(r, rank=i, modality=hit_mod, reg=self.registry, queue=self.queue)
            for i, r in enumerate(raw, start=1)
        ]
        floor = 0.10 if min_score is None else float(min_score)
        hits = [h for h in hits if h.score >= floor]

        if by_download:
            # first best-per-download, then domain diversity
            pool = max(limit * 4, 24)
            hits = _aggregate_by_download(hits, limit=pool)
            cap = SEARCH_MAX_PER_DOMAIN if max_per_domain is None else int(max_per_domain)
            hits = _diversify_hits(hits, limit=limit, max_per_domain=cap)
        else:
            cap = SEARCH_MAX_PER_DOMAIN if max_per_domain is None else int(max_per_domain)
            hits = _diversify_hits(hits, limit=limit, max_per_domain=max(cap, 3))

        return SearchOutcome(
            hits=hits,
            query=query,
            query_used=query_used,
            translated=translated,
            modality=hit_mod,
            notes=notes,
        )

    def search_similar(
        self,
        download_id: int,
        *,
        scene_id: int | None = None,
        scene_index: int | None = None,
        limit: int = 10,
        modality: Modality = "clip",
        by_download: bool = True,
        max_per_domain: int | None = None,
    ) -> list[SearchHit]:
        return self.search_similar_ex(
            download_id,
            scene_id=scene_id,
            scene_index=scene_index,
            limit=limit,
            modality=modality,
            by_download=by_download,
            max_per_domain=max_per_domain,
        ).hits

    def search_similar_ex(
        self,
        download_id: int,
        *,
        scene_id: int | None = None,
        scene_index: int | None = None,
        limit: int = 10,
        modality: Modality = "clip",
        by_download: bool = True,
        max_per_domain: int | None = None,
    ) -> SearchOutcome:
        """
        Similarity search using a registry download (or one of its scenes) as query.
        """
        rec = self.registry.get(download_id)
        if not rec or rec.status != "completed":
            raise ValueError(f"download_id={download_id} が completed ではありません")

        emb_rows = [
            e
            for e in self.queue.list_embeddings(download_id)
            if e.get("modality") == modality
        ]
        if not emb_rows:
            raise ValueError(
                f"download_id={download_id} に modality={modality} の埋め込みがありません。"
                " `vdl queue reindex {download_id}` を実行してください。"
            )

        if scene_id is not None:
            emb_rows = [e for e in emb_rows if e.get("scene_id") == scene_id]
        elif scene_index is not None:
            scenes = {
                s["scene_index"]: s["id"] for s in self.queue.list_scenes(download_id)
            }
            sid = scenes.get(scene_index)
            if sid is None:
                raise ValueError(f"scene_index={scene_index} が見つかりません")
            emb_rows = [e for e in emb_rows if e.get("scene_id") == sid]

        if not emb_rows:
            raise ValueError("指定シーンの埋め込みがありません")

        chroma_ids = [str(e["chroma_id"]) for e in emb_rows if e.get("chroma_id")]
        vectors = self.store.get_embeddings(chroma_ids, modality=modality)
        if not vectors:
            raise ValueError(
                "Chroma から埋め込みベクトルを取得できませんでした（再索引が必要な可能性）"
            )

        if scene_id is None and scene_index is None and len(vectors) > 1:
            dim = len(vectors[0])
            cols = list(zip(*[v for v in vectors if len(v) == dim]))
            avg = [float(sorted(col)[len(col) // 2]) for col in cols]
        else:
            dim = len(vectors[0])
            avg = [0.0] * dim
            used = 0
            for v in vectors:
                if len(v) != dim:
                    continue
                used += 1
                for i, x in enumerate(v):
                    avg[i] += float(x)
            n = max(1, used)
            avg = [x / n for x in avg]

        norm = sum(x * x for x in avg) ** 0.5 or 1.0
        avg = [x / norm for x in avg]

        n_fetch = max(limit * 16, limit + len(vectors) + 10)
        raw = self.store.query(avg, n_results=n_fetch, modality=modality)
        raw = _dedupe_raw_hits(raw)
        hits = [
            _enrich_hit(r, rank=i, modality=modality, reg=self.registry, queue=self.queue)
            for i, r in enumerate(raw, start=1)
        ]
        hits = [h for h in hits if h.download_id != download_id]

        if by_download:
            pool = max(limit * 4, 24)
            hits = _aggregate_by_download(
                hits, exclude_download_id=download_id, limit=pool
            )
            cap = SEARCH_MAX_PER_DOMAIN if max_per_domain is None else int(max_per_domain)
            hits = _diversify_hits(hits, limit=limit, max_per_domain=cap)
        else:
            cap = SEARCH_MAX_PER_DOMAIN if max_per_domain is None else int(max_per_domain)
            hits = _diversify_hits(hits, limit=limit, max_per_domain=max(cap, 3))

        return SearchOutcome(
            hits=hits,
            query=f"similar:{download_id}",
            query_used=f"similar:{download_id}",
            translated=False,
            modality=modality,
            notes=[],
        )

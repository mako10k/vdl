"""Text semantic search and registry-target similar search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from vdl.config import Config
from vdl.index.embed import ollama_embed
from vdl.queue import JobQueue
from vdl.registry import Registry
from vdl.vectorstore import VectorStore

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


def _distance_to_score(distance: float | None) -> float:
    """Chroma cosine space: distance lower is better; map to 0..1-ish score."""
    if distance is None:
        return 0.0
    # cosine distance in chromadb is typically 1 - cos_sim for some versions,
    # or L2; we treat smaller as better.
    return max(0.0, 1.0 - float(distance))


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
    out: list[SearchHit] = []
    for i, h in enumerate(ordered[:limit], start=1):
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
        modality: Modality = "text",
        by_download: bool = True,
    ) -> list[SearchHit]:
        """
        Text → embed (Ollama) → nearest scenes in the text (or clip) index.
        by_download=True aggregates to one best scene per download.
        """
        query = (query or "").strip()
        if not query:
            return []
        if modality == "clip":
            raise ValueError(
                "テキスト検索は modality=text のみ対応です。"
                "視覚類似は `search similar` を使ってください。"
            )
        emb = ollama_embed(query)
        # over-fetch if aggregating
        n = max(limit * 5, limit) if by_download else limit
        raw = self.store.query(emb, n_results=n, modality="text")
        hits = [
            _enrich_hit(r, rank=i, modality="text", reg=self.registry, queue=self.queue)
            for i, r in enumerate(raw, start=1)
        ]
        if by_download:
            return _aggregate_by_download(hits, limit=limit)
        return hits[:limit]

    def search_similar(
        self,
        download_id: int,
        *,
        scene_id: int | None = None,
        scene_index: int | None = None,
        limit: int = 10,
        modality: Modality = "clip",
        by_download: bool = True,
    ) -> list[SearchHit]:
        """
        Similarity search using a registry download (or one of its scenes) as query.

        Default modality=clip (visual). Use modality=text for semantic neighbors
        of the same video's text embeddings.
        """
        rec = self.registry.get(download_id)
        if not rec or rec.status != "completed":
            raise ValueError(f"download_id={download_id} が completed ではありません")

        # Resolve target embedding(s) from chroma via registry embedding meta
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

        # Filter to one scene if requested
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

        # Average vectors for multi-scene download-level query
        dim = len(vectors[0])
        avg = [0.0] * dim
        for v in vectors:
            if len(v) != dim:
                continue
            for i, x in enumerate(v):
                avg[i] += float(x)
        n = max(1, len(vectors))
        avg = [x / n for x in avg]
        # L2 normalize for cosine space stability
        norm = sum(x * x for x in avg) ** 0.5 or 1.0
        avg = [x / norm for x in avg]

        n_fetch = max(limit * 8, limit + len(vectors) + 5)
        raw = self.store.query(avg, n_results=n_fetch, modality=modality)
        hits = [
            _enrich_hit(r, rank=i, modality=modality, reg=self.registry, queue=self.queue)
            for i, r in enumerate(raw, start=1)
        ]
        # drop self download
        hits = [h for h in hits if h.download_id != download_id]
        if by_download:
            return _aggregate_by_download(
                hits, exclude_download_id=download_id, limit=limit
            )
        return hits[:limit]

"""Chroma-backed vector store for scene embeddings (per-modality collections)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from uuid import uuid4


# Chroma persistent client is not fully multi-thread safe across processes/threads.
_store_lock = threading.Lock()


class VectorStore:
    """
    Thin wrapper around chromadb PersistentClient.
    CLIP (512-d) and text/nomic (768-d) must live in separate collections.
    """

    COLLECTIONS = {
        "clip": "vdl_scenes_clip",
        "text": "vdl_scenes_text",
    }

    def __init__(self, chroma_dir: Path) -> None:
        self.chroma_dir = Path(chroma_dir)
        self.chroma_dir.mkdir(parents=True, exist_ok=True)
        self._client = None
        self._collections: dict[str, Any] = {}

    def _client_ensure(self) -> Any:
        if self._client is not None:
            return self._client
        import chromadb

        self._client = chromadb.PersistentClient(path=str(self.chroma_dir))
        return self._client

    def _collection(self, modality: str) -> Any:
        key = modality if modality in self.COLLECTIONS else "text"
        if key in self._collections:
            return self._collections[key]
        client = self._client_ensure()
        name = self.COLLECTIONS[key]
        col = client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine", "modality": key},
        )
        self._collections[key] = col
        return col

    def upsert(
        self,
        *,
        embedding: list[float],
        document: str,
        metadata: dict[str, Any],
        chroma_id: str | None = None,
        modality: str | None = None,
    ) -> str:
        mod = modality or str(metadata.get("modality") or "text")
        cid = chroma_id or str(uuid4())
        meta = {k: v for k, v in metadata.items() if v is not None}
        meta["modality"] = mod
        with _store_lock:
            col = self._collection(mod)
            col.upsert(
                ids=[cid],
                embeddings=[embedding],
                documents=[document],
                metadatas=[meta],
            )
        return cid

    def delete_for_download(self, download_id: int, modality: str | None = None) -> None:
        mods = [modality] if modality else list(self.COLLECTIONS.keys())
        with _store_lock:
            for mod in mods:
                col = self._collection(mod)
                try:
                    col.delete(where={"download_id": download_id})
                except Exception:
                    pass

    def query(
        self,
        embedding: list[float],
        *,
        n_results: int = 10,
        modality: str = "text",
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        with _store_lock:
            col = self._collection(modality)
            kwargs: dict[str, Any] = {
                "query_embeddings": [embedding],
                "n_results": max(1, n_results),
                "include": ["documents", "metadatas", "distances"],
            }
            if where:
                kwargs["where"] = where
            try:
                res = col.query(**kwargs)
            except Exception:
                # empty collection or bad where
                return []
        out: list[dict[str, Any]] = []
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        for i, cid in enumerate(ids):
            out.append(
                {
                    "chroma_id": cid,
                    "document": docs[i] if i < len(docs) else "",
                    "metadata": metas[i] if i < len(metas) else {},
                    "distance": dists[i] if i < len(dists) else None,
                }
            )
        return out

    def get_embeddings(
        self,
        chroma_ids: list[str],
        *,
        modality: str = "text",
    ) -> list[list[float]]:
        """Fetch stored embedding vectors by chroma ids."""
        if not chroma_ids:
            return []
        with _store_lock:
            col = self._collection(modality)
            try:
                res = col.get(ids=list(chroma_ids), include=["embeddings"])
            except Exception:
                return []
        embs = res.get("embeddings")
        if embs is None:
            return []
        out: list[list[float]] = []
        for e in embs:
            if e is None:
                continue
            # chroma may return numpy arrays — avoid bool() on arrays
            try:
                out.append([float(x) for x in list(e)])
            except (TypeError, ValueError):
                continue
        return out

    def count(self, modality: str = "text") -> int:
        with _store_lock:
            try:
                return int(self._collection(modality).count())
            except Exception:
                return 0

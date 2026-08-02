"""Priority job queue (SQLite) — download preferred over indexing."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Optional


class JobKind(str, Enum):
    DOWNLOAD = "download"
    SCENE_DETECT = "scene_detect"
    CLIP_OPTIMIZE = "clip_optimize"
    EMBED = "embed"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: int
    kind: str
    priority: int
    status: str
    download_id: Optional[int]
    depends_on_job_id: Optional[int]
    payload_json: Optional[str]
    result_json: Optional[str]
    error: Optional[str]
    created_at: str
    updated_at: str
    started_at: Optional[str]
    finished_at: Optional[str]

    @property
    def payload(self) -> dict[str, Any]:
        if not self.payload_json:
            return {}
        try:
            return json.loads(self.payload_json)
        except json.JSONDecodeError:
            return {}

    @property
    def result(self) -> dict[str, Any]:
        if not self.result_json:
            return {}
        try:
            return json.loads(self.result_json)
        except json.JSONDecodeError:
            return {}


class JobQueue:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    download_id INTEGER,
                    depends_on_job_id INTEGER,
                    payload_json TEXT,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status_priority
                    ON jobs(status, priority DESC, id ASC);
                CREATE INDEX IF NOT EXISTS idx_jobs_download
                    ON jobs(download_id);
                CREATE INDEX IF NOT EXISTS idx_jobs_depends
                    ON jobs(depends_on_job_id);

                CREATE TABLE IF NOT EXISTS scenes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    download_id INTEGER NOT NULL,
                    scene_index INTEGER NOT NULL,
                    start_sec REAL NOT NULL,
                    end_sec REAL NOT NULL,
                    keyframe_path TEXT,
                    meta_json TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(download_id, scene_index)
                );
                CREATE INDEX IF NOT EXISTS idx_scenes_download
                    ON scenes(download_id);

                CREATE TABLE IF NOT EXISTS embeddings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    download_id INTEGER NOT NULL,
                    scene_id INTEGER,
                    modality TEXT NOT NULL,
                    model TEXT NOT NULL,
                    chroma_id TEXT NOT NULL,
                    dim INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_embeddings_download
                    ON embeddings(download_id);
                CREATE INDEX IF NOT EXISTS idx_embeddings_scene
                    ON embeddings(scene_id);
                """
            )

    def _row(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            kind=row["kind"],
            priority=row["priority"],
            status=row["status"],
            download_id=row["download_id"],
            depends_on_job_id=row["depends_on_job_id"],
            payload_json=row["payload_json"],
            result_json=row["result_json"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def enqueue(
        self,
        kind: JobKind | str,
        *,
        priority: int,
        payload: dict[str, Any] | None = None,
        download_id: int | None = None,
        depends_on_job_id: int | None = None,
    ) -> Job:
        now = _utc_now()
        k = kind.value if isinstance(kind, JobKind) else kind
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO jobs (
                    kind, priority, status, download_id, depends_on_job_id,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    k,
                    priority,
                    download_id,
                    depends_on_job_id,
                    json.dumps(payload or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            assert row is not None
            return self._row(row)

    def get(self, job_id: int) -> Optional[Job]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return self._row(row) if row else None

    def list_jobs(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[Job]:
        with self._conn() as conn:
            clauses: list[str] = []
            args: list[Any] = []
            if status:
                clauses.append("status = ?")
                args.append(status)
            if kind:
                clauses.append("kind = ?")
                args.append(kind)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            args.append(limit)
            rows = conn.execute(
                f"SELECT * FROM jobs{where} ORDER BY id DESC LIMIT ?",
                args,
            ).fetchall()
            return [self._row(r) for r in rows]

    def claim_next(self) -> Optional[Job]:
        """
        Atomically claim highest-priority runnable job.
        Runnable = pending AND (no dependency OR dependency is done).
        """
        now = _utc_now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT j.* FROM jobs j
                WHERE j.status = 'pending'
                  AND (
                    j.depends_on_job_id IS NULL
                    OR EXISTS (
                      SELECT 1 FROM jobs d
                      WHERE d.id = j.depends_on_job_id AND d.status = 'done'
                    )
                  )
                ORDER BY j.priority DESC, j.id ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """
                UPDATE jobs SET status = 'running', started_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, now, row["id"]),
            )
            claimed = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            assert claimed is not None
            return self._row(claimed)

    def mark_done(
        self, job_id: int, result: dict[str, Any] | None = None
    ) -> Job:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE jobs SET
                    status = 'done',
                    result_json = ?,
                    error = NULL,
                    finished_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(result or {}, ensure_ascii=False),
                    now,
                    now,
                    job_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            assert row is not None
            return self._row(row)

    def mark_failed(self, job_id: int, error: str) -> Job:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE jobs SET
                    status = 'failed',
                    error = ?,
                    finished_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (error, now, now, job_id),
            )
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            assert row is not None
            return self._row(row)

    def cancel(self, job_id: int) -> Optional[Job]:
        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                return None
            if row["status"] not in ("pending", "running"):
                return self._row(row)
            conn.execute(
                """
                UPDATE jobs SET status = 'cancelled', finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, job_id),
            )
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            assert row is not None
            return self._row(row)

    def retry(self, job_id: int) -> Optional[Job]:
        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                return None
            if row["status"] not in ("failed", "cancelled"):
                return self._row(row)
            conn.execute(
                """
                UPDATE jobs SET
                    status = 'pending',
                    error = NULL,
                    result_json = NULL,
                    started_at = NULL,
                    finished_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, job_id),
            )
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            assert row is not None
            return self._row(row)

    def counts(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
            return {r["status"]: r["n"] for r in rows}

    # --- scenes / embeddings meta ---

    def replace_scenes(
        self,
        download_id: int,
        scenes: list[dict[str, Any]],
    ) -> list[int]:
        """Replace all scenes for a download. scenes: start_sec, end_sec, keyframe_path?, meta?"""
        now = _utc_now()
        ids: list[int] = []
        with self._conn() as conn:
            conn.execute("DELETE FROM scenes WHERE download_id = ?", (download_id,))
            for i, s in enumerate(scenes):
                cur = conn.execute(
                    """
                    INSERT INTO scenes (
                        download_id, scene_index, start_sec, end_sec,
                        keyframe_path, meta_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        download_id,
                        i,
                        float(s["start_sec"]),
                        float(s["end_sec"]),
                        s.get("keyframe_path"),
                        json.dumps(s.get("meta") or {}, ensure_ascii=False),
                        now,
                    ),
                )
                ids.append(int(cur.lastrowid))
        return ids

    def list_scenes(self, download_id: int) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scenes WHERE download_id = ?
                ORDER BY scene_index ASC
                """,
                (download_id,),
            ).fetchall()
            out = []
            for r in rows:
                out.append(
                    {
                        "id": r["id"],
                        "download_id": r["download_id"],
                        "scene_index": r["scene_index"],
                        "start_sec": r["start_sec"],
                        "end_sec": r["end_sec"],
                        "keyframe_path": r["keyframe_path"],
                        "meta_json": r["meta_json"],
                    }
                )
            return out

    def update_scene_keyframe(
        self, scene_id: int, keyframe_path: str, meta: dict[str, Any] | None = None
    ) -> None:
        with self._conn() as conn:
            if meta is not None:
                conn.execute(
                    """
                    UPDATE scenes SET keyframe_path = ?, meta_json = ?
                    WHERE id = ?
                    """,
                    (keyframe_path, json.dumps(meta, ensure_ascii=False), scene_id),
                )
            else:
                conn.execute(
                    "UPDATE scenes SET keyframe_path = ? WHERE id = ?",
                    (keyframe_path, scene_id),
                )

    def add_embedding(
        self,
        *,
        download_id: int,
        scene_id: int | None,
        modality: str,
        model: str,
        chroma_id: str,
        dim: int | None,
    ) -> int:
        now = _utc_now()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO embeddings (
                    download_id, scene_id, modality, model, chroma_id, dim, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (download_id, scene_id, modality, model, chroma_id, dim, now),
            )
            return int(cur.lastrowid)

    def clear_embeddings(self, download_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM embeddings WHERE download_id = ?", (download_id,)
            )

    def list_embeddings(self, download_id: int | None = None) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if download_id is None:
                rows = conn.execute(
                    "SELECT * FROM embeddings ORDER BY id DESC LIMIT 200"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM embeddings WHERE download_id = ? ORDER BY id",
                    (download_id,),
                ).fetchall()
            return [dict(r) for r in rows]

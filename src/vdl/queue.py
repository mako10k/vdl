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
    # Implicit gate: all other work waits on this; daemon re-execs to load new code.
    DAEMON_RELOAD = "daemon_reload"


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
    progress_pct: Optional[float] = 0.0
    progress_msg: Optional[str] = None
    progress_current: Optional[int] = None
    progress_total: Optional[int] = None
    eta_sec: Optional[float] = None

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

    def progress_label(self, *, bar_width: int = 10) -> str:
        """Human-readable progress for list views."""
        if self.kind == JobKind.DAEMON_RELOAD.value:
            if self.status == "pending":
                return "⚡ self-update gate (blocks all)"
            if self.status == "running":
                return "⚡ reloading daemon…"
            if self.status == "done":
                return "self-update done"
        if self.status == "pending":
            return "queued"
        if self.status == "cancelled":
            return "cancelled"
        if self.status == "failed":
            pct = self.progress_pct if self.progress_pct is not None else 0.0
            return f"failed @{pct:.0f}%"
        if self.status == "done":
            return "100% done"

        pct = float(self.progress_pct or 0.0)
        pct = max(0.0, min(100.0, pct))
        filled = int(round(bar_width * pct / 100.0))
        filled = min(bar_width, max(0, filled))
        bar = "=" * filled + "-" * (bar_width - filled)
        parts = [f"[{bar}] {pct:5.1f}%"]
        if self.progress_current is not None and self.progress_total:
            parts.append(f"{self.progress_current}/{self.progress_total}")
        if self.progress_msg:
            msg = self.progress_msg
            if len(msg) > 28:
                msg = msg[:25] + "…"
            parts.append(msg)
        eta = self.eta_sec
        if eta is None:
            eta = estimate_eta_sec(self.started_at, pct)
        if eta is not None and self.status == "running" and pct < 100:
            parts.append(f"ETA {format_duration(eta)}")
        return " ".join(parts)


def format_duration(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def estimate_eta_sec(started_at: str | None, progress_pct: float) -> float | None:
    """Linear ETA from elapsed / progress ratio."""
    if not started_at or progress_pct is None or progress_pct <= 0.5:
        return None
    if progress_pct >= 100:
        return 0.0
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if elapsed <= 0:
            return None
        total = elapsed / (progress_pct / 100.0)
        return max(0.0, total - elapsed)
    except Exception:
        return None


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
                    finished_at TEXT,
                    progress_pct REAL DEFAULT 0,
                    progress_msg TEXT,
                    progress_current INTEGER,
                    progress_total INTEGER,
                    eta_sec REAL
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
            self._migrate_job_progress_columns(conn)

    def _migrate_job_progress_columns(self, conn: sqlite3.Connection) -> None:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        migrations = [
            ("progress_pct", "REAL DEFAULT 0"),
            ("progress_msg", "TEXT"),
            ("progress_current", "INTEGER"),
            ("progress_total", "INTEGER"),
            ("eta_sec", "REAL"),
        ]
        for name, decl in migrations:
            if name not in cols:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")

    def _row(self, row: sqlite3.Row) -> Job:
        keys = row.keys()
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
            progress_pct=row["progress_pct"] if "progress_pct" in keys else 0.0,
            progress_msg=row["progress_msg"] if "progress_msg" in keys else None,
            progress_current=row["progress_current"] if "progress_current" in keys else None,
            progress_total=row["progress_total"] if "progress_total" in keys else None,
            eta_sec=row["eta_sec"] if "eta_sec" in keys else None,
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

    def find_active_reload(self) -> Optional[Job]:
        """Pending or running daemon_reload job (newest)."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE kind = ?
                  AND status IN ('pending', 'running')
                ORDER BY id DESC
                LIMIT 1
                """,
                (JobKind.DAEMON_RELOAD.value,),
            ).fetchone()
            return self._row(row) if row else None

    def reclaim_stale_running(self) -> int:
        """
        After daemon crash/re-exec, mark orphaned running jobs as pending again
        (except daemon_reload, which is re-enqueued as pending too).
        """
        now = _utc_now()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE jobs SET
                    status = 'pending',
                    started_at = NULL,
                    finished_at = NULL,
                    progress_pct = 0,
                    progress_msg = 'reclaimed after daemon restart',
                    eta_sec = NULL,
                    updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
            return int(cur.rowcount or 0)

    def ensure_daemon_reload_gate(
        self,
        *,
        reason: str,
        fingerprint: str,
        priority: int = 10_000,
    ) -> Job:
        """
        Ensure a daemon_reload job exists. Other pending root jobs get
        depends_on_job_id → reload so the gate is visible in the queue.
        """
        existing = self.find_active_reload()
        if existing:
            return existing

        job = self.enqueue(
            JobKind.DAEMON_RELOAD,
            priority=priority,
            payload={
                "reason": reason,
                "fingerprint": fingerprint,
                "implicit_gate": True,
            },
        )
        # Attach all currently claimable pending roots to the reload gate.
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE jobs SET
                    depends_on_job_id = ?,
                    updated_at = ?
                WHERE status = 'pending'
                  AND id != ?
                  AND kind != ?
                  AND (
                    depends_on_job_id IS NULL
                    OR EXISTS (
                      SELECT 1 FROM jobs d
                      WHERE d.id = jobs.depends_on_job_id
                        AND d.status IN ('done', 'failed', 'cancelled')
                    )
                  )
                """,
                (job.id, now, job.id, JobKind.DAEMON_RELOAD.value),
            )
        return self.get(job.id) or job

    def claim_next(self) -> Optional[Job]:
        """
        Atomically claim highest-priority runnable job.
        Runnable = pending AND (no dependency OR dependency is done).

        If a daemon_reload gate is active (pending/running), only that job
        may be claimed — all other work is blocked (implicit global dependency).
        """
        now = _utc_now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")

            gate = conn.execute(
                """
                SELECT * FROM jobs
                WHERE kind = ?
                  AND status IN ('pending', 'running')
                ORDER BY id DESC
                LIMIT 1
                """,
                (JobKind.DAEMON_RELOAD.value,),
            ).fetchone()

            if gate is not None:
                if gate["status"] == "running":
                    # Reload already in progress — do not claim anything else.
                    return None
                # Only claim the reload gate (must be runnable).
                row = conn.execute(
                    """
                    SELECT j.* FROM jobs j
                    WHERE j.id = ?
                      AND j.status = 'pending'
                      AND (
                        j.depends_on_job_id IS NULL
                        OR EXISTS (
                          SELECT 1 FROM jobs d
                          WHERE d.id = j.depends_on_job_id AND d.status = 'done'
                        )
                      )
                    """,
                    (gate["id"],),
                ).fetchone()
            else:
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
                UPDATE jobs SET
                    status = 'running',
                    started_at = ?,
                    updated_at = ?,
                    progress_pct = 0,
                    progress_msg = 'starting',
                    progress_current = NULL,
                    progress_total = NULL,
                    eta_sec = NULL
                WHERE id = ? AND status = 'pending'
                """,
                (now, now, row["id"]),
            )
            claimed = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            assert claimed is not None
            return self._row(claimed)

    def set_download_id(self, job_id: int, download_id: int) -> None:
        """Link a job to a registry downloads.id (for download/index jobs)."""
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE jobs SET download_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (download_id, now, job_id),
            )

    def update_progress(
        self,
        job_id: int,
        *,
        pct: float | None = None,
        message: str | None = None,
        current: int | None = None,
        total: int | None = None,
        eta_sec: float | None = None,
        started_at: str | None = None,
    ) -> None:
        """Best-effort progress update for list/status UIs."""
        now = _utc_now()
        if pct is not None:
            pct = max(0.0, min(100.0, float(pct)))
        # auto ETA if not provided
        if eta_sec is None and pct is not None and started_at:
            eta_sec = estimate_eta_sec(started_at, pct)
        with self._conn() as conn:
            # load started_at if needed for ETA
            if eta_sec is None and pct is not None:
                row = conn.execute(
                    "SELECT started_at FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if row and row["started_at"]:
                    eta_sec = estimate_eta_sec(row["started_at"], pct)
            conn.execute(
                """
                UPDATE jobs SET
                    progress_pct = COALESCE(?, progress_pct),
                    progress_msg = COALESCE(?, progress_msg),
                    progress_current = COALESCE(?, progress_current),
                    progress_total = COALESCE(?, progress_total),
                    eta_sec = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (pct, message, current, total, eta_sec, now, job_id),
            )

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
                    updated_at = ?,
                    progress_pct = 100,
                    progress_msg = 'done',
                    eta_sec = 0
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

    def index_status_for_download(self, download_id: int) -> dict[str, Any]:
        """Aggregate index pipeline progress for a download (list UIs)."""
        with self._conn() as conn:
            scenes = conn.execute(
                "SELECT COUNT(*) AS n FROM scenes WHERE download_id = ?",
                (download_id,),
            ).fetchone()["n"]
            clip_n = conn.execute(
                """
                SELECT COUNT(*) AS n FROM embeddings
                WHERE download_id = ? AND modality = 'clip'
                """,
                (download_id,),
            ).fetchone()["n"]
            text_n = conn.execute(
                """
                SELECT COUNT(*) AS n FROM embeddings
                WHERE download_id = ? AND modality = 'text'
                """,
                (download_id,),
            ).fetchone()["n"]
            jobs = conn.execute(
                """
                SELECT kind, status, progress_pct, progress_msg
                FROM jobs WHERE download_id = ?
                ORDER BY id DESC
                """,
                (download_id,),
            ).fetchall()
        by_kind: dict[str, sqlite3.Row] = {}
        for j in jobs:
            if j["kind"] not in by_kind:
                by_kind[j["kind"]] = j

        # pipeline weights: scene 20 / clip 40 / embed 40
        def stage_pct(kind: str) -> float:
            j = by_kind.get(kind)
            if not j:
                # infer from artifacts
                if kind == "scene_detect":
                    return 100.0 if scenes > 0 else 0.0
                if kind == "clip_optimize":
                    if scenes <= 0:
                        return 0.0
                    return min(100.0, 100.0 * clip_n / scenes)
                if kind == "embed":
                    if scenes <= 0:
                        return 0.0
                    return min(100.0, 100.0 * text_n / scenes)
                return 0.0
            if j["status"] == "done":
                return 100.0
            if j["status"] == "failed":
                return float(j["progress_pct"] or 0.0)
            if j["status"] == "running":
                return float(j["progress_pct"] or 0.0)
            return 0.0  # pending

        sp, cp, ep = (
            stage_pct("scene_detect"),
            stage_pct("clip_optimize"),
            stage_pct("embed"),
        )
        overall = 0.2 * sp + 0.4 * cp + 0.4 * ep
        active_msg = None
        for kind in ("embed", "clip_optimize", "scene_detect"):
            j = by_kind.get(kind)
            if j and j["status"] == "running":
                if j["progress_msg"]:
                    active_msg = f"{kind}: {j['progress_msg']}"
                else:
                    active_msg = f"{kind}: {float(j['progress_pct'] or 0):.0f}%"
                break
            if j and j["status"] == "pending":
                active_msg = f"queued {kind}"
                break

        return {
            "scenes": scenes,
            "clip_embeddings": clip_n,
            "text_embeddings": text_n,
            "scene_pct": sp,
            "clip_pct": cp,
            "embed_pct": ep,
            "overall_pct": overall,
            "message": active_msg
            or (
                "indexed"
                if overall >= 99.9
                else ("partial" if overall > 0 else "not indexed")
            ),
        }

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

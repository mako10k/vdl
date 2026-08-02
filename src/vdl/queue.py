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
    ARCHIVED = "archived"


# Errors that usually succeed on another attempt
_RETRYABLE_HINTS = (
    "timeout",
    "timed out",
    "temporarily",
    "connection",
    "reset by peer",
    "broken pipe",
    "frame extract failed",
    "ffmpeg",
    "httpx",
    "http error",
    "status 5",
    "503",
    "502",
    "504",
    "429",
    "unavailable",
    "try again",
    "econnreset",
    "network",
    "ssl",
    "read error",
    "write error",
    "errno",
)
_NON_RETRYABLE_HINTS = (
    "unsupported",
    "mvp",
    "not a media",
    "invalid url",
    "404",
    "403",
    "401",
    "permission denied",
    "no such file",
    "missing on disk",
    "cancelled (upstream",
    "upstream job",
)


def is_retryable_error(error: str | None, *, kind: str | None = None) -> bool:
    """Heuristic: whether a failed job is worth another try."""
    if not error:
        return True  # unknown — allow one housekeep retry
    low = error.lower()
    if any(h in low for h in _NON_RETRYABLE_HINTS):
        return False
    if any(h in low for h in _RETRYABLE_HINTS):
        return True
    # download / index stages: default optimistic for transient infra flakes
    if kind in {
        JobKind.DOWNLOAD.value,
        JobKind.SCENE_DETECT.value,
        JobKind.CLIP_OPTIMIZE.value,
        JobKind.EMBED.value,
    }:
        return True
    return False


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t
    except Exception:
        return None


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
    # Number of finished attempts (failed runs). 0 = not yet failed.
    attempts: int = 0

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
        if self.status == "archived":
            return "archived"
        if self.status == "failed":
            pct = self.progress_pct if self.progress_pct is not None else 0.0
            att = f" after {self.attempts} try" if self.attempts else ""
            if self.attempts and self.attempts != 1:
                att = f" after {self.attempts} tries"
            return f"failed @{pct:.0f}%{att}"
        if self.status == "done":
            return "100% done"

        pct = float(self.progress_pct or 0.0)
        pct = max(0.0, min(100.0, pct))
        filled = int(round(bar_width * pct / 100.0))
        filled = min(bar_width, max(0, filled))
        bar = "=" * filled + "-" * (bar_width - filled)
        parts = [f"[{bar}] {pct:5.1f}%"]
        if self.attempts and self.status in ("pending", "running"):
            parts.append(f"try {self.attempts + 1}")
        if self.progress_current is not None and self.progress_total:
            parts.append(f"{self.progress_current}/{self.progress_total}")
        if self.progress_msg:
            msg = self.progress_msg
            if len(msg) > 28:
                msg = msg[:25] + "…"
            parts.append(msg)
        # Always recompute ETA from "now" for running jobs (stale eta_sec is ignored).
        if self.status == "running" and pct < 100:
            eta = estimate_eta_sec(self.started_at, pct)
            if eta is not None:
                parts.append(f"ETA {format_duration(eta)}")
            else:
                elapsed = elapsed_sec(self.started_at)
                if elapsed is not None and elapsed >= 1:
                    parts.append(f"run {format_duration(elapsed)}")
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


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        started = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return started
    except Exception:
        return None


def elapsed_sec(started_at: str | None) -> float | None:
    started = _parse_iso(started_at)
    if not started:
        return None
    return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())


def estimate_eta_sec(started_at: str | None, progress_pct: float) -> float | None:
    """
    Linear ETA from elapsed / progress ratio.
    Recomputed against wall-clock "now" every time it is called (for live UI).
    """
    if progress_pct is None or progress_pct < 1.0:
        return None
    if progress_pct >= 100:
        return 0.0
    elapsed = elapsed_sec(started_at)
    if elapsed is None or elapsed <= 0:
        return None
    total = elapsed / (progress_pct / 100.0)
    return max(0.0, total - elapsed)


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
            ("attempts", "INTEGER DEFAULT 0"),
            ("housekeep_retried", "INTEGER DEFAULT 0"),
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
            attempts=int(row["attempts"] or 0) if "attempts" in keys else 0,
        )

    def _job_age_minutes(self, job: Job, *, now: datetime | None = None) -> float | None:
        now = now or datetime.now(timezone.utc)
        ts = _parse_ts(job.finished_at or job.updated_at)
        if not ts:
            return None
        return max(0.0, (now - ts).total_seconds() / 60.0)

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

    def active_download_urls(self) -> set[str]:
        """Normalized URLs with pending/running download jobs (for candidate dedupe)."""
        from vdl.util import normalize_url

        out: set[str] = set()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT payload_json FROM jobs
                WHERE kind = 'download'
                  AND status IN ('pending', 'running')
                """
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            url = payload.get("url")
            if isinstance(url, str) and url.strip():
                out.add(normalize_url(url.strip()))
            # batch HTML may store urls list
            urls = payload.get("urls")
            if isinstance(urls, list):
                for u in urls:
                    if isinstance(u, str) and u.strip():
                        out.add(normalize_url(u.strip()))
        return out

    def list_jobs_display(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 30,
        done_within_minutes: float = 10.0,
        failed_within_minutes: float | None = None,
        cancelled_within_minutes: float | None = None,
        include_all_done: bool = False,
        include_archived: bool = False,
        major_kinds: tuple[str, ...] | None = None,
    ) -> tuple[list[Job], dict[str, Any]]:
        """
        List jobs for CLI/watch — only “effective” rows by default.

        Default keeps:
        - all pending / running
        - done finished within done_within_minutes
        - failed finished within failed_within_minutes (default 30)
        - cancelled finished within cancelled_within_minutes (default 10)
          (upstream-cascade cancels hide sooner: half window)
        - never archived (unless status=archived or include_archived)

        include_all_done / status filters bypass time windows for that status.
        """
        from vdl.config import LIST_CANCELLED_MINUTES, LIST_FAILED_MINUTES

        fail_mins = (
            LIST_FAILED_MINUTES
            if failed_within_minutes is None
            else float(failed_within_minutes)
        )
        canc_mins = (
            LIST_CANCELLED_MINUTES
            if cancelled_within_minutes is None
            else float(cancelled_within_minutes)
        )

        # Fetch a wider window then filter (simple, portable).
        fetch_n = max(limit * 20, 200)
        if status == "archived" or include_archived:
            raw = self.list_jobs(status=status, kind=kind, limit=fetch_n)
        elif status:
            raw = self.list_jobs(status=status, kind=kind, limit=fetch_n)
        else:
            # exclude archived from default pull
            with self._conn() as conn:
                clauses = ["status != 'archived'"]
                args: list[Any] = []
                if kind:
                    clauses.append("kind = ?")
                    args.append(kind)
                args.append(fetch_n)
                rows_db = conn.execute(
                    f"SELECT * FROM jobs WHERE {' AND '.join(clauses)} "
                    f"ORDER BY id DESC LIMIT ?",
                    args,
                ).fetchall()
                raw = [self._row(r) for r in rows_db]

        if include_all_done or status in (
            "done",
            "failed",
            "cancelled",
            "archived",
            "pending",
            "running",
        ):
            rows = raw[:limit]
            return rows, {
                "filtered": False,
                "hidden": 0,
                "done_within_minutes": done_within_minutes,
                "failed_within_minutes": fail_mins,
                "cancelled_within_minutes": canc_mins,
                "limit": limit,
            }

        now = datetime.now(timezone.utc)
        hidden = {"done": 0, "failed": 0, "cancelled": 0, "archived": 0}

        def _age_ok(j: Job, minutes: float) -> bool:
            age = self._job_age_minutes(j, now=now)
            if age is None:
                return True
            return age <= max(0.0, minutes)

        def _keep(j: Job) -> bool:
            if j.status == "archived":
                hidden["archived"] += 1
                return False
            if j.status in ("pending", "running"):
                return True
            if j.status == "done":
                if _age_ok(j, done_within_minutes):
                    return True
                hidden["done"] += 1
                return False
            if j.status == "failed":
                if _age_ok(j, fail_mins):
                    return True
                hidden["failed"] += 1
                return False
            if j.status == "cancelled":
                # cascade noise: shorter window
                err = (j.error or "") + " " + (j.progress_msg or "")
                window = canc_mins
                if "upstream" in err.lower():
                    window = min(window, max(2.0, canc_mins * 0.5))
                if _age_ok(j, window):
                    return True
                hidden["cancelled"] += 1
                return False
            return True

        kept = [j for j in raw if _keep(j)]
        active_order = {
            "running": 0,
            "pending": 1,
            "failed": 2,
            "cancelled": 3,
            "done": 4,
            "archived": 5,
        }
        kept.sort(key=lambda j: (active_order.get(j.status, 9), -j.id))
        rows = kept[:limit]
        return rows, {
            "filtered": True,
            "hidden": sum(hidden.values()),
            "hidden_detail": hidden,
            "hidden_done": hidden["done"],  # backward compat
            "done_within_minutes": done_within_minutes,
            "failed_within_minutes": fail_mins,
            "cancelled_within_minutes": canc_mins,
            "limit": limit,
        }

    def housekeep(
        self,
        *,
        archive_after_minutes: float = 60.0,
        retry_after_minutes: float = 5.0,
        auto_retry: bool = True,
        max_attempts: int | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """
        Maintenance pass:
        1) cancel orphans stuck on failed parents
        2) auto-retry failed jobs that look recoverable (once via housekeep_retried)
        3) archive done/failed/cancelled older than archive_after_minutes
           (retryable failures get one last retry before archive if not yet retried)
        """
        from vdl.config import JOB_MAX_ATTEMPTS

        max_att = max(1, int(max_attempts if max_attempts is not None else JOB_MAX_ATTEMPTS))
        now = datetime.now(timezone.utc)
        stats: dict[str, Any] = {
            "orphans_cancelled": 0,
            "retried": 0,
            "archived": 0,
            "retry_ids": [],
            "archived_ids": [],
            "max_attempts": max_att,
        }

        orphans = self.cancel_orphaned_dependents()
        stats["orphans_cancelled"] = len(orphans)

        # Load terminal-ish jobs
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('failed', 'done', 'cancelled')
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        for row in rows:
            job = self._row(row)
            age = self._job_age_minutes(job, now=now)
            if age is None:
                continue
            hk = 0
            if "housekeep_retried" in row.keys():
                hk = int(row["housekeep_retried"] or 0)

            if job.status == "failed" and auto_retry:
                retryable = is_retryable_error(job.error, kind=job.kind)
                cool = age >= max(0.0, retry_after_minutes)
                # last chance right at archive age if not yet housekeep-retried
                last_chance = age >= max(0.0, archive_after_minutes) and hk == 0
                # Single extra attempt only: do not reset full attempt budget
                under_cap = int(job.attempts or 0) < max_att + 1
                if retryable and hk == 0 and under_cap and (cool or last_chance):
                    with self._conn() as conn:
                        conn.execute(
                            """
                            UPDATE jobs SET
                                status = 'pending',
                                housekeep_retried = 1,
                                error = ?,
                                result_json = NULL,
                                started_at = NULL,
                                finished_at = NULL,
                                progress_msg = ?,
                                eta_sec = NULL,
                                updated_at = ?
                            WHERE id = ? AND status = 'failed'
                            """,
                            (
                                job.error,
                                f"housekeep retry (attempts={job.attempts}/{max_att})",
                                _utc_now(),
                                job.id,
                            ),
                        )
                        # reopen cancelled direct chain once (same as manual retry)
                        conn.execute(
                            """
                            UPDATE jobs SET
                                status = 'pending',
                                attempts = 0,
                                error = NULL,
                                finished_at = NULL,
                                started_at = NULL,
                                progress_msg = 'retry (housekeep chain)',
                                updated_at = ?
                            WHERE depends_on_job_id = ?
                              AND status = 'cancelled'
                            """,
                            (_utc_now(), job.id),
                        )
                    stats["retried"] += 1
                    stats["retry_ids"].append(job.id)
                    continue

            if age >= max(0.0, archive_after_minutes):
                if job.status == "failed" and auto_retry and hk == 0 and is_retryable_error(
                    job.error, kind=job.kind
                ):
                    continue
                # Never archive a parent while dependents are still live
                with self._conn() as conn:
                    live = conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM jobs
                        WHERE depends_on_job_id = ?
                          AND status IN ('pending', 'running')
                        """,
                        (job.id,),
                    ).fetchone()["n"]
                    if live:
                        continue
                    cur = conn.execute(
                        """
                        UPDATE jobs SET
                            status = 'archived',
                            updated_at = ?,
                            progress_msg = COALESCE(progress_msg, 'archived')
                        WHERE id = ? AND status IN ('done', 'failed', 'cancelled')
                        """,
                        (_utc_now(), job.id),
                    )
                    if cur.rowcount:
                        stats["archived"] += 1
                        stats["archived_ids"].append(job.id)

        return stats

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

    def cancel_orphaned_dependents(self) -> list[int]:
        """
        Cancel pending jobs whose dependency is already failed/cancelled
        (safety net for rows created before cascade-on-fail).
        """
        now = _utc_now()
        cancelled: list[int] = []
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT j.id, j.depends_on_job_id, d.status AS dep_status
                FROM jobs j
                JOIN jobs d ON d.id = j.depends_on_job_id
                WHERE j.status = 'pending'
                  AND (
                    d.status IN ('failed', 'cancelled')
                    OR (
                      d.status = 'archived'
                      AND (
                        d.error IS NOT NULL
                        OR COALESCE(d.progress_msg, '') LIKE '%failed%'
                        OR COALESCE(d.progress_msg, '') LIKE '%cancel%'
                      )
                    )
                  )
                """
            ).fetchall()
            for row in rows:
                reason = (
                    f"upstream job #{row['depends_on_job_id']} is {row['dep_status']}"
                )
                cur = conn.execute(
                    """
                    UPDATE jobs SET
                        status = 'cancelled',
                        error = ?,
                        finished_at = ?,
                        updated_at = ?,
                        progress_msg = 'cancelled (upstream failed)'
                    WHERE id = ? AND status = 'pending'
                    """,
                    (reason, now, now, row["id"]),
                )
                if cur.rowcount:
                    cancelled.append(int(row["id"]))
        # one more pass for multi-level chains
        if cancelled:
            more = self.cancel_orphaned_dependents()
            cancelled.extend(more)
        return cancelled

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

            # Parent satisfied if done, or archived after successful completion
            # (archived after fail is handled by orphan cancel, not claim).
            dep_ok = """
                        j.depends_on_job_id IS NULL
                        OR EXISTS (
                          SELECT 1 FROM jobs d
                          WHERE d.id = j.depends_on_job_id
                            AND (
                              d.status = 'done'
                              OR (
                                d.status = 'archived'
                                AND d.error IS NULL
                                AND COALESCE(d.progress_pct, 0) >= 100
                              )
                            )
                        )
            """
            if gate is not None:
                if gate["status"] == "running":
                    # Reload already in progress — do not claim anything else.
                    return None
                # Only claim the reload gate (must be runnable).
                row = conn.execute(
                    f"""
                    SELECT j.* FROM jobs j
                    WHERE j.id = ?
                      AND j.status = 'pending'
                      AND ({dep_ok})
                    """,
                    (gate["id"],),
                ).fetchone()
            else:
                row = conn.execute(
                    f"""
                    SELECT j.* FROM jobs j
                    WHERE j.status = 'pending'
                      AND ({dep_ok})
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

    def refresh_running_etas(self) -> int:
        """
        Recompute and persist eta_sec for all running jobs (call every few seconds).
        Returns number of rows updated.
        """
        now = _utc_now()
        n = 0
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, started_at, progress_pct FROM jobs
                WHERE status = 'running'
                """
            ).fetchall()
            for row in rows:
                pct = float(row["progress_pct"] or 0.0)
                eta = estimate_eta_sec(row["started_at"], pct)
                conn.execute(
                    """
                    UPDATE jobs SET eta_sec = ?, updated_at = ? WHERE id = ?
                    """,
                    (eta, now, row["id"]),
                )
                n += 1
        return n

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

    def mark_failed(
        self,
        job_id: int,
        error: str,
        *,
        max_attempts: int | None = None,
        cancel_dependents: bool = True,
    ) -> Job:
        """
        Record a failed attempt.

        If attempts < max_attempts, re-queue as pending (auto-retry).
        Otherwise mark failed permanently and cancel pending dependents.
        """
        from vdl.config import JOB_MAX_ATTEMPTS

        max_att = max(1, int(max_attempts if max_attempts is not None else JOB_MAX_ATTEMPTS))
        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"job {job_id} not found")
            prev_attempts = int(row["attempts"] or 0) if "attempts" in row.keys() else 0
            attempts = prev_attempts + 1
            err_text = (error or "unknown error")[:2000]

            if attempts < max_att:
                # Auto-retry: back to pending for another claim
                msg = f"retry {attempts}/{max_att - 1}: {err_text[:120]}"
                conn.execute(
                    """
                    UPDATE jobs SET
                        status = 'pending',
                        attempts = ?,
                        error = ?,
                        result_json = NULL,
                        started_at = NULL,
                        finished_at = NULL,
                        progress_msg = ?,
                        progress_pct = COALESCE(progress_pct, 0),
                        eta_sec = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (attempts, err_text, msg, now, job_id),
                )
                out = conn.execute(
                    "SELECT * FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                assert out is not None
                return self._row(out)

            # Permanent failure
            conn.execute(
                """
                UPDATE jobs SET
                    status = 'failed',
                    attempts = ?,
                    error = ?,
                    finished_at = ?,
                    updated_at = ?,
                    progress_msg = COALESCE(progress_msg, 'failed')
                WHERE id = ?
                """,
                (attempts, err_text, now, now, job_id),
            )
            out = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            assert out is not None
            job = self._row(out)

        if cancel_dependents:
            self.cancel_dependents(
                job_id,
                reason=f"upstream job #{job_id} failed: {err_text[:200]}",
            )
        return job

    def cancel_dependents(
        self,
        job_id: int,
        *,
        reason: str = "upstream cancelled",
    ) -> list[int]:
        """
        Cancel all pending (and recursively dependent) jobs that depend on job_id.
        Does not cancel already running jobs (would need cooperative cancel).
        Returns cancelled job ids.
        """
        cancelled: list[int] = []
        queue_ids = [job_id]
        seen: set[int] = {job_id}
        now = _utc_now()
        with self._conn() as conn:
            while queue_ids:
                parent = queue_ids.pop(0)
                rows = conn.execute(
                    """
                    SELECT id, status FROM jobs
                    WHERE depends_on_job_id = ?
                    """,
                    (parent,),
                ).fetchall()
                for row in rows:
                    cid = int(row["id"])
                    if cid in seen:
                        continue
                    seen.add(cid)
                    st = row["status"]
                    if st == "pending":
                        cur = conn.execute(
                            """
                            UPDATE jobs SET
                                status = 'cancelled',
                                error = ?,
                                finished_at = ?,
                                updated_at = ?,
                                progress_msg = 'cancelled (upstream failed)'
                            WHERE id = ? AND status = 'pending'
                            """,
                            (reason[:2000], now, now, cid),
                        )
                        if cur.rowcount:
                            cancelled.append(cid)
                    # Always walk dependents (scene→clip→embed chain)
                    queue_ids.append(cid)
        return cancelled

    def cancel(self, job_id: int, *, cascade: bool = True) -> Optional[Job]:
        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                return None
            if row["status"] not in ("pending", "running"):
                job = self._row(row)
            else:
                conn.execute(
                    """
                    UPDATE jobs SET status = 'cancelled', finished_at = ?, updated_at = ?,
                        progress_msg = 'cancelled'
                    WHERE id = ?
                    """,
                    (now, now, job_id),
                )
                row = conn.execute(
                    "SELECT * FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                assert row is not None
                job = self._row(row)
        if cascade:
            self.cancel_dependents(
                job_id, reason=f"upstream job #{job_id} cancelled"
            )
        return job

    def retry(self, job_id: int) -> Optional[Job]:
        """Manual / housekeep retry: reset attempts and re-queue failed/cancelled/archived."""
        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                return None
            if row["status"] not in ("failed", "cancelled", "archived"):
                return self._row(row)
            conn.execute(
                """
                UPDATE jobs SET
                    status = 'pending',
                    attempts = 0,
                    error = NULL,
                    result_json = NULL,
                    started_at = NULL,
                    finished_at = NULL,
                    progress_msg = 'retry (manual)',
                    progress_pct = 0,
                    eta_sec = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, job_id),
            )
            # Re-open cancelled descendants so index chains can run again
            walk = [job_id]
            seen: set[int] = {job_id}
            while walk:
                parent = walk.pop(0)
                kids = conn.execute(
                    "SELECT id, status FROM jobs WHERE depends_on_job_id = ?",
                    (parent,),
                ).fetchall()
                for kid in kids:
                    kid_id = int(kid["id"])
                    if kid_id in seen:
                        continue
                    seen.add(kid_id)
                    walk.append(kid_id)
                    if kid["status"] == "cancelled":
                        conn.execute(
                            """
                            UPDATE jobs SET
                                status = 'pending',
                                attempts = 0,
                                error = NULL,
                                finished_at = NULL,
                                started_at = NULL,
                                progress_msg = 'retry (manual chain)',
                                progress_pct = 0,
                                eta_sec = NULL,
                                updated_at = ?
                            WHERE id = ? AND status = 'cancelled'
                            """,
                            (now, kid_id),
                        )
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            assert row is not None
            return self._row(row)

    def counts(self, *, include_archived: bool = True) -> dict[str, int]:
        with self._conn() as conn:
            if include_archived:
                rows = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT status, COUNT(*) AS n FROM jobs
                    WHERE status != 'archived'
                    GROUP BY status
                    """
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

"""Local download registry (SQLite) — duplicate prevention & history."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Optional


class SourceType(str, Enum):
    DIRECT = "direct"
    M3U8 = "m3u8"
    FROM_HTML = "from_html"


class Status(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DownloadRecord:
    id: int
    url: str
    canonical_url: str
    source_type: str
    status: str
    file_path: Optional[str]
    title: Optional[str]
    content_type: Optional[str]
    file_size: Optional[int]
    file_hash: Optional[str]
    parent_page_url: Optional[str]
    m3u8_fingerprint: Optional[str]
    meta_json: Optional[str]
    created_at: str
    updated_at: str
    error: Optional[str] = None

    @property
    def meta(self) -> dict[str, Any]:
        if not self.meta_json:
            return {}
        try:
            return json.loads(self.meta_json)
        except json.JSONDecodeError:
            return {}


class Registry:
    """SQLite-backed registry of downloads."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
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
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    file_path TEXT,
                    title TEXT,
                    content_type TEXT,
                    file_size INTEGER,
                    file_hash TEXT,
                    parent_page_url TEXT,
                    m3u8_fingerprint TEXT,
                    meta_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_downloads_url
                    ON downloads(url);
                CREATE INDEX IF NOT EXISTS idx_downloads_canonical
                    ON downloads(canonical_url);
                CREATE INDEX IF NOT EXISTS idx_downloads_fingerprint
                    ON downloads(m3u8_fingerprint);
                CREATE INDEX IF NOT EXISTS idx_downloads_hash
                    ON downloads(file_hash);
                CREATE INDEX IF NOT EXISTS idx_downloads_status
                    ON downloads(status);
                """
            )

    def _row_to_record(self, row: sqlite3.Row) -> DownloadRecord:
        return DownloadRecord(
            id=row["id"],
            url=row["url"],
            canonical_url=row["canonical_url"],
            source_type=row["source_type"],
            status=row["status"],
            file_path=row["file_path"],
            title=row["title"],
            content_type=row["content_type"],
            file_size=row["file_size"],
            file_hash=row["file_hash"],
            parent_page_url=row["parent_page_url"],
            m3u8_fingerprint=row["m3u8_fingerprint"],
            meta_json=row["meta_json"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            error=row["error"],
        )

    def find_by_url(self, url: str, completed_only: bool = True) -> Optional[DownloadRecord]:
        """Find a download matching exact or canonical URL."""
        with self._conn() as conn:
            # Parentheses required: AND binds tighter than OR in SQL.
            if completed_only:
                sql = """
                    SELECT * FROM downloads
                    WHERE (url = ? OR canonical_url = ?)
                      AND status = 'completed'
                    ORDER BY id DESC LIMIT 1
                """
            else:
                sql = """
                    SELECT * FROM downloads
                    WHERE url = ? OR canonical_url = ?
                    ORDER BY id DESC LIMIT 1
                """
            row = conn.execute(sql, (url, url)).fetchone()
            return self._row_to_record(row) if row else None

    def find_by_fingerprint(self, fingerprint: str) -> Optional[DownloadRecord]:
        """Find completed m3u8 download by playlist fingerprint."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM downloads
                WHERE m3u8_fingerprint = ? AND status = 'completed'
                ORDER BY id DESC LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
            return self._row_to_record(row) if row else None

    def find_by_hash(self, file_hash: str) -> Optional[DownloadRecord]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM downloads
                WHERE file_hash = ? AND status = 'completed'
                ORDER BY id DESC LIMIT 1
                """,
                (file_hash,),
            ).fetchone()
            return self._row_to_record(row) if row else None

    def is_downloaded(
        self,
        url: str,
        *,
        fingerprint: str | None = None,
    ) -> Optional[DownloadRecord]:
        """
        Check if URL (or m3u8 fingerprint) is already completed.
        Returns the existing record if found.
        """
        if fingerprint:
            rec = self.find_by_fingerprint(fingerprint)
            if rec:
                return rec
        return self.find_by_url(url, completed_only=True)

    def create(
        self,
        url: str,
        *,
        canonical_url: str | None = None,
        source_type: SourceType | str,
        title: str | None = None,
        content_type: str | None = None,
        parent_page_url: str | None = None,
        m3u8_fingerprint: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> DownloadRecord:
        now = _utc_now()
        st = source_type.value if isinstance(source_type, SourceType) else source_type
        can = canonical_url or url
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO downloads (
                    url, canonical_url, source_type, status, title, content_type,
                    parent_page_url, m3u8_fingerprint, meta_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    url,
                    can,
                    st,
                    title,
                    content_type,
                    parent_page_url,
                    m3u8_fingerprint,
                    meta_json,
                    now,
                    now,
                ),
            )
            row_id = cur.lastrowid
            row = conn.execute("SELECT * FROM downloads WHERE id = ?", (row_id,)).fetchone()
            assert row is not None
            return self._row_to_record(row)

    def mark_completed(
        self,
        record_id: int,
        *,
        file_path: str,
        file_size: int | None = None,
        file_hash: str | None = None,
        content_type: str | None = None,
        title: str | None = None,
        m3u8_fingerprint: str | None = None,
    ) -> DownloadRecord:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE downloads SET
                    status = 'completed',
                    file_path = ?,
                    file_size = COALESCE(?, file_size),
                    file_hash = COALESCE(?, file_hash),
                    content_type = COALESCE(?, content_type),
                    title = COALESCE(?, title),
                    m3u8_fingerprint = COALESCE(?, m3u8_fingerprint),
                    error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    file_path,
                    file_size,
                    file_hash,
                    content_type,
                    title,
                    m3u8_fingerprint,
                    now,
                    record_id,
                ),
            )
            row = conn.execute("SELECT * FROM downloads WHERE id = ?", (record_id,)).fetchone()
            assert row is not None
            return self._row_to_record(row)

    def mark_failed(self, record_id: int, error: str) -> DownloadRecord:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE downloads SET status = 'failed', error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error, now, record_id),
            )
            row = conn.execute("SELECT * FROM downloads WHERE id = ?", (record_id,)).fetchone()
            assert row is not None
            return self._row_to_record(row)

    def mark_skipped(self, record_id: int, reason: str = "duplicate") -> DownloadRecord:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE downloads SET status = 'skipped', error = ?, updated_at = ?
                WHERE id = ?
                """,
                (reason, now, record_id),
            )
            row = conn.execute("SELECT * FROM downloads WHERE id = ?", (record_id,)).fetchone()
            assert row is not None
            return self._row_to_record(row)

    def list_all(
        self,
        *,
        status: str | None = None,
        limit: int | None = 50,
    ) -> list[DownloadRecord]:
        with self._conn() as conn:
            clauses: list[str] = []
            args: list[Any] = []
            if status:
                clauses.append("status = ?")
                args.append(status)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            sql = f"SELECT * FROM downloads{where} ORDER BY id DESC"
            if limit is not None:
                sql += " LIMIT ?"
                args.append(int(limit))
            rows = conn.execute(sql, args).fetchall()
            return [self._row_to_record(r) for r in rows]

    def find_all_by_hash(
        self, file_hash: str, *, completed_only: bool = True
    ) -> list[DownloadRecord]:
        with self._conn() as conn:
            if completed_only:
                rows = conn.execute(
                    """
                    SELECT * FROM downloads
                    WHERE file_hash = ? AND status = 'completed'
                    ORDER BY id ASC
                    """,
                    (file_hash,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM downloads
                    WHERE file_hash = ?
                    ORDER BY id ASC
                    """,
                    (file_hash,),
                ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def update_artifact(
        self,
        record_id: int,
        *,
        file_path: str | None = None,
        file_size: int | None = None,
        file_hash: str | None = None,
        content_type: str | None = None,
        title: str | None = None,
        meta_update: dict[str, Any] | None = None,
    ) -> DownloadRecord:
        rec = self.get(record_id)
        if rec is None:
            raise KeyError(f"download {record_id} not found")
        now = _utc_now()
        path = file_path if file_path is not None else rec.file_path
        size = file_size if file_size is not None else rec.file_size
        digest = file_hash if file_hash is not None else rec.file_hash
        ctype = content_type if content_type is not None else rec.content_type
        title_v = title if title is not None else rec.title
        meta = rec.meta
        if meta_update:
            meta = {**meta, **meta_update}
        meta_json = json.dumps(meta, ensure_ascii=False)
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE downloads SET
                    file_path = ?,
                    file_size = ?,
                    file_hash = ?,
                    content_type = ?,
                    title = ?,
                    meta_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (path, size, digest, ctype, title_v, meta_json, now, record_id),
            )
            row = conn.execute(
                "SELECT * FROM downloads WHERE id = ?", (record_id,)
            ).fetchone()
            assert row is not None
            return self._row_to_record(row)

    def get(self, record_id: int) -> Optional[DownloadRecord]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM downloads WHERE id = ?", (record_id,)
            ).fetchone()
            return self._row_to_record(row) if row else None

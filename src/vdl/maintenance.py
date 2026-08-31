"""Library maintenance: MPEG-TS remux and content-hash dedup."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from vdl.config import Config
from vdl.queue import JobQueue
from vdl.registry import DownloadRecord, Registry
from vdl.util import sha256_file, unique_path

ProgressCb = Callable[[float | None, str | None, int | None, int | None], None]

TS_SUFFIXES = {".ts"}
SOURCE_HASH_META = "source_hash"


def is_ts_path(path: Path | str | None) -> bool:
    if not path:
        return False
    return Path(path).suffix.lower() in TS_SUFFIXES


def source_hash_of(rec: DownloadRecord) -> str | None:
    """Original .ts hash when remuxed; absent means compare by current file_hash only."""
    h = rec.meta.get(SOURCE_HASH_META) if rec.meta else None
    if isinstance(h, str) and h.strip():
        return h.strip()
    return None


def _progress(
    cb: ProgressCb | None,
    *,
    pct: float | None = None,
    message: str | None = None,
    current: int | None = None,
    total: int | None = None,
) -> None:
    if cb:
        cb(pct, message, current, total)


def _under_dir(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _drop_index(config: Config, queue: JobQueue, download_id: int) -> None:
    had = queue.list_embeddings(download_id)
    queue.replace_scenes(download_id, [])
    queue.clear_embeddings(download_id)
    if had:
        try:
            from vdl.vectorstore import VectorStore

            VectorStore(config.chroma_dir).delete_for_download(download_id)
        except Exception:
            pass
    kdir = config.keyframes_dir / f"dl_{download_id}"
    if kdir.is_dir():
        shutil.rmtree(kdir, ignore_errors=True)


def remux_ts_to_mp4(src: Path, *, ffmpeg_bin: str = "ffmpeg") -> Path:
    """Remux MPEG-TS to MP4 (stream copy). Deletes src on success."""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)
    if not is_ts_path(src):
        raise ValueError(f"not a .ts file: {src}")
    if not shutil.which(ffmpeg_bin):
        raise RuntimeError(f"ffmpeg が見つかりません ({ffmpeg_bin})")

    dest = src.with_suffix(".mp4")
    if dest.exists() and dest.stat().st_size == 0:
        dest.unlink()
    if dest.exists():
        dest = unique_path(dest.parent, dest.name)
    tmp = dest.with_name(f"{dest.stem}.{uuid.uuid4().hex[:8]}.remux-tmp.mp4")
    tmp.unlink(missing_ok=True)

    def _run(extra: list[str]) -> None:
        cmd = [
            ffmpeg_bin,
            "-y",
            "-nostats",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-c",
            "copy",
            *extra,
            "-movflags",
            "+faststart",
            str(tmp),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
            raise subprocess.CalledProcessError(proc.returncode, cmd, stderr=err)

    try:
        try:
            _run(["-bsf:a", "aac_adtstoasc"])
        except subprocess.CalledProcessError:
            _run([])
        if not tmp.is_file() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("remux produced an empty file")
        tmp.replace(dest)
        src.unlink(missing_ok=True)
        return dest
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def list_ts_records(reg: Registry) -> list[DownloadRecord]:
    out: list[DownloadRecord] = []
    for rec in reg.list_all(status="completed", limit=None):
        if is_ts_path(rec.file_path):
            out.append(rec)
    return out


def run_ts_to_mp4(
    config: Config,
    *,
    download_id: int | None = None,
    on_progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """Convert completed registry .ts artifacts to .mp4 and update paths."""
    reg = Registry(config.db_path)
    if download_id is not None:
        rec = reg.get(download_id)
        targets = [rec] if rec and rec.status == "completed" else []
    else:
        targets = list_ts_records(reg)

    converted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    n = len(targets)
    for i, rec in enumerate(targets):
        _progress(
            on_progress,
            pct=100.0 * i / n if n else 100.0,
            message=f"ts_to_mp4 {i + 1}/{n}",
            current=i,
            total=n,
        )
        if rec is None or not rec.file_path:
            skipped.append({"download_id": getattr(rec, "id", None), "reason": "no file"})
            continue
        src = Path(rec.file_path)
        if not is_ts_path(src):
            skipped.append({"download_id": rec.id, "reason": "not ts"})
            continue
        if not src.is_file():
            errors.append({"download_id": rec.id, "error": f"missing on disk: {src}"})
            continue
        try:
            src_hash = rec.file_hash or sha256_file(src)
            dest = remux_ts_to_mp4(src)
            dest_hash = sha256_file(dest)
            title = rec.title
            if title and title.lower().endswith(".ts"):
                title = title[: -len(".ts")] + ".mp4"
            meta_update: dict[str, Any] = {"remuxed_from": str(src)}
            if not source_hash_of(rec):
                meta_update[SOURCE_HASH_META] = src_hash
            reg.update_artifact(
                rec.id,
                file_path=str(dest),
                file_size=dest.stat().st_size,
                file_hash=dest_hash,
                content_type="video/mp4",
                title=title,
                meta_update=meta_update,
            )
            converted.append(
                {
                    "download_id": rec.id,
                    "from": str(src),
                    "to": str(dest),
                    "bytes": dest.stat().st_size,
                    "source_hash": src_hash,
                    "file_hash": dest_hash,
                }
            )
        except Exception as e:
            errors.append({"download_id": rec.id, "path": str(src), "error": str(e)})

    _progress(
        on_progress,
        pct=100.0,
        message=f"ts_to_mp4 converted={len(converted)} skip={len(skipped)} err={len(errors)}",
        current=n,
        total=n,
    )
    if errors and not converted and any(
        t and t.file_path and Path(t.file_path).is_file() for t in targets
    ):
        msgs = "; ".join(e.get("error", "") for e in errors[:3])
        raise RuntimeError(msgs or "ts_to_mp4 failed")
    return {
        "converted": converted,
        "skipped": skipped,
        "errors": errors,
        "considered": n,
    }


def _pick_keeper(
    recs: list[DownloadRecord], scene_counts: dict[int, int]
) -> DownloadRecord:
    def key(r: DownloadRecord) -> tuple[int, int, int]:
        exists = 1 if r.file_path and Path(r.file_path).is_file() else 0
        return (-exists, -int(scene_counts.get(r.id, 0)), r.id)

    return sorted(recs, key=key)[0]


def dedup_groups(recs: list[DownloadRecord]) -> list[list[DownloadRecord]]:
    """
    Duplicate clusters.

    - Always join records that share current file_hash (post-conversion / on-disk).
    - Optionally join records that *all* have the same source_hash (original .ts).
      A source_hash present on only one side is ignored (compare by file_hash).
    - Records with no hash at all join by file_path.
    """
    if not recs:
        return []
    parent: dict[int, int] = {r.id: r.id for r in recs}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_file: dict[str, list[int]] = defaultdict(list)
    by_source: dict[str, list[int]] = defaultdict(list)
    by_path: dict[str, list[int]] = defaultdict(list)
    for rec in recs:
        if rec.file_hash:
            by_file[rec.file_hash].append(rec.id)
        src = source_hash_of(rec)
        if src:
            by_source[src].append(rec.id)
        elif not rec.file_hash and rec.file_path:
            by_path[rec.file_path].append(rec.id)

    for ids in by_file.values():
        for other in ids[1:]:
            union(ids[0], other)
    # Optional .ts-origin compare: only when both sides recorded source_hash.
    for ids in by_source.values():
        if len(ids) < 2:
            continue
        for other in ids[1:]:
            union(ids[0], other)
    for ids in by_path.values():
        for other in ids[1:]:
            union(ids[0], other)

    buckets: dict[int, list[DownloadRecord]] = defaultdict(list)
    for rec in recs:
        buckets[find(rec.id)].append(rec)
    return [g for g in buckets.values() if len(g) > 1]


def _fill_missing_hashes(reg: Registry, recs: list[DownloadRecord]) -> int:
    n = 0
    for rec in recs:
        if rec.file_hash or not rec.file_path:
            continue
        path = Path(rec.file_path)
        if not path.is_file():
            continue
        digest = sha256_file(path)
        reg.update_artifact(rec.id, file_hash=digest, file_size=path.stat().st_size)
        rec.file_hash = digest
        rec.file_size = path.stat().st_size
        n += 1
    return n


def _collapse_group(
    config: Config,
    reg: Registry,
    queue: JobQueue,
    recs: list[DownloadRecord],
    scene_counts: dict[int, int],
) -> dict[str, Any]:
    keeper = _pick_keeper(recs, scene_counts)
    extras = [r for r in recs if r.id != keeper.id]
    keeper_path = Path(keeper.file_path) if keeper.file_path else None
    unlinked: list[str] = []
    retargeted: list[int] = []
    cancelled: list[int] = []

    referenced = {str(Path(keeper_path).resolve())} if keeper_path and keeper_path.exists() else set()
    # Don't unlink a path still needed by the keeper.

    for extra in extras:
        cancelled.extend(
            queue.cancel_for_download(
                extra.id, reason=f"duplicate of download_id={keeper.id}"
            )
        )
        _drop_index(config, queue, extra.id)
        extra_path = Path(extra.file_path) if extra.file_path else None
        same_file = False
        if extra_path and keeper_path:
            try:
                same_file = extra_path.resolve() == keeper_path.resolve()
            except Exception:
                same_file = str(extra_path) == str(keeper_path)
        if extra_path and extra_path.is_file() and not same_file:
            if _under_dir(extra_path, config.download_dir):
                try:
                    resolved = str(extra_path.resolve())
                except Exception:
                    resolved = str(extra_path)
                if resolved not in referenced:
                    extra_path.unlink(missing_ok=True)
                    unlinked.append(str(extra_path))
        extra_src = source_hash_of(extra)
        if extra_src and not source_hash_of(keeper):
            keeper = reg.update_artifact(
                keeper.id, meta_update={SOURCE_HASH_META: extra_src}
            )
        if keeper_path:
            extra_meta: dict[str, Any] = {"dedup_of": keeper.id}
            if extra_src:
                extra_meta[SOURCE_HASH_META] = extra_src
            reg.update_artifact(
                extra.id,
                file_path=str(keeper_path),
                file_size=keeper.file_size,
                file_hash=keeper.file_hash,
                meta_update=extra_meta,
            )
            retargeted.append(extra.id)
    return {
        "keeper_id": keeper.id,
        "extra_ids": [e.id for e in extras],
        "retargeted": retargeted,
        "unlinked": unlinked,
        "cancelled_jobs": cancelled,
    }


def run_dedup(
    config: Config,
    *,
    on_progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """
    Collapse completed downloads that share current file_hash, or that share
    an original .ts source_hash when both records recorded one.

    Extra records stay completed so URL/fingerprint skip still works, but they
    point at the keeper file. Duplicate copies on disk are removed. Leftover
    .ts originals whose hash matches a known source_hash are deleted.
    """
    reg = Registry(config.db_path)
    queue = JobQueue(config.db_path)
    recs = [
        r
        for r in reg.list_all(status="completed", limit=None)
        if r.file_path
    ]
    _progress(on_progress, pct=2.0, message=f"hashing {len(recs)} records")
    hashed = _fill_missing_hashes(reg, recs)
    # Reload after hash fill
    recs = [
        r
        for r in reg.list_all(status="completed", limit=None)
        if r.file_path
    ]
    scene_counts = queue.scene_counts()

    dup_groups = dedup_groups(recs)
    collapsed: list[dict[str, Any]] = []
    n = len(dup_groups)
    for i, group in enumerate(dup_groups):
        _progress(
            on_progress,
            pct=10.0 + 80.0 * i / n if n else 90.0,
            message=f"dedup group {i + 1}/{n}",
            current=i,
            total=n,
        )
        collapsed.append(_collapse_group(config, reg, queue, group, scene_counts))

    # Unregisterable extra copies in the download dir (same hash, not referenced).
    referenced: set[str] = set()
    hash_by_size: dict[int, set[str]] = defaultdict(set)
    known_file_hashes: set[str] = set()
    known_source_hashes: set[str] = set()
    recs = [
        r
        for r in reg.list_all(status="completed", limit=None)
        if r.file_path
    ]
    for rec in recs:
        try:
            referenced.add(str(Path(rec.file_path).resolve()))
        except Exception:
            referenced.add(rec.file_path)
        if rec.file_hash:
            known_file_hashes.add(rec.file_hash)
            if rec.file_size:
                hash_by_size[int(rec.file_size)].add(rec.file_hash)
        src = source_hash_of(rec)
        if src:
            known_source_hashes.add(src)

    orphan_unlinked: list[str] = []
    download_dir = config.download_dir
    if download_dir.is_dir():
        files = [p for p in download_dir.iterdir() if p.is_file()]
        for path in files:
            try:
                resolved = str(path.resolve())
            except Exception:
                resolved = str(path)
            if resolved in referenced:
                continue
            size = path.stat().st_size
            leftover_ts = is_ts_path(path)
            if size not in hash_by_size and not leftover_ts:
                continue
            digest = sha256_file(path)
            drop = digest in known_file_hashes
            # Converted records keep source_hash; the original .ts bytes are not needed.
            if leftover_ts and digest in known_source_hashes:
                drop = True
            if drop:
                path.unlink(missing_ok=True)
                orphan_unlinked.append(str(path))

    _progress(
        on_progress,
        pct=100.0,
        message=(
            f"dedup groups={len(collapsed)} unlinked_orphans={len(orphan_unlinked)}"
        ),
        current=n,
        total=n,
    )
    return {
        "hashed": hashed,
        "groups": collapsed,
        "orphan_unlinked": orphan_unlinked,
        "records": len(recs),
    }

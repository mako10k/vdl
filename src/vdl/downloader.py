"""Direct file download with progress."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import httpx
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from vdl.util import filename_from_url, safe_filename, sha256_file, unique_path


ProgressCallback = Callable[[int, int | None], None]


def download_file(
    client: httpx.Client,
    url: str,
    dest_dir: Path,
    *,
    filename: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 120.0,
    show_progress: bool = True,
    on_progress: ProgressCallback | None = None,
) -> tuple[Path, int, str]:
    """
    Stream-download a URL to dest_dir.
    Returns (path, size_bytes, sha256).
    on_progress(written_bytes, total_or_None) is called periodically.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    req_headers = {"User-Agent": "Mozilla/5.0 (compatible; vdl/0.1)"}
    if headers:
        req_headers.update(headers)

    with client.stream(
        "GET", url, headers=req_headers, follow_redirects=True, timeout=timeout
    ) as resp:
        resp.raise_for_status()

        # Resolve filename
        name = filename
        if not name:
            cd = resp.headers.get("content-disposition")
            if cd:
                from vdl.classifier import filename_from_content_disposition

                name = filename_from_content_disposition(cd)
        if not name:
            name = filename_from_url(str(resp.url), default="download.bin")
        name = safe_filename(name)
        # ensure extension if server gave video content-type
        if "." not in name:
            ct = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            ext_map = {
                "video/mp4": ".mp4",
                "video/webm": ".webm",
                "video/x-matroska": ".mkv",
                "video/quicktime": ".mov",
                "video/x-msvideo": ".avi",
                "video/mp2t": ".ts",
            }
            name = name + ext_map.get(ct, ".bin")

        out_path = unique_path(dest_dir, name)
        from vdl.execution import track_partial
        track_partial(out_path)
        total = resp.headers.get("content-length")
        total_n: int | None = int(total) if total and total.isdigit() else None

        written = 0
        last_report = 0

        def _report() -> None:
            nonlocal last_report
            if on_progress is None:
                return
            # throttle: every 256KB or at end
            if written - last_report >= 256 * 1024 or (total_n and written >= total_n):
                on_progress(written, total_n)
                last_report = written

        if show_progress:
            with Progress(
                TextColumn("[bold blue]{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
            ) as progress:
                task = progress.add_task(out_path.name, total=total_n)
                with open(out_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        written += len(chunk)
                        progress.update(task, completed=written)
                        _report()
        else:
            with open(out_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    written += len(chunk)
                    _report()
        if on_progress:
            on_progress(written, total_n)

    digest = sha256_file(out_path)
    return out_path, written, digest

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
) -> tuple[Path, int, str]:
    """
    Stream-download a URL to dest_dir.
    Returns (path, size_bytes, sha256).
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
        total = resp.headers.get("content-length")
        total_n: int | None = int(total) if total and total.isdigit() else None

        written = 0
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
        else:
            with open(out_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    written += len(chunk)

    digest = sha256_file(out_path)
    return out_path, written, digest

"""m3u8 / HLS index handling: fingerprint, quality pick, download via ffmpeg."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin
from urllib.request import pathname2url

import httpx
import m3u8

from vdl.util import is_mpd_url, safe_filename, sha256_text, unique_path

# out_time_sec, duration_hint_or_None
FFmpegProgressCb = Callable[[float, Optional[float]], None]


class UnsupportedIndexError(ValueError):
    """Raised for index formats outside MVP scope (e.g. MPD / encrypted HLS policy)."""


@dataclass
class PlaylistVariant:
    bandwidth: int | None
    resolution: str | None
    uri: str
    absolute_uri: str
    codecs: str | None = None

    def label(self) -> str:
        parts = []
        if self.resolution:
            parts.append(self.resolution)
        if self.bandwidth:
            parts.append(f"{self.bandwidth // 1000}kbps")
        if self.codecs:
            parts.append(self.codecs)
        return " / ".join(parts) if parts else self.uri


@dataclass
class M3U8Info:
    url: str
    is_master: bool
    variants: list[PlaylistVariant]
    media_playlist_url: str  # chosen or only media playlist
    fingerprint: str
    raw_text: str
    segment_count: int | None = None
    duration_approx: float | None = None
    is_encrypted: bool = False
    is_endlist: bool = True


def ensure_hls_supported(url: str) -> None:
    """Reject MPD / DASH per design D3 (MVP non-goal)."""
    if is_mpd_url(url):
        raise UnsupportedIndexError(
            "MPEG-DASH (.mpd) は MVP 対象外です。HLS (.m3u8) のみ対応しています。"
        )


def _path_to_file_url(path: Path) -> str:
    return "file://" + pathname2url(str(path.resolve()))


def _fetch_text(
    client: httpx.Client | None,
    url: str,
    headers: dict[str, str] | None = None,
) -> str:
    if url.startswith("file://"):
        from urllib.parse import unquote, urlparse

        path = Path(unquote(urlparse(url).path))
        return path.read_text(encoding="utf-8", errors="replace")
    if client is None:
        raise ValueError("remote playlist requires httpx client")
    h = {"User-Agent": "Mozilla/5.0 (compatible; vdl/0.1)", "Accept": "*/*"}
    if headers:
        h.update(headers)
    resp = client.get(url, headers=h, follow_redirects=True, timeout=60.0)
    resp.raise_for_status()
    return resp.text


def _playlist_has_keys(pl: m3u8.M3U8) -> bool:
    if getattr(pl, "keys", None):
        for k in pl.keys:
            if k is not None and getattr(k, "method", None) and k.method and k.method.upper() != "NONE":
                return True
    if pl.segments:
        for seg in pl.segments:
            key = getattr(seg, "key", None)
            if key is not None and getattr(key, "method", None) and key.method and key.method.upper() != "NONE":
                return True
    return False


def fingerprint_playlist(text: str, media_url: str) -> str:
    """
    Stable fingerprint for an m3u8 stream.
    Prefer segment URI list when available; else hash media playlist URL + body.
    """
    try:
        pl = m3u8.loads(text, uri=media_url)
        if pl.segments:
            # Use last N segment URIs (live windows change; VOD is stable)
            uris = [seg.absolute_uri or seg.uri for seg in pl.segments]
            # For VOD full list; for live, use media sequence + count + first/last
            if pl.is_endlist or len(uris) <= 50:
                payload = "\n".join(uris)
            else:
                payload = json.dumps(
                    {
                        "media_sequence": pl.media_sequence,
                        "count": len(uris),
                        "first": uris[0],
                        "last": uris[-1],
                    },
                    sort_keys=True,
                )
            return sha256_text(payload)
    except Exception:
        pass
    return sha256_text(media_url + "\n" + text)


def parse_m3u8(
    client: httpx.Client | None,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    quality: str = "best",
    local_path: Path | None = None,
) -> M3U8Info:
    """
    Load playlist. If master, select variant by quality: best | worst | index (0-based) | resolution substring.
    Accepts remote HTTP(S) or local_path / file:// URL.
    """
    ensure_hls_supported(url)
    if local_path is not None:
        ensure_hls_supported(str(local_path))
        url = _path_to_file_url(local_path)

    text = _fetch_text(client, url, headers)
    pl = m3u8.loads(text, uri=url)
    variants: list[PlaylistVariant] = []

    if pl.is_variant and pl.playlists:
        for p in pl.playlists:
            stream = p.stream_info
            bw = stream.bandwidth if stream else None
            res = None
            if stream and stream.resolution:
                res = f"{stream.resolution[0]}x{stream.resolution[1]}"
            abs_uri = p.absolute_uri or urljoin(url, p.uri)
            variants.append(
                PlaylistVariant(
                    bandwidth=bw,
                    resolution=res,
                    uri=p.uri,
                    absolute_uri=abs_uri,
                    codecs=stream.codecs if stream else None,
                )
            )
        chosen = _select_variant(variants, quality)
        media_url = chosen.absolute_uri
        media_text = _fetch_text(client, media_url, headers)
        media_pl = m3u8.loads(media_text, uri=media_url)
        fp = fingerprint_playlist(media_text, media_url)
        seg_count = len(media_pl.segments) if media_pl.segments else None
        duration = None
        if media_pl.segments:
            duration = sum(s.duration or 0 for s in media_pl.segments)
        encrypted = _playlist_has_keys(media_pl)
        return M3U8Info(
            url=url,
            is_master=True,
            variants=variants,
            media_playlist_url=media_url,
            fingerprint=fp,
            raw_text=media_text,
            segment_count=seg_count,
            duration_approx=duration,
            is_encrypted=encrypted,
            is_endlist=bool(media_pl.is_endlist),
        )

    # Media playlist directly
    fp = fingerprint_playlist(text, url)
    seg_count = len(pl.segments) if pl.segments else None
    duration = None
    if pl.segments:
        duration = sum(s.duration or 0 for s in pl.segments)
    encrypted = _playlist_has_keys(pl)
    return M3U8Info(
        url=url,
        is_master=False,
        variants=[],
        media_playlist_url=url,
        fingerprint=fp,
        raw_text=text,
        segment_count=seg_count,
        duration_approx=duration,
        is_encrypted=encrypted,
        is_endlist=bool(pl.is_endlist),
    )


def _select_variant(variants: list[PlaylistVariant], quality: str) -> PlaylistVariant:
    if not variants:
        raise ValueError("master playlist has no variants")
    q = quality.strip().lower()
    # sort by bandwidth (None last)
    ranked = sorted(
        variants,
        key=lambda v: v.bandwidth if v.bandwidth is not None else -1,
    )
    if q in ("best", "highest", "max"):
        return ranked[-1]
    if q in ("worst", "lowest", "min"):
        return ranked[0]
    # 0-based index only when in range (so "720" is resolution, not index 720)
    if q.isdigit():
        idx = int(q)
        if 0 <= idx < len(variants):
            return variants[idx]
        # else fall through — treat as resolution token (720, 1080, …)
    # match resolution substring e.g. 720, 1080p, 1280x720
    for v in reversed(ranked):
        label = ((v.resolution or "") + " " + v.uri).lower()
        q_norm = q.replace("p", "")
        if q in label or q_norm in label.replace("p", ""):
            return v
        if v.resolution and (q in v.resolution or q_norm in v.resolution):
            return v
    if q.isdigit():
        raise ValueError(
            f"quality {q!r} matched neither variant index 0..{len(variants)-1} "
            f"nor a resolution label"
        )
    # fallback best
    return ranked[-1]


def download_m3u8_ffmpeg(
    media_playlist_url: str,
    dest_dir: Path,
    *,
    filename: str | None = None,
    headers: dict[str, str] | None = None,
    ffmpeg_bin: str = "ffmpeg",
    insecure: bool = False,
    duration_hint: float | None = None,
    on_progress: FFmpegProgressCb | None = None,
) -> tuple[Path, int]:
    """
    Download HLS via ffmpeg (-c copy). Returns (path, size).

    on_progress(out_time_sec, duration_hint_or_None) is called as ffmpeg reports
    progress (about every second with -progress).
    """
    if not shutil.which(ffmpeg_bin):
        raise RuntimeError(
            f"ffmpeg が見つかりません ({ffmpeg_bin})。インストールしてください。"
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    name = filename or "stream.mp4"
    if not name.lower().endswith((".mp4", ".mkv", ".ts", ".m4v")):
        name = name + ".mp4"
    name = safe_filename(name)
    out_path = unique_path(dest_dir, name)

    from vdl.execution import track_partial
    track_partial(out_path)

    # Build ffmpeg headers string (CRLF separated)
    header_args: list[str] = []
    if headers:
        lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        header_args = ["-headers", lines]

    def _run(with_bsf: bool) -> None:
        cmd = [
            ffmpeg_bin,
            "-y",
            "-nostats",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            *header_args,
            "-tls_verify",
            "0" if insecure else "1",
            "-i",
            media_playlist_url,
            "-c",
            "copy",
        ]
        if with_bsf:
            cmd.extend(["-bsf:a", "aac_adtstoasc"])
        cmd.append(str(out_path))

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        last_report = 0.0
        out_time = 0.0
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                if key == "out_time_ms":
                    try:
                        out_time = max(0.0, int(val) / 1_000_000.0)
                    except ValueError:
                        continue
                elif key == "out_time":
                    # HH:MM:SS.micro
                    try:
                        parts = val.split(":")
                        if len(parts) == 3:
                            out_time = (
                                int(parts[0]) * 3600
                                + int(parts[1]) * 60
                                + float(parts[2])
                            )
                    except ValueError:
                        continue
                elif key == "progress" and val == "end":
                    break
                else:
                    continue
                # throttle callbacks ~1/s
                if on_progress and (out_time - last_report >= 0.9 or out_time == 0):
                    on_progress(out_time, duration_hint)
                    last_report = out_time
            rc = proc.wait()
            if rc != 0:
                err = (proc.stderr.read() if proc.stderr else "") or f"exit {rc}"
                raise subprocess.CalledProcessError(rc, cmd, stderr=err)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    try:
        _run(with_bsf=True)
    except subprocess.CalledProcessError:
        if out_path.exists():
            out_path.unlink(missing_ok=True)
        _run(with_bsf=False)

    if on_progress and duration_hint:
        on_progress(duration_hint, duration_hint)

    size = out_path.stat().st_size if out_path.exists() else 0
    if size == 0:
        raise RuntimeError("ffmpeg の出力が空です")
    return out_path, size

"""Scene detection via ffmpeg scene filter (fallback: fixed interval)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from vdl.config import SCENE_THRESHOLD


def _probe_duration(video_path: Path) -> float:
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return float((r.stdout or "").strip())
    except ValueError:
        return 0.0


def detect_scenes(
    video_path: Path,
    *,
    threshold: float = SCENE_THRESHOLD,
    min_scene_sec: float = 1.0,
    fallback_interval: float = 5.0,
) -> list[dict[str, Any]]:
    """
    Return list of {start_sec, end_sec}.
    Uses ffmpeg showinfo on scene filter; falls back to fixed intervals.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(str(video_path))

    duration = _probe_duration(video_path)
    cuts: list[float] = [0.0]

    # ffmpeg scene scores via showinfo (pts_time)
    cmd = [
        "ffmpeg",
        "-i",
        str(video_path),
        "-filter:v",
        f"select='gt(scene,{threshold})',showinfo",
        "-f",
        "null",
        "-",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    # showinfo logs to stderr
    for m in re.finditer(r"pts_time:([0-9.]+)", r.stderr or ""):
        t = float(m.group(1))
        if not cuts or t - cuts[-1] >= min_scene_sec * 0.5:
            cuts.append(t)

    if len(cuts) <= 1:
        # fallback fixed interval
        if duration <= 0:
            duration = fallback_interval
        cuts = [0.0]
        t = fallback_interval
        while t < duration:
            cuts.append(t)
            t += fallback_interval

    if duration <= 0:
        duration = cuts[-1] + fallback_interval

    scenes: list[dict[str, Any]] = []
    for i, start in enumerate(cuts):
        end = cuts[i + 1] if i + 1 < len(cuts) else duration
        if end - start < min_scene_sec * 0.25 and i + 1 < len(cuts):
            continue
        scenes.append(
            {
                "start_sec": float(start),
                "end_sec": float(max(end, start + 0.1)),
                "meta": {"threshold": threshold},
            }
        )
    if not scenes:
        scenes = [{"start_sec": 0.0, "end_sec": max(duration, 1.0), "meta": {"fallback": True}}]
    return scenes


def extract_frame(video_path: Path, time_sec: float, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not out_path.exists():
        from vdl.execution import track_partial
        track_partial(out_path)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{time_sec:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(out_path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"frame extract failed at {time_sec}s")
    return out_path

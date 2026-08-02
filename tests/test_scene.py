from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vdl.index.scene import detect_scenes, extract_frame


@pytest.fixture
def short_video(tmp_path: Path) -> Path:
    out = tmp_path / "s.mp4"
    r = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=3:size=160x120:rate=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        capture_output=True,
    )
    if r.returncode != 0:
        pytest.skip("ffmpeg encode failed")
    return out


def test_detect_scenes_fallback(short_video: Path) -> None:
    scenes = detect_scenes(short_video, threshold=0.9, fallback_interval=1.0)
    assert scenes
    assert scenes[0]["start_sec"] == 0.0
    assert scenes[-1]["end_sec"] > 0


def test_extract_frame(short_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "f.jpg"
    extract_frame(short_video, 0.5, out)
    assert out.exists() and out.stat().st_size > 0

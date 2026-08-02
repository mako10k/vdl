from __future__ import annotations

from pathlib import Path

from vdl.util import (
    normalize_url,
    safe_filename,
    sha256_file,
    sha256_text,
    unique_path,
    url_extension,
)


def test_normalize_url_strips_fragment_and_default_port() -> None:
    assert (
        normalize_url("HTTPS://Example.com:443/a/b?x=1#frag")
        == "https://example.com/a/b?x=1"
    )
    assert normalize_url("http://example.com:80/x") == "http://example.com/x"


def test_url_extension() -> None:
    assert url_extension("https://x/a.MP4?token=1") == ".mp4"
    assert url_extension("https://x/noext") == ""


def test_safe_filename() -> None:
    assert "/" not in safe_filename("a/b:c*.mp4")
    assert safe_filename("") == "download"


def test_unique_path(tmp_path: Path) -> None:
    p1 = unique_path(tmp_path, "f.mp4")
    p1.write_text("a")
    p2 = unique_path(tmp_path, "f.mp4")
    assert p2.name == "f_2.mp4"


def test_sha256(tmp_path: Path) -> None:
    f = tmp_path / "x.bin"
    f.write_bytes(b"hello")
    assert sha256_file(f) == sha256_text("hello")

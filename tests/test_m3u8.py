"""m3u8 parse, fingerprint, quality, MPD rejection."""

from __future__ import annotations

from pathlib import Path

import pytest

from vdl.m3u8_dl import (
    UnsupportedIndexError,
    _select_variant,
    ensure_hls_supported,
    fingerprint_playlist,
    parse_m3u8,
    PlaylistVariant,
)
from vdl.util import is_mpd_url


MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-PLAYLIST-TYPE:VOD
#EXTINF:10.0,
seg0.ts
#EXTINF:10.0,
seg1.ts
#EXT-X-ENDLIST
"""

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
high.m3u8
"""


def test_is_mpd_url() -> None:
    assert is_mpd_url("https://x/a.mpd")
    assert is_mpd_url("https://x/manifest.mpd?x=1")
    assert not is_mpd_url("https://x/a.m3u8")


def test_ensure_hls_rejects_mpd() -> None:
    with pytest.raises(UnsupportedIndexError):
        ensure_hls_supported("https://cdn.example.com/manifest.mpd")


def test_fingerprint_stable_for_vod(tmp_path: Path) -> None:
    p = tmp_path / "pl.m3u8"
    p.write_text(MEDIA)
    info = parse_m3u8(None, p.as_uri(), local_path=p)
    info2 = parse_m3u8(None, p.as_uri(), local_path=p)
    assert info.fingerprint == info2.fingerprint
    assert info.segment_count == 2
    assert info.is_endlist
    assert not info.is_encrypted


def test_fingerprint_function_direct() -> None:
    fp1 = fingerprint_playlist(MEDIA, "http://x/pl.m3u8")
    fp2 = fingerprint_playlist(MEDIA, "http://x/pl.m3u8")
    assert fp1 == fp2
    other = MEDIA.replace("seg1.ts", "seg9.ts")
    assert fingerprint_playlist(other, "http://x/pl.m3u8") != fp1


def test_select_variant_best_worst_index() -> None:
    variants = [
        PlaylistVariant(800_000, "640x360", "low.m3u8", "http://x/low.m3u8"),
        PlaylistVariant(2_000_000, "1280x720", "high.m3u8", "http://x/high.m3u8"),
    ]
    assert _select_variant(variants, "best").bandwidth == 2_000_000
    assert _select_variant(variants, "worst").bandwidth == 800_000
    assert _select_variant(variants, "0").uri == "low.m3u8"
    assert _select_variant(variants, "720").resolution == "1280x720"


def test_encrypted_detection(tmp_path: Path) -> None:
    text = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-KEY:METHOD=AES-128,URI="https://x/key"
#EXTINF:1.0,
a.ts
#EXT-X-ENDLIST
"""
    p = tmp_path / "enc.m3u8"
    p.write_text(text)
    info = parse_m3u8(None, str(p), local_path=p)
    assert info.is_encrypted


def test_master_quality_local(tmp_path: Path) -> None:
    (tmp_path / "low.m3u8").write_text(MEDIA)
    high = MEDIA.replace("seg0.ts", "h0.ts").replace("seg1.ts", "h1.ts")
    (tmp_path / "high.m3u8").write_text(high)
    master = tmp_path / "master.m3u8"
    master.write_text(MASTER)
    info = parse_m3u8(None, str(master), local_path=master, quality="best")
    assert info.is_master
    assert "high.m3u8" in info.media_playlist_url
    info_low = parse_m3u8(None, str(master), local_path=master, quality="worst")
    assert "low.m3u8" in info_low.media_playlist_url

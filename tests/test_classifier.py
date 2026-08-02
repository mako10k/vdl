"""ResourceKind classification matrix (D1)."""

from __future__ import annotations

from vdl.classifier import ResourceKind, classify_from_meta, kind_from_url


def test_url_extensions() -> None:
    assert kind_from_url("https://x/a.mp4") == ResourceKind.VIDEO
    assert kind_from_url("https://x/a.m3u8") == ResourceKind.INDEX
    assert kind_from_url("https://x/a.mpd") == ResourceKind.INDEX
    assert kind_from_url("https://x/page.html") == ResourceKind.HTML
    assert kind_from_url("https://x/") == ResourceKind.HTML


def test_content_type_priority_over_url() -> None:
    # misleading extension but HTML content-type
    k = classify_from_meta(
        "https://x/clip.mp4",
        content_type="text/html; charset=utf-8",
    )
    assert k == ResourceKind.HTML


def test_body_sniff_m3u8() -> None:
    k = classify_from_meta(
        "https://x/stream",
        content_type="application/octet-stream",
        body_sample=b"#EXTM3U\n#EXT-X-VERSION:3\n",
    )
    assert k == ResourceKind.INDEX


def test_body_sniff_html() -> None:
    k = classify_from_meta(
        "https://x/watch",
        body_sample=b"<!DOCTYPE html><html><body>hi</body></html>",
    )
    assert k == ResourceKind.HTML


def test_video_content_type() -> None:
    k = classify_from_meta(
        "https://x/blob",
        content_type="video/mp4",
    )
    assert k == ResourceKind.VIDEO


def test_mpegurl_content_type() -> None:
    k = classify_from_meta(
        "https://x/pl",
        content_type="application/vnd.apple.mpegurl",
    )
    assert k == ResourceKind.INDEX


def test_bare_path_defaults_html() -> None:
    k = classify_from_meta("https://example.com/watch/123")
    assert k == ResourceKind.HTML

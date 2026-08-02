from __future__ import annotations

from vdl.scraper import extract_candidates


HTML = """
<html><head><title>Demo Page</title>
<meta property="og:video" content="https://cdn.example.com/og.mp4"/>
</head><body>
<video src="https://cdn.example.com/clip.webm"></video>
<a href="/media/stream.m3u8">HLS</a>
<a href="/dash/manifest.mpd">DASH</a>
<script>
var u = "https://cdn.example.com/playlist.m3u8?token=1";
</script>
<script type="application/ld+json">
{"@type":"VideoObject","contentUrl":"https://cdn.example.com/ld.mp4","name":"LD"}
</script>
</body></html>
"""


def test_extract_candidates_collects_sources() -> None:
    cands = extract_candidates(HTML, "https://example.com/watch/1")
    urls = {c.url for c in cands}
    kinds = {c.kind for c in cands}
    assert any(".mp4" in u for u in urls)
    assert any(".m3u8" in u for u in urls)
    assert any(".webm" in u for u in urls)
    assert "video" in kinds
    assert "index" in kinds
    # absolute-ized relative
    assert any(u.endswith(".m3u8") and u.startswith("https://example.com/") for u in urls)


def test_dedupe_normalized() -> None:
    html = """
    <a href="https://cdn.example.com/v.mp4">a</a>
    <a href="https://cdn.example.com/v.mp4#frag">b</a>
    """
    cands = extract_candidates(html, "https://example.com/")
    mp4s = [c for c in cands if c.url.rstrip("/").endswith(".mp4") or ".mp4" in c.url]
    # fragment stripped by normalize → one candidate
    assert len(mp4s) == 1

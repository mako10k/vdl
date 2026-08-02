from __future__ import annotations

from vdl.scraper import classify_media_url, extract_candidates


HTML = """
<html><head><title>Demo Page</title>
<meta property="og:video" content="https://cdn.example.com/og.mp4"/>
<meta property="og:video:type" content="video/mp4"/>
<meta property="og:video:url" content="https://cdn.example.com/player/watch.html"/>
</head><body>
<video src="https://cdn.example.com/clip.webm"></video>
<a href="/media/stream.m3u8">HLS</a>
<a href="/dash/manifest.mpd">DASH</a>
<a href="/play/123.html">player page</a>
<a href="/videos/index">html-like index path</a>
<iframe src="/embed/player.php?id=1"></iframe>
<iframe src="https://cdn.example.com/direct/stream.m3u8"></iframe>
<script>
var u = "https://cdn.example.com/playlist.m3u8?token=1";
var page = "https://cdn.example.com/watch/999";
</script>
<script type="application/ld+json">
{"@type":"VideoObject","contentUrl":"https://cdn.example.com/ld.mp4","name":"LD",
 "url":"https://cdn.example.com/watch/ld-page"}
</script>
</body></html>
"""


def test_extract_candidates_collects_sources() -> None:
    cands = extract_candidates(HTML, "https://example.com/watch/1")
    urls = {c.url for c in cands}
    kinds = {c.kind for c in cands}
    assert any(u.endswith(".mp4") or ".mp4" in u for u in urls)
    assert any(".m3u8" in u for u in urls)
    assert any(".webm" in u for u in urls)
    assert "video" in kinds
    assert "index" in kinds
    assert any(u.endswith(".m3u8") and u.startswith("https://example.com/") for u in urls)


def test_rejects_html_pages_and_player_urls() -> None:
    cands = extract_candidates(HTML, "https://example.com/watch/1")
    urls = {c.url for c in cands}
    # HTML player / watch pages must not appear
    assert not any(u.endswith(".html") or u.endswith(".php") for u in urls)
    assert "https://cdn.example.com/player/watch.html" not in urls
    assert "https://cdn.example.com/watch/999" not in urls
    assert "https://cdn.example.com/watch/ld-page" not in urls
    assert not any("/videos/index" == urlparse_path(u) for u in urls)


def urlparse_path(u: str) -> str:
    from urllib.parse import urlparse

    return urlparse(u).path


def test_classify_media_url_strict() -> None:
    assert classify_media_url("https://x/a.mp4") == "video"
    assert classify_media_url("https://x/a.m3u8?t=1") == "index"
    assert classify_media_url("https://x/play/1.html") is None
    assert classify_media_url("https://x/watch/123") is None
    assert classify_media_url("https://x/blob", mime="video/mp4") == "video"
    assert classify_media_url("https://x/page.html", mime="video/mp4") is None
    assert classify_media_url("https://x/a", mime="text/html") is None
    # image ext never becomes video even with wrong type=video/*
    assert classify_media_url("https://x/thumb.jpg", mime="video/mp4") is None
    assert classify_media_url("https://x/a.png", mime="video/webm") is None
    assert classify_media_url("https://x/p.webp") is None
    # real media wins over wrong image MIME
    assert classify_media_url("https://x/a.mp4", mime="image/jpeg") == "video"


def test_rejects_hls_segment_ts() -> None:
    assert classify_media_url("https://cdn.example.com/seg0001.ts") is None
    assert classify_media_url("https://cdn.example.com/chunk_12.ts") is None
    assert classify_media_url("https://cdn.example.com/x.m4s") is None
    # rare standalone .ts may still count as video
    assert classify_media_url("https://cdn.example.com/movie.ts") == "video"


def test_rejects_image_source_with_video_type() -> None:
    html = """
    <video poster="https://cdn.example.com/p.jpg" src="https://cdn.example.com/ok.mp4">
      <source src="https://cdn.example.com/wrong.jpg" type="video/mp4"/>
      <source src="https://cdn.example.com/ok2.webm" type="video/webm"/>
    </video>
    <picture><source src="https://cdn.example.com/pic.jpg" type="image/jpeg"/></picture>
    <a href="https://cdn.example.com/cover.jpg">cover</a>
    """
    cands = extract_candidates(html, "https://example.com/")
    urls = {c.url for c in cands}
    assert "https://cdn.example.com/ok.mp4" in urls
    assert "https://cdn.example.com/ok2.webm" in urls
    assert not any(u.endswith((".jpg", ".jpeg", ".png", ".webp")) for u in urls)


def test_dedupe_normalized() -> None:
    html = """
    <a href="https://cdn.example.com/v.mp4">a</a>
    <a href="https://cdn.example.com/v.mp4#frag">b</a>
    """
    cands = extract_candidates(html, "https://example.com/")
    mp4s = [c for c in cands if ".mp4" in c.url]
    assert len(mp4s) == 1


def test_iframe_recursion_finds_nested_media() -> None:
    outer = """
    <html><body>
    <iframe src="https://embed.example.com/player.html"></iframe>
    <a href="/not-media/page.html">x</a>
    </body></html>
    """
    inner = """
    <html><body>
    <source src="https://cdn.example.com/nested.m3u8" type="application/vnd.apple.mpegurl"/>
    <a href="https://cdn.example.com/nested.mp4">dl</a>
    </body></html>
    """

    def fetch(u: str) -> str | None:
        if "embed.example.com" in u:
            return inner
        return None

    cands = extract_candidates(
        outer,
        "https://example.com/watch/1",
        fetch_html=fetch,
        iframe_depth=1,
    )
    urls = {c.url for c in cands}
    assert "https://cdn.example.com/nested.m3u8" in urls
    assert "https://cdn.example.com/nested.mp4" in urls
    # iframe page itself is not a candidate
    assert not any("player.html" in u for u in urls)
    assert any(c.source.startswith("iframe>") for c in cands)

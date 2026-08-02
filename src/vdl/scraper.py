"""Extract video / stream candidates from HTML pages."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from vdl.util import (
    INDEX_EXTENSIONS,
    VIDEO_EXTENSIONS,
    is_index_extension,
    is_video_extension,
    normalize_url,
    url_extension,
)


@dataclass
class MediaCandidate:
    url: str
    kind: str  # "video" | "index"
    source: str  # where found: video_tag, source_tag, anchor, meta, jsonld, script, iframe
    title: str | None = None
    mime: str | None = None
    extras: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        t = self.title or ""
        note = ""
        if self.kind == "index" and (
            self.url.lower().endswith(".mpd") or ".mpd?" in self.url.lower()
        ):
            note = " [MPD=MVP非対応]"
        return f"[{self.kind}] {t + ' — ' if t else ''}{self.url}{note}  ({self.source})"


# Patterns inside inline scripts / page text
_URL_RE = re.compile(
    r"""(?P<url>https?://[^\s"'<>\\]+?\.(?:mp4|webm|mkv|mov|m4v|m3u8|mpd)(?:\?[^\s"'<>\\]*)?)""",
    re.I,
)
_REL_URL_RE = re.compile(
    r"""(?P<url>(?:/|\./|\.\./)[^\s"'<>\\]+?\.(?:mp4|webm|mkv|mov|m4v|m3u8|mpd)(?:\?[^\s"'<>\\]*)?)""",
    re.I,
)


def _kind_for_url(url: str) -> str | None:
    if is_index_extension(url) or url_extension(url) in INDEX_EXTENSIONS:
        return "index"
    if is_video_extension(url) or url_extension(url) in VIDEO_EXTENSIONS:
        return "video"
    # query-only m3u8 sometimes ends without clear ext in path — handled by regex groups
    path = urlparse(url).path.lower()
    if ".m3u8" in path or path.endswith(".m3u"):
        return "index"
    if any(path.endswith(e) or f"{e}?" in path for e in VIDEO_EXTENSIONS):
        return "video"
    return None


def _add(
    found: dict[str, MediaCandidate],
    raw_url: str,
    base: str,
    *,
    source: str,
    title: str | None = None,
    mime: str | None = None,
    extras: dict | None = None,
) -> None:
    if not raw_url or raw_url.startswith("data:"):
        return
    abs_url = urljoin(base, raw_url.strip().strip("'\""))
    # strip trailing punctuation from regex captures
    abs_url = abs_url.rstrip("\\).,;'\"")
    kind = _kind_for_url(abs_url)
    if kind is None:
        # mime hint
        if mime:
            m = mime.lower()
            if "mpegurl" in m or m == "application/dash+xml":
                kind = "index"
            elif m.startswith("video/"):
                kind = "video"
    if kind is None:
        return
    key = normalize_url(abs_url)
    if key in found:
        # enrich title if missing
        if title and not found[key].title:
            found[key].title = title
        return
    found[key] = MediaCandidate(
        url=abs_url,
        kind=kind,
        source=source,
        title=title,
        mime=mime,
        extras=extras or {},
    )


def _walk_json(obj: object, base: str, found: dict[str, MediaCandidate], source: str) -> None:
    if isinstance(obj, dict):
        # common keys
        for k, v in obj.items():
            lk = str(k).lower()
            if isinstance(v, str) and lk in {
                "contenturl",
                "url",
                "src",
                "file",
                "video_url",
                "videourl",
                "stream",
                "stream_url",
                "hls",
                "hlsurl",
                "playlist",
                "source",
                "playbackurl",
            }:
                _add(found, v, base, source=source, title=obj.get("name") if isinstance(obj.get("name"), str) else None)
            elif isinstance(v, str) and (_kind_for_url(v) or any(ext in v.lower() for ext in (".m3u8", ".mp4", ".webm"))):
                _add(found, v, base, source=source)
            else:
                _walk_json(v, base, found, source)
    elif isinstance(obj, list):
        for item in obj:
            _walk_json(item, base, found, source)
    elif isinstance(obj, str):
        if _kind_for_url(obj) or any(ext in obj.lower() for ext in (".m3u8", ".mp4")):
            _add(found, obj, base, source=source)


def extract_candidates(html: str, page_url: str) -> list[MediaCandidate]:
    """Parse HTML and return unique media candidates."""
    found: dict[str, MediaCandidate] = {}
    soup = BeautifulSoup(html, "lxml")

    page_title = None
    if soup.title and soup.title.string:
        page_title = soup.title.string.strip()

    # <video src> and nested <source>
    for video in soup.find_all("video"):
        if video.get("src"):
            _add(
                found,
                video["src"],
                page_url,
                source="video_tag",
                title=video.get("title") or page_title,
            )
        for src in video.find_all("source"):
            _add(
                found,
                src.get("src"),
                page_url,
                source="source_tag",
                title=video.get("title") or page_title,
                mime=src.get("type"),
            )

    # bare <source>
    for src in soup.find_all("source"):
        _add(found, src.get("src"), page_url, source="source_tag", mime=src.get("type"))

    # anchors to media files
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if _kind_for_url(urljoin(page_url, href)):
            text = a.get_text(strip=True) or a.get("title")
            _add(found, href, page_url, source="anchor", title=text or page_title)

    # og:video / twitter:player etc.
    for prop in (
        "og:video",
        "og:video:url",
        "og:video:secure_url",
        "twitter:player:stream",
    ):
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            _add(found, tag["content"], page_url, source="meta", title=page_title)

    # JSON-LD
    for script in soup.find_all("script", type=lambda t: t and "ld+json" in t):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
            _walk_json(data, page_url, found, "jsonld")
        except json.JSONDecodeError:
            for m in _URL_RE.finditer(raw):
                _add(found, m.group("url"), page_url, source="jsonld")

    # iframe src (sometimes embed pages — keep as candidate only if media ext)
    for iframe in soup.find_all("iframe", src=True):
        src = iframe["src"]
        if _kind_for_url(urljoin(page_url, src)):
            _add(found, src, page_url, source="iframe", title=page_title)

    # inline scripts: absolute + relative media URLs
    for script in soup.find_all("script"):
        raw = script.string or script.get_text() or ""
        if not raw or len(raw) > 2_000_000:
            continue
        for m in _URL_RE.finditer(raw):
            _add(found, m.group("url"), page_url, source="script", title=page_title)
        for m in _REL_URL_RE.finditer(raw):
            _add(found, m.group("url"), page_url, source="script", title=page_title)
        # try JSON blobs in script
        for jm in re.finditer(r"\{[^{}]{20,}\}", raw):
            try:
                data = json.loads(jm.group(0))
                _walk_json(data, page_url, found, "script_json")
            except json.JSONDecodeError:
                pass

    # full-page URL scrape as last pass (og etc. already covered)
    for m in _URL_RE.finditer(html):
        _add(found, m.group("url"), page_url, source="page_text", title=page_title)

    # Prefer video/index over noise: sort index first then video, stable by url
    items = list(found.values())
    items.sort(key=lambda c: (0 if c.kind == "index" else 1, c.url))
    return items

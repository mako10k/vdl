"""Extract video / HLS index candidates from HTML (strict ext/MIME; iframe recurse)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from vdl.util import (
    INDEX_EXTENSIONS,
    VIDEO_EXTENSIONS,
    normalize_url,
    url_extension,
)

# Optional fetcher: url -> html text (or None on failure). Used for iframe recursion.
FetchHtml = Callable[[str], Optional[str]]


@dataclass
class MediaCandidate:
    url: str
    kind: str  # "video" | "index"
    source: str  # video_tag, source_tag, anchor, meta, jsonld, script, iframe, …
    title: str | None = None
    mime: str | None = None
    extras: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        t = self.title or ""
        note = ""
        if self.kind == "index" and (
            ".mpd" in urlparse(self.url).path.lower()
        ):
            note = " [MPD=MVP非対応]"
        return f"[{self.kind}] {t + ' — ' if t else ''}{self.url}{note}  ({self.source})"


# HTML / document-like extensions — never treat as media
_PAGE_EXTENSIONS = {
    ".html",
    ".htm",
    ".xhtml",
    ".php",
    ".asp",
    ".aspx",
    ".jsp",
    ".cgi",
    ".pl",
    ".py",
    ".rb",
    ".do",
    ".action",
    ".cfm",
    ".shtml",
    ".css",
    ".js",
    ".mjs",
    ".json",
    ".xml",
    ".svg",
    ".txt",
    ".pdf",
    ".zip",
    ".rar",
    ".7z",
    ".gz",
}

# Images / non-AV assets — never promote to video even if type="video/*"
_NON_MEDIA_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".ico",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".avif",
    ".svg",
    ".css",
    ".js",
    ".mjs",
    ".map",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".vtt",
    ".srt",
    ".ass",
    ".ssa",
}

# Prefer more specific discovery sources when the same URL is seen twice
_SOURCE_RANK: dict[str, int] = {
    "video_tag": 10,
    "source_tag": 20,
    "audio_tag": 25,
    "iframe": 30,
    "embed": 35,
    "object": 36,
    "meta": 40,
    "jsonld": 45,
    "anchor": 50,
    "script_json": 60,
    "script": 70,
    "page_text": 90,
}

_INDEX_MIME = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "application/mpegurl",
    "application/dash+xml",
}

_VIDEO_MIME_PREFIXES = ("video/",)
_VIDEO_MIME_EXACT = {
    "application/mp4",
    "application/x-matroska",
}

# Media file extensions we accept (path last segment only)
_MEDIA_VIDEO_EXT = VIDEO_EXTENSIONS
_MEDIA_INDEX_EXT = INDEX_EXTENSIONS

# Strict media URL in text (extension must be path segment, not mid-host)
_URL_RE = re.compile(
    r"""(?P<url>https?://[^\s"'<>\\]+?\.(?:mp4|webm|mkv|mov|m4v|m3u8|m3u|mpd|ts|mpg|mpeg|avi|flv|wmv|3gp|ogv)(?:\?[^\s"'<>\\]*)?)""",
    re.I,
)
_REL_URL_RE = re.compile(
    r"""(?P<url>(?:/|\./|\.\./)[^\s"'<>\\]+?\.(?:mp4|webm|mkv|mov|m4v|m3u8|m3u|mpd|ts|mpg|mpeg|avi|flv|wmv|3gp|ogv)(?:\?[^\s"'<>\\]*)?)""",
    re.I,
)

# JSON keys that often hold playback URLs
_MEDIA_JSON_KEYS = {
    "contenturl",
    "video_url",
    "videourl",
    "stream_url",
    "streamurl",
    "hls",
    "hlsurl",
    "hls_url",
    "m3u8",
    "m3u8_url",
    "dash",
    "dash_url",
    "playbackurl",
    "playback_url",
    "file",
    "src",
    "source",
}


def _path_last_segment(url: str) -> str:
    path = unquote(urlparse(url).path or "")
    seg = path.rsplit("/", 1)[-1] if path else ""
    return seg


def _segment_extension(url: str) -> str:
    """Extension of the last path segment only (ignores query)."""
    seg = _path_last_segment(url)
    if "." not in seg:
        return ""
    return "." + seg.rsplit(".", 1)[-1].lower()


def _is_page_like_url(url: str) -> bool:
    """True if URL looks like an HTML/document page, not a media file."""
    ext = _segment_extension(url)
    if ext in _PAGE_EXTENSIONS:
        return True
    # bare path ending with / or no extension — treat as page-like unless mime says otherwise
    path = urlparse(url).path or ""
    if path.endswith("/") or path == "":
        return True
    # common player/watch page patterns without media ext
    if ext == "" and re.search(
        r"/(watch|play|player|embed|video|v|clip|view)(/|$)",
        path,
        re.I,
    ):
        return True
    return False


def _mime_main(mime: str | None) -> str:
    if not mime:
        return ""
    return mime.split(";", 1)[0].strip().lower()


def _is_image_or_doc_mime(mime_main: str) -> bool:
    if not mime_main:
        return False
    if mime_main.startswith("image/"):
        return True
    return mime_main in {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "text/css",
        "application/javascript",
        "text/javascript",
        "application/json",
        "font/woff",
        "font/woff2",
        "application/font-woff",
        "application/font-woff2",
    }


def _source_base(source: str) -> str:
    """Strip iframe> prefixes for ranking."""
    if ">" in source:
        source = source.rsplit(">", 1)[-1]
    if ":" in source and not source.startswith("http"):
        # e.g. prefix:video_tag from nested scrape — keep last token
        source = source.rsplit(":", 1)[-1]
    return source


def _source_rank(source: str) -> int:
    return _SOURCE_RANK.get(_source_base(source), 80)


def classify_media_url(
    url: str,
    *,
    mime: str | None = None,
) -> str | None:
    """
    Return 'video' | 'index' | None.

    Rules (strict):
    - Last path-segment extension is authoritative for known media / non-media.
    - Image/document extensions (.jpg, .png, …) are NEVER media, even if
      ``type="video/mp4"`` is set on a ``<source>`` tag (common poster mistake).
    - Real media extensions (.mp4, .m3u8, …) win even if MIME is wrong/missing.
    - MIME is only used when the path has no usable media/non-media extension
      (CDN blob URLs without an extension).
    """
    if not url or url.startswith(("data:", "javascript:", "blob:", "#")):
        return None

    mime_main = _mime_main(mime)
    ext = _segment_extension(url)

    # Hard reject non-media / page extensions (tag MIME cannot override)
    if ext in _NON_MEDIA_EXTENSIONS or ext in _PAGE_EXTENSIONS:
        return None

    # Explicit media extensions win over wrong MIME (e.g. type=image/jpeg on .mp4)
    if ext in _MEDIA_INDEX_EXT:
        return "index"
    # HLS/DASH segments are never candidates (align with Chrome extension)
    if ext in {".m4s", ".m4f"}:
        return None
    if ext == ".ts":
        path_l0 = (urlparse(url).path or "").lower()
        if re.search(
            r"(?:seg(?:ment)?|chunk|media|part|init)[_-]?\d+",
            path_l0,
            re.I,
        ):
            return None
        # rare standalone .ts file → video
        return "video"
    if ext in _MEDIA_VIDEO_EXT:
        return "video"

    # path ends with media ext even with odd trailing segments
    path_l = (urlparse(url).path or "").lower()
    if path_l.endswith(".m3u8") or path_l.endswith(".m3u") or path_l.endswith(".mpd"):
        return "index"

    # Extensionless / unknown ext: use MIME carefully
    if mime_main:
        if _is_image_or_doc_mime(mime_main):
            return None
        if mime_main in _INDEX_MIME or "mpegurl" in mime_main:
            return "index"
        if mime_main.startswith(_VIDEO_MIME_PREFIXES) or mime_main in _VIDEO_MIME_EXACT:
            # reject page-like paths even with video MIME
            if _is_page_like_url(url):
                return None
            return "video"

    # No extension + no strong mime → not a media candidate
    if _is_page_like_url(url) or ext == "":
        return None

    return None


def _kind_for_url(url: str, mime: str | None = None) -> str | None:
    return classify_media_url(url, mime=mime)


def _clean_url(raw: str, base: str) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip().strip("'\"")
    if not raw or raw.startswith(("data:", "javascript:", "blob:", "#")):
        return None
    abs_url = urljoin(base, raw)
    abs_url = abs_url.rstrip("\\).,;'\"")
    # drop pure fragments
    if abs_url.startswith("#"):
        return None
    return abs_url


def _add(
    found: dict[str, MediaCandidate],
    raw_url: str | None,
    base: str,
    *,
    source: str,
    title: str | None = None,
    mime: str | None = None,
    extras: dict | None = None,
) -> None:
    if not raw_url:
        return
    abs_url = _clean_url(raw_url, base)
    if not abs_url:
        return
    kind = classify_media_url(abs_url, mime=mime)
    if kind is None:
        return
    key = normalize_url(abs_url)
    if key in found:
        prev = found[key]
        if title and not prev.title:
            prev.title = title
        if mime and not prev.mime:
            prev.mime = mime
        # Prefer higher-quality discovery source; keep first URL string
        if _source_rank(source) < _source_rank(prev.source):
            prev.source = source
            if extras:
                prev.extras = {**prev.extras, **extras}
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
        title = obj.get("name") if isinstance(obj.get("name"), str) else None
        for k, v in obj.items():
            lk = str(k).lower().replace("-", "_")
            if isinstance(v, str):
                if lk in _MEDIA_JSON_KEYS or lk.endswith("_url") or lk.endswith("url"):
                    # Only add if it classifies as media (strict)
                    if classify_media_url(v) or classify_media_url(urljoin(base, v)):
                        _add(found, v, base, source=source, title=title)
                elif classify_media_url(v):
                    _add(found, v, base, source=source, title=title)
                else:
                    # still recurse into nested? skip string
                    pass
            else:
                _walk_json(v, base, found, source)
    elif isinstance(obj, list):
        for item in obj:
            _walk_json(item, base, found, source)
    elif isinstance(obj, str):
        if classify_media_url(obj):
            _add(found, obj, base, source=source)


def _extract_from_soup(
    soup: BeautifulSoup,
    page_url: str,
    found: dict[str, MediaCandidate],
    *,
    page_title: str | None,
    source_prefix: str = "",
) -> list[str]:
    """
    Fill `found` from one document. Returns absolute iframe src URLs that look
    like embed pages (not direct media) for optional recursion.
    """
    pref = f"{source_prefix}:" if source_prefix else ""
    iframe_pages: list[str] = []

    for video in soup.find_all("video"):
        # poster= is intentionally ignored (almost always an image)
        if video.get("src"):
            _add(
                found,
                video["src"],
                page_url,
                source=f"{pref}video_tag",
                title=video.get("title") or page_title,
                mime=video.get("type"),
            )
        for src in video.find_all("source"):
            # type="image/*" or .jpg + type=video/* → rejected by classify
            _add(
                found,
                src.get("src"),
                page_url,
                source=f"{pref}source_tag",
                title=video.get("title") or page_title,
                mime=src.get("type"),
            )

    for audio in soup.find_all("audio"):
        if audio.get("src"):
            _add(
                found,
                audio["src"],
                page_url,
                source=f"{pref}audio_tag",
                title=audio.get("title") or page_title,
                mime=audio.get("type"),
            )
        for src in audio.find_all("source"):
            _add(
                found,
                src.get("src"),
                page_url,
                source=f"{pref}source_tag",
                title=audio.get("title") or page_title,
                mime=src.get("type"),
            )

    # Note: bare <source> under <picture> is intentionally ignored (images).
    # Media <source> is collected only via the video/audio loops above.

    for a in soup.find_all("a", href=True):
        href = a["href"]
        abs_u = urljoin(page_url, href)
        if classify_media_url(abs_u):
            text = a.get_text(strip=True) or a.get("title")
            _add(found, href, page_url, source=f"{pref}anchor", title=text or page_title)

    for prop in (
        "og:video",
        "og:video:url",
        "og:video:secure_url",
        "twitter:player:stream",
    ):
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            # og:video:type if present
            mime = None
            if prop.startswith("og:video"):
                ttag = soup.find("meta", property="og:video:type")
                if ttag and ttag.get("content"):
                    mime = ttag["content"]
            content = tag["content"]
            # Only accept if media (skip HTML player landing pages in og:video)
            if classify_media_url(content, mime=mime):
                _add(
                    found,
                    content,
                    page_url,
                    source=f"{pref}meta",
                    title=page_title,
                    mime=mime,
                )

    for script in soup.find_all("script", type=lambda t: t and "ld+json" in str(t)):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
            _walk_json(data, page_url, found, f"{pref}jsonld")
        except json.JSONDecodeError:
            for m in _URL_RE.finditer(raw):
                _add(found, m.group("url"), page_url, source=f"{pref}jsonld")

    # iframe: collect media directly; queue HTML-like for recursion
    for iframe in soup.find_all("iframe", src=True):
        src = iframe["src"]
        abs_src = urljoin(page_url, src)
        if classify_media_url(abs_src):
            _add(found, src, page_url, source=f"{pref}iframe", title=page_title)
        else:
            # skip about:blank, javascript
            if abs_src.startswith(("http://", "https://", "/")):
                iframe_pages.append(abs_src)

    # also <embed src> / <object data>
    for emb in soup.find_all("embed", src=True):
        _add(found, emb["src"], page_url, source=f"{pref}embed", title=page_title)
    for obj in soup.find_all("object", data=True):
        _add(found, obj["data"], page_url, source=f"{pref}object", title=page_title)

    for script in soup.find_all("script"):
        raw = script.string or script.get_text() or ""
        if not raw or len(raw) > 2_000_000:
            continue
        for m in _URL_RE.finditer(raw):
            _add(found, m.group("url"), page_url, source=f"{pref}script", title=page_title)
        for m in _REL_URL_RE.finditer(raw):
            _add(found, m.group("url"), page_url, source=f"{pref}script", title=page_title)
        for jm in re.finditer(r"\{[^{}]{20,}\}", raw):
            try:
                data = json.loads(jm.group(0))
                _walk_json(data, page_url, found, f"{pref}script_json")
            except json.JSONDecodeError:
                pass

    return iframe_pages


def extract_candidates(
    html: str,
    page_url: str,
    *,
    fetch_html: FetchHtml | None = None,
    iframe_depth: int = 1,
    max_iframes: int = 8,
) -> list[MediaCandidate]:
    """
    Parse HTML and return unique media candidates (video file or stream index).

    - Only URLs with media extensions (last path segment) or strong media MIME
    - HTML/player pages are not listed as candidates
    - iframes: if src is media → candidate; else fetch HTML (when fetch_html
      provided) and recurse up to iframe_depth
    """
    found: dict[str, MediaCandidate] = {}
    soup = BeautifulSoup(html, "lxml")
    page_title = None
    if soup.title and soup.title.string:
        page_title = soup.title.string.strip()

    iframe_pages = _extract_from_soup(
        soup, page_url, found, page_title=page_title, source_prefix=""
    )

    # Strict media URLs anywhere in page text (extension-gated regex)
    for m in _URL_RE.finditer(html):
        _add(found, m.group("url"), page_url, source="page_text", title=page_title)

    # iframe recursion
    if fetch_html and iframe_depth > 0 and iframe_pages:
        seen_frames: set[str] = set()
        for i, frame_url in enumerate(iframe_pages):
            if i >= max_iframes:
                break
            key = normalize_url(frame_url)
            if key in seen_frames:
                continue
            seen_frames.add(key)
            # skip if already have as media
            if key in found:
                continue
            try:
                sub_html = fetch_html(frame_url)
            except Exception:
                sub_html = None
            if not sub_html:
                continue
            # recurse with reduced depth; prefix source
            nested = extract_candidates(
                sub_html,
                frame_url,
                fetch_html=fetch_html if iframe_depth > 1 else None,
                iframe_depth=iframe_depth - 1,
                max_iframes=max_iframes,
            )
            for c in nested:
                k = normalize_url(c.url)
                if k not in found:
                    # mark provenance via iframe
                    found[k] = MediaCandidate(
                        url=c.url,
                        kind=c.kind,
                        source=f"iframe>{c.source}",
                        title=c.title or page_title,
                        mime=c.mime,
                        extras={**c.extras, "iframe_src": frame_url},
                    )

    items = list(found.values())
    # index first, then video; stable by url
    items.sort(key=lambda c: (0 if c.kind == "index" else 1, c.url))
    return items

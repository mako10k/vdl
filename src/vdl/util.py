"""Shared helpers: hashing, filenames, URL normalization."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunparse


VIDEO_EXTENSIONS = {
    ".mp4",
    ".webm",
    ".mkv",
    ".mov",
    ".m4v",
    ".avi",
    ".flv",
    ".ts",
    ".mpg",
    ".mpeg",
    ".wmv",
    ".3gp",
    ".ogv",
}

INDEX_EXTENSIONS = {".m3u8", ".m3u", ".mpd"}
M3U8_EXTENSIONS = {".m3u8", ".m3u"}
MPD_EXTENSIONS = {".mpd"}

HTML_CONTENT_HINTS = {
    "text/html",
    "application/xhtml+xml",
}

VIDEO_CONTENT_HINTS = {
    "video/",
    "application/octet-stream",
    "application/mp4",
    "binary/octet-stream",
}

INDEX_CONTENT_HINTS = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "application/mpegurl",
    "application/dash+xml",
}


def normalize_url(url: str) -> str:
    """Normalize URL for registry matching (strip fragment, default ports)."""
    url = url.strip()
    parsed = urlparse(url)
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    # drop default ports
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    path = parsed.path or "/"
    # collapse //
    path = re.sub(r"/{2,}", "/", path)
    query = parsed.query  # keep query — often carries stream id
    return urlunparse((scheme, netloc, path, "", query, ""))


def url_extension(url: str) -> str:
    path = urlparse(url).path
    # strip query-like junk already handled; use last path segment
    name = unquote(path.rsplit("/", 1)[-1])
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


def is_video_extension(url: str) -> bool:
    return url_extension(url) in VIDEO_EXTENSIONS


def is_index_extension(url: str) -> bool:
    return url_extension(url) in INDEX_EXTENSIONS


def is_m3u8_extension(url: str) -> bool:
    return url_extension(url) in M3U8_EXTENSIONS


def is_mpd_url(url: str) -> bool:
    """True if URL or path looks like MPEG-DASH (MVP unsupported)."""
    ext = url_extension(url)
    if ext in MPD_EXTENSIONS:
        return True
    path = urlparse(url).path.lower()
    return path.endswith(".mpd") or ".mpd?" in path or "/manifest.mpd" in path


def resolve_local_path(url_or_path: str) -> Path | None:
    """
    If input is a local filesystem path or file:// URL that exists, return Path.
    Otherwise None (treat as remote URL).
    """
    raw = url_or_path.strip()
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        path = Path(unquote(parsed.path))
        return path if path.exists() else path  # return even if missing for clearer errors
    # bare path (absolute or relative), not a network URL
    if "://" not in raw:
        path = Path(raw).expanduser()
        if path.exists():
            return path.resolve()
    return None


def safe_filename(name: str, max_len: int = 180) -> str:
    """Make a filesystem-safe filename stem (without forcing extension)."""
    name = unquote(name)
    name = unicodedata.normalize("NFKC", name)
    # remove path separators and control chars
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "download"
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 8:
            keep = max_len - len(ext) - 1
            name = stem[:keep] + "." + ext
        else:
            name = name[:max_len]
    return name


def filename_from_url(url: str, default: str = "download") -> str:
    path = urlparse(url).path
    base = unquote(path.rsplit("/", 1)[-1]) if path else ""
    if not base or base in (".", "/"):
        return default
    return safe_filename(base)


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unique_path(directory: Path, filename: str) -> Path:
    """Return a non-colliding path inside directory."""
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    n = 1
    while True:
        alt = directory / f"{stem}_{n}{suffix}"
        if not alt.exists():
            return alt
        n += 1

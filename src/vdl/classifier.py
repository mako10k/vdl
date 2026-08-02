"""Classify a URL / response into video | index (m3u8) | html | unknown."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

import httpx

from vdl.util import (
    HTML_CONTENT_HINTS,
    INDEX_CONTENT_HINTS,
    INDEX_EXTENSIONS,
    VIDEO_CONTENT_HINTS,
    VIDEO_EXTENSIONS,
    is_index_extension,
    is_video_extension,
    normalize_url,
    url_extension,
)


class ResourceKind(str, Enum):
    VIDEO = "video"
    INDEX = "index"  # m3u8 / mpd etc.
    HTML = "html"
    UNKNOWN = "unknown"


@dataclass
class ClassifiedResource:
    url: str
    canonical_url: str
    kind: ResourceKind
    content_type: Optional[str]
    content_length: Optional[int]
    final_url: str  # after redirects
    filename_hint: Optional[str] = None
    headers: Optional[dict[str, str]] = None


def _content_type_main(ct: str | None) -> str:
    if not ct:
        return ""
    return ct.split(";", 1)[0].strip().lower()


def kind_from_content_type(ct: str | None) -> ResourceKind | None:
    main = _content_type_main(ct)
    if not main:
        return None
    if main in HTML_CONTENT_HINTS or main.startswith("text/html"):
        return ResourceKind.HTML
    if main in INDEX_CONTENT_HINTS or "mpegurl" in main or main.endswith("mpegurl"):
        return ResourceKind.INDEX
    if main.startswith("video/") or main in {
        "application/mp4",
        "application/x-matroska",
    }:
        return ResourceKind.VIDEO
    # application/octet-stream alone is inconclusive
    return None


def kind_from_url(url: str) -> ResourceKind | None:
    ext = url_extension(url)
    if ext in VIDEO_EXTENSIONS:
        return ResourceKind.VIDEO
    if ext in INDEX_EXTENSIONS:
        return ResourceKind.INDEX
    # common HTML endings
    if ext in {".html", ".htm", ".php", ".asp", ".aspx", ".jsp"}:
        return ResourceKind.HTML
    path = urlparse(url).path.rstrip("/")
    if not path or path == "":
        return ResourceKind.HTML  # bare domain → likely a page
    return None


def kind_from_body_sniff(sample: bytes, content_type: str | None) -> ResourceKind | None:
    """Peek at first bytes when content-type is missing/ambiguous."""
    text_start = sample[:256].lstrip()
    lower = text_start[:32].lower()
    if lower.startswith(b"<!doctype") or lower.startswith(b"<html") or lower.startswith(b"<head"):
        return ResourceKind.HTML
    # m3u8 playlists
    if lower.startswith(b"#extm3u") or b"#ext-x-" in sample[:2048].lower():
        return ResourceKind.INDEX
    # HTML-ish tags further in
    if b"<html" in sample[:4096].lower() or b"<body" in sample[:4096].lower():
        return ResourceKind.HTML
    main = _content_type_main(content_type)
    if main == "application/octet-stream":
        # if URL looked like video, trust that
        return None
    return None


def classify_from_meta(
    url: str,
    *,
    content_type: str | None = None,
    final_url: str | None = None,
    body_sample: bytes | None = None,
) -> ResourceKind:
    """Combine signals: content-type > body sniff > URL extension > default HTML for bare paths."""
    final = final_url or url

    by_ct = kind_from_content_type(content_type)
    if by_ct is not None:
        # octet-stream + video ext should be video
        if by_ct == ResourceKind.UNKNOWN:
            pass
        elif _content_type_main(content_type) not in {
            "application/octet-stream",
            "binary/octet-stream",
        }:
            return by_ct

    if body_sample:
        sniffed = kind_from_body_sniff(body_sample, content_type)
        if sniffed is not None:
            return sniffed

    by_url = kind_from_url(final) or kind_from_url(url)
    if by_url is not None:
        return by_url

    main = _content_type_main(content_type)
    if main in VIDEO_CONTENT_HINTS or main.startswith("video/"):
        return ResourceKind.VIDEO
    if main in INDEX_CONTENT_HINTS:
        return ResourceKind.INDEX
    if main in HTML_CONTENT_HINTS:
        return ResourceKind.HTML

    # last resort: no extension → treat as HTML page (scrape)
    if not url_extension(final):
        return ResourceKind.HTML
    return ResourceKind.UNKNOWN


def filename_from_content_disposition(cd: str | None) -> str | None:
    if not cd:
        return None
    # filename*=UTF-8''... or filename="..."
    m = re_search_filename(cd)
    return m


def re_search_filename(cd: str) -> str | None:
    import re
    from urllib.parse import unquote

    m = re.search(r"filename\*=(?:UTF-8''|utf-8'')([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="([^"]+)"', cd, re.I)
    if m:
        return m.group(1)
    m = re.search(r"filename=([^;]+)", cd, re.I)
    if m:
        return m.group(1).strip().strip('"')
    return None


class ResourceProbe:
    """HTTP probe to classify a remote URL."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        timeout: float = 30.0,
        user_agent: str | None = None,
    ) -> None:
        self.client = client
        self.timeout = timeout
        self.user_agent = user_agent or (
            "Mozilla/5.0 (compatible; vdl/0.1; +https://github.com/local/vdl)"
        )

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if extra:
            h.update(extra)
        return h

    def probe(self, url: str, extra_headers: dict[str, str] | None = None) -> ClassifiedResource:
        url = url.strip()
        headers = self._headers(extra_headers)

        content_type: str | None = None
        content_length: int | None = None
        final_url = url
        filename_hint: str | None = None
        body_sample = b""
        resp_headers: dict[str, str] = {}

        # Try HEAD first
        try:
            head = self.client.head(
                url, headers=headers, follow_redirects=True, timeout=self.timeout
            )
            final_url = str(head.url)
            content_type = head.headers.get("content-type")
            cl = head.headers.get("content-length")
            if cl and cl.isdigit():
                content_length = int(cl)
            filename_hint = filename_from_content_disposition(
                head.headers.get("content-disposition")
            )
            resp_headers = dict(head.headers)
            # Some servers reject HEAD — fall through to GET range
            if head.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"HEAD {head.status_code}", request=head.request, response=head
                )
        except Exception:
            # Range GET for sniffing
            try:
                get = self.client.get(
                    url,
                    headers={**headers, "Range": "bytes=0-8191"},
                    follow_redirects=True,
                    timeout=self.timeout,
                )
                final_url = str(get.url)
                content_type = get.headers.get("content-type") or content_type
                cl = get.headers.get("content-length") or get.headers.get(
                    "content-range", ""
                ).split("/")[-1]
                if isinstance(cl, str) and cl.isdigit():
                    content_length = int(cl)
                filename_hint = filename_hint or filename_from_content_disposition(
                    get.headers.get("content-disposition")
                )
                body_sample = get.content[:8192]
                resp_headers = dict(get.headers)
            except Exception:
                # last try: full GET limited by streaming
                get = self.client.get(
                    url, headers=headers, follow_redirects=True, timeout=self.timeout
                )
                get.raise_for_status()
                final_url = str(get.url)
                content_type = get.headers.get("content-type")
                body_sample = get.content[:8192]
                resp_headers = dict(get.headers)

        # If still no body sample and kind ambiguous, fetch a bit
        kind = classify_from_meta(
            url, content_type=content_type, final_url=final_url, body_sample=body_sample or None
        )
        if kind == ResourceKind.UNKNOWN and not body_sample:
            try:
                get = self.client.get(
                    final_url,
                    headers={**headers, "Range": "bytes=0-8191"},
                    follow_redirects=True,
                    timeout=self.timeout,
                )
                body_sample = get.content[:8192]
                content_type = get.headers.get("content-type") or content_type
                kind = classify_from_meta(
                    url,
                    content_type=content_type,
                    final_url=str(get.url),
                    body_sample=body_sample,
                )
                final_url = str(get.url)
            except Exception:
                pass

        return ClassifiedResource(
            url=url,
            canonical_url=normalize_url(final_url),
            kind=kind,
            content_type=content_type,
            content_length=content_length,
            final_url=final_url,
            filename_hint=filename_hint,
            headers=resp_headers,
        )

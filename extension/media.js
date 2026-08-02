/**
 * Media URL classification helpers (shared by content + background).
 */

const SEGMENT_TS =
  /(?:^|[/_-])(?:seg(?:ment)?|chunk|media|part|init)[_-]?\d+.*\.ts(?:\?|$)/i;
const EXT_RE =
  /\.(m3u8|mpd|mp4|webm|mkv|mov|m4v|ts|m4s|aac|mp3|flv)(?:[?#]|$)/i;

const IMAGE_EXT = /\.(jpe?g|png|gif|webp|bmp|ico|tiff?|heic|heif|avif|svg)([?#]|$)/i;

/** @param {string} url */
export function classifyMediaUrl(url) {
  if (!url || typeof url !== 'string') return null;
  const u = url.trim();
  if (!/^https?:\/\//i.test(u)) return null;
  if (u.startsWith('blob:') || u.startsWith('data:')) return null;

  // Images never become video candidates (even with video/* Content-Type)
  if (IMAGE_EXT.test(u)) return null;

  const lower = u.toLowerCase();
  // HLS playlist (path or query)
  if (/\.m3u8([?#]|$)/i.test(u) || lower.includes('m3u8') || lower.includes('playlist.m3u')) {
    return 'm3u8';
  }
  if (/\.mpd([?#]|$)/i.test(u) || lower.includes('manifest.mpd')) {
    return 'dash'; // mark; backend may reject — still show
  }
  // Skip typical HLS/DASH media segments
  if (SEGMENT_TS.test(u) || /\.m4s([?#]|$)/i.test(u)) {
    return null;
  }
  if (/\.(mp4|webm|mkv|mov|m4v)([?#]|$)/i.test(u)) {
    return 'video';
  }
  // standalone .ts (rare) — keep only if not segment-like
  if (/\.ts([?#]|$)/i.test(u) && !/segment|chunk|media_\d+/i.test(u)) {
    return 'video';
  }
  if (/\.(mp3|aac|flac|ogg)([?#]|$)/i.test(u)) {
    return 'audio';
  }
  return null;
}

/**
 * @param {string | undefined} contentType
 * @param {string} url
 */
export function classifyFromContentType(contentType, url) {
  // Path extension wins: never promote .jpg via Content-Type
  if (IMAGE_EXT.test(url || '')) return null;
  const byUrl = classifyMediaUrl(url);
  if (byUrl) return byUrl;

  const ct = (contentType || '').split(';')[0].trim().toLowerCase();
  if (!ct) return null;
  if (ct.startsWith('image/')) return null;
  if (
    ct.includes('mpegurl') ||
    ct === 'application/vnd.apple.mpegurl' ||
    ct === 'application/x-mpegurl'
  ) {
    return 'm3u8';
  }
  if (ct.includes('dash+xml') || ct === 'application/dash+xml') {
    return 'dash';
  }
  if (ct.startsWith('video/')) {
    if (SEGMENT_TS.test(url) || /\.m4s([?#]|$)/i.test(url)) return null;
    return 'video';
  }
  if (ct.startsWith('audio/')) {
    return 'audio';
  }
  return null;
}

/**
 * Prefer playlists over raw segments when both present.
 * @param {Array<{url: string, kind: string}>} items
 */
export function rankKind(kind) {
  const order = { m3u8: 0, dash: 1, video: 2, audio: 3 };
  return order[kind] ?? 9;
}

/** Absolute URL from page / base. */
export function absUrl(href, base) {
  try {
    return new URL(href, base || (typeof location !== 'undefined' ? location.href : undefined)).href;
  } catch {
    return null;
  }
}

export function hasMediaExtensionHint(url) {
  return EXT_RE.test(url || '');
}

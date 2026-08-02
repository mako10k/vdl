/**
 * In-page media discovery: <video>/<audio>/<source>, anchors, Performance API.
 * Reports findings to the service worker.
 */
(function () {
  // content scripts cannot use ES modules in all Chrome versions the same way —
  // inline classification to avoid import issues.
  const SEGMENT_TS =
    /(?:^|[/_-])(?:seg(?:ment)?|chunk|media|part|init)[_-]?\d+.*\.ts(?:\?|$)/i;

  const IMAGE_EXT = /\.(jpe?g|png|gif|webp|bmp|ico|tiff?|heic|heif|avif|svg)([?#]|$)/i;

  function classify(url) {
    if (!url || typeof url !== 'string') return null;
    const u = url.trim();
    if (!/^https?:\/\//i.test(u)) return null;
    if (u.startsWith('blob:') || u.startsWith('data:')) return null;
    // Never treat images as media (e.g. <source type="video/mp4" src="x.jpg">)
    if (IMAGE_EXT.test(u)) return null;
    const lower = u.toLowerCase();
    if (/\.m3u8([?#]|$)/i.test(u) || lower.includes('m3u8') || lower.includes('playlist.m3u')) {
      return 'm3u8';
    }
    if (/\.mpd([?#]|$)/i.test(u) || lower.includes('manifest.mpd')) return 'dash';
    if (SEGMENT_TS.test(u) || /\.m4s([?#]|$)/i.test(u)) return null;
    if (/\.(mp4|webm|mkv|mov|m4v)([?#]|$)/i.test(u)) return 'video';
    if (/\.ts([?#]|$)/i.test(u) && !/segment|chunk|media_\d+/i.test(u)) return 'video';
    if (/\.(mp3|aac|flac|ogg)([?#]|$)/i.test(u)) return 'audio';
    return null;
  }

  function abs(href) {
    try {
      return new URL(href, location.href).href;
    } catch {
      return null;
    }
  }

  function push(map, url, kind, source, title) {
    if (!url || !kind) return;
    if (!map.has(url)) {
      map.set(url, { url, kind, source, title: title || document.title || null });
    }
  }

  function scanDom(map) {
    document.querySelectorAll('video, audio').forEach((el) => {
      // poster is image — skip
      const src = el.currentSrc || el.src;
      if (src) {
        const u = abs(src);
        const k = classify(u);
        if (k) push(map, u, k, 'dom', el.getAttribute('title') || null);
      }
      el.querySelectorAll('source').forEach((s) => {
        const u = abs(s.src || s.getAttribute('src'));
        if (!u || IMAGE_EXT.test(u)) return;
        const type = (s.getAttribute('type') || '').toLowerCase();
        if (type.startsWith('image/')) return;
        let k = classify(u);
        if (!k && type.includes('mpegurl')) k = 'm3u8';
        if (!k && type.startsWith('video/') && !IMAGE_EXT.test(u)) {
          // extensionless CDN only
          if (!/\.[a-z0-9]{2,5}([?#]|$)/i.test(u)) k = 'video';
        }
        if (k) push(map, u, k, 'dom-source', null);
      });
    });

    // Only video/audio[src] — do not pick bare <source> under <picture>
    document.querySelectorAll('video[src], audio[src]').forEach((el) => {
      const u = abs(el.getAttribute('src'));
      const k = classify(u);
      if (u && k) push(map, u, k, 'dom-attr', null);
    });

    document.querySelectorAll('a[href]').forEach((a) => {
      const u = abs(a.getAttribute('href'));
      const k = classify(u);
      if (u && k) push(map, u, k, 'link', a.textContent?.trim()?.slice(0, 80) || null);
    });

    // common data attributes used by players
    document.querySelectorAll('[data-src], [data-video], [data-url], [data-stream]').forEach((el) => {
      for (const attr of ['data-src', 'data-video', 'data-url', 'data-stream', 'data-hls', 'data-m3u8']) {
        const raw = el.getAttribute(attr);
        if (!raw) continue;
        const u = abs(raw);
        const k = classify(u);
        if (u && k) push(map, u, k, 'data-attr', null);
      }
    });
  }

  function scanPerformance(map) {
    try {
      const entries = performance.getEntriesByType('resource') || [];
      for (const e of entries) {
        const u = e.name;
        const k = classify(u);
        if (k) push(map, u, k, 'performance', null);
      }
    } catch {
      /* ignore */
    }
  }

  function collect() {
    const map = new Map();
    scanDom(map);
    scanPerformance(map);
    return [...map.values()];
  }

  function report(items) {
    if (!items.length) return;
    try {
      chrome.runtime.sendMessage({ type: 'mediaFound', items, pageUrl: location.href });
    } catch {
      /* extension reloaded */
    }
  }

  // Initial + periodic light rescan
  report(collect());
  const mo = new MutationObserver(() => {
    // debounce
    clearTimeout(mo._t);
    mo._t = setTimeout(() => report(collect()), 600);
  });
  try {
    mo.observe(document.documentElement, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['src', 'href', 'data-src', 'data-url', 'data-hls', 'data-m3u8'],
    });
  } catch {
    /* ignore */
  }

  // Performance buffer fills as network loads
  setInterval(() => report(collect()), 4000);

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type === 'rescan') {
      const items = collect();
      report(items);
      sendResponse({ ok: true, items, pageUrl: location.href });
      return true;
    }
    return false;
  });
})();

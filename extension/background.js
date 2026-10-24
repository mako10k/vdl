/**
 * vdl service worker — media detection, context menus, enqueue, UI bridge.
 */
import { api, getSettings } from './api.js';
import { classifyFromContentType, classifyMediaUrl, rankKind, mediaUrlKey, uniqueMediaItems } from './media.js';

const MENU = {
  PAGE: 'vdl-enqueue-page',
  LINK: 'vdl-enqueue-link',
  MEDIA: 'vdl-enqueue-media',
  SCRAPE: 'vdl-scrape-page',
  DETECT: 'vdl-detect-page',
  OPEN_UI: 'vdl-open-console',
};

/** @type {Map<number, Map<string, object>>} */
const tabMedia = new Map();

function notify(title, message) {
  try {
    chrome.notifications.create({
      type: 'basic',
      iconUrl: 'icons/icon128.png',
      title,
      message: String(message).slice(0, 200),
      priority: 0,
    });
  } catch {
    /* optional */
  }
}

function getOrCreateTabMap(tabId) {
  if (!tabMedia.has(tabId)) tabMedia.set(tabId, new Map());
  return tabMedia.get(tabId);
}

function addMediaItems(tabId, items, pageUrl) {
  if (tabId == null || tabId < 0) return 0;
  const map = getOrCreateTabMap(tabId);
  let added = 0;
  for (const raw of items || []) {
    const url = (raw.url || '').trim();
    if (!url) continue;
    const kind = raw.kind || classifyMediaUrl(url);
    if (!kind) continue;
    const key = mediaUrlKey(url);
    const prev = map.get(key);
    if (!prev) {
      map.set(key, {
        url,
        kind,
        source: raw.source || 'unknown',
        title: raw.title || null,
        mime: raw.mime || null,
        pageUrl: pageUrl || raw.pageUrl || null,
        firstSeen: Date.now(),
      });
      added += 1;
    } else {
      // upgrade source/kind if better
      if (rankKind(kind) < rankKind(prev.kind)) prev.kind = kind;
      if (raw.source && prev.source === 'unknown') prev.source = raw.source;
      if (raw.title && !prev.title) prev.title = raw.title;
      if (raw.mime) prev.mime = raw.mime;
    }
  }
  if (added) void updateBadge(tabId);
  return added;
}

function listTabMedia(tabId) {
  const map = tabMedia.get(tabId);
  if (!map) return [];
  return [...map.values()].sort(
    (a, b) => rankKind(a.kind) - rankKind(b.kind) || a.url.localeCompare(b.url),
  );
}

async function updateBadge(tabId) {
  const n = listTabMedia(tabId).length;
  const text = n > 0 ? String(Math.min(n, 99)) : '';
  try {
    await chrome.action.setBadgeBackgroundColor({ color: '#1a3a6e', tabId });
    await chrome.action.setBadgeText({ text, tabId });
  } catch {
    /* ignore */
  }
}

async function enqueueUrl(url, { quality } = {}) {
  if (!url || !/^https?:\/\//i.test(url)) {
    throw new Error(`http(s) URL のみ対応: ${url || '(empty)'}`);
  }
  const settings = await getSettings();
  const q = quality || settings.quality || 'best';
  try {
    const r = await api.enqueue({ url, quality: q });
    const jobId = r?.job?.id;
    const started = r?.runtime?.started ? ' (worker started)' : '';
    notify('vdl queued', `job #${jobId} · ${url.slice(0, 80)}${started}`);
    return r;
  } catch (e) {
    const msg = e.message || String(e);
    if (msg.includes('HTML') || msg.includes('candidates')) {
      throw new Error(
        'HTML ページです。右クリック「候補を抽出」または検出→Console を使ってください',
      );
    }
    throw e;
  }
}

async function openConsoleWithMedia(tabId, pageUrl, { tab = 'candidates' } = {}) {
  const items = listTabMedia(tabId).map((m) => ({
    url: m.url,
    kind: m.kind,
    source: m.source,
    title: m.title,
    mime: m.mime,
  }));
  const settings = await getSettings();
  let url = `${settings.uiBase}/?tab=${encodeURIComponent(tab)}`;
  if (items.length) {
    const r = await api.bridgePush({
      page_url: pageUrl || null,
      items,
      tab,
      note: 'from chrome extension',
    });
    url = `${settings.uiBase}/?${r.ui_query || `vdl_bridge=${r.token}&tab=${tab}`}`;
  }
  await chrome.tabs.create({ url });
  return { url, count: items.length };
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU.DETECT,
      title: 'vdl: このページの動画を検出',
      contexts: ['page'],
    });
    chrome.contextMenus.create({
      id: MENU.OPEN_UI,
      title: 'vdl: 検出結果を Console で開く',
      contexts: ['page'],
    });
    chrome.contextMenus.create({
      id: MENU.PAGE,
      title: 'vdl: このページの候補を抽出',
      contexts: ['page'],
    });
    chrome.contextMenus.create({
      id: MENU.SCRAPE,
      title: 'vdl: サーバー側で候補スクレイプ…',
      contexts: ['page'],
    });
    chrome.contextMenus.create({
      id: MENU.LINK,
      title: 'vdl: リンクを Enqueue',
      contexts: ['link'],
    });
    chrome.contextMenus.create({
      id: MENU.MEDIA,
      title: 'vdl: メディア URL を Enqueue',
      contexts: ['video', 'audio', 'image'],
    });
  });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  tabMedia.delete(tabId);
});

chrome.tabs.onActivated.addListener(({ tabId }) => {
  void updateBadge(tabId);
});

// Network capture (HLS / progressive media)
chrome.webRequest.onCompleted.addListener(
  (details) => {
    if (details.tabId < 0) return;
    if (details.method && details.method !== 'GET' && details.method !== 'HEAD') return;
    let mime = null;
    if (details.responseHeaders) {
      const h = details.responseHeaders.find(
        (x) => x.name && x.name.toLowerCase() === 'content-type',
      );
      mime = h?.value || null;
    }
    const kind = classifyFromContentType(mime, details.url);
    if (!kind) return;
    // skip our own API
    if (/^https?:\/\/(127\.0\.0\.1|localhost):1876[56]\b/i.test(details.url)) return;
    addMediaItems(
      details.tabId,
      [{ url: details.url, kind, source: 'network', mime }],
      details.initiator || null,
    );
  },
  { urls: ['http://*/*', 'https://*/*'] },
  ['responseHeaders'],
);

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  try {
    const tabId = tab?.id;
    if (info.menuItemId === MENU.PAGE) {
      const url = tab?.url || info.pageUrl;
      if (tabId != null) {
        try {
          const res = await chrome.tabs.sendMessage(tabId, { type: 'rescan' });
          if (res?.items) addMediaItems(tabId, res.items, res.pageUrl || url);
        } catch {
          /* ignore */
        }
        if (listTabMedia(tabId).length) {
          notify('vdl', `検出 ${listTabMedia(tabId).length} 件 — ポップアップで選択`);
          return;
        }
      }
      if (url) {
        const r = await api.candidates(url);
        const candidates = uniqueMediaItems(r.candidates || []);
        await chrome.storage.session.set({
          lastCandidates: { page_url: r.page_url || url, candidates, at: Date.now() },
        });
        notify('vdl candidates', `${candidates.length} 件 — ポップアップで選択`);
      }
      return;
    }
    if (info.menuItemId === MENU.LINK) {
      await enqueueUrl(info.linkUrl);
      return;
    }
    if (info.menuItemId === MENU.MEDIA) {
      await enqueueUrl(info.srcUrl || info.linkUrl);
      return;
    }
    if (info.menuItemId === MENU.SCRAPE) {
      const url = tab?.url || info.pageUrl;
      if (!url) throw new Error('ページ URL がありません');
      await chrome.storage.session.set({ pendingScrapeUrl: url });
      const r = await api.candidates(url);
      await chrome.storage.session.set({
        lastCandidates: {
          page_url: r.page_url || url,
          candidates: uniqueMediaItems(r.candidates || []),
          at: Date.now(),
        },
      });
      notify('vdl candidates', `${r.count ?? 0} candidate(s) — ポップアップで選択`);
      return;
    }
    if (info.menuItemId === MENU.DETECT) {
      if (tabId == null) return;
      try {
        const res = await chrome.tabs.sendMessage(tabId, { type: 'rescan' });
        if (res?.items) addMediaItems(tabId, res.items, res.pageUrl || tab.url);
      } catch {
        /* content script may be missing on restricted pages */
      }
      const n = listTabMedia(tabId).length;
      notify('vdl detect', `${n} media URL(s) on this tab`);
      return;
    }
    if (info.menuItemId === MENU.OPEN_UI) {
      if (tabId == null) return;
      try {
        const res = await chrome.tabs.sendMessage(tabId, { type: 'rescan' });
        if (res?.items) addMediaItems(tabId, res.items, res.pageUrl || tab.url);
      } catch {
        /* ignore */
      }
      const r = await openConsoleWithMedia(tabId, tab?.url, { tab: 'candidates' });
      notify('vdl console', `opened with ${r.count} item(s)`);
    }
  } catch (e) {
    notify('vdl error', e.message || String(e));
    console.error(e);
  }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      if (msg?.type === 'mediaFound') {
        const tabId = sender.tab?.id;
        if (tabId != null) {
          addMediaItems(tabId, msg.items, msg.pageUrl || sender.tab?.url);
        }
        sendResponse({ ok: true });
        return;
      }
      if (msg?.type === 'getDetected') {
        const tabId = msg.tabId ?? sender.tab?.id;
        sendResponse({
          ok: true,
          items: tabId != null ? listTabMedia(tabId) : [],
          tabId,
        });
        return;
      }
      if (msg?.type === 'clearDetected') {
        const tabId = msg.tabId;
        if (tabId != null) {
          tabMedia.delete(tabId);
          await updateBadge(tabId);
        }
        sendResponse({ ok: true });
        return;
      }
      if (msg?.type === 'rescanTab') {
        const tabId = msg.tabId;
        if (tabId == null) throw new Error('tabId required');
        let items = [];
        try {
          const res = await chrome.tabs.sendMessage(tabId, { type: 'rescan' });
          items = res?.items || [];
          addMediaItems(tabId, items, res?.pageUrl);
        } catch (e) {
          // try inject content script once
          try {
            await chrome.scripting.executeScript({
              target: { tabId, allFrames: true },
              files: ['content.js'],
            });
            const res = await chrome.tabs.sendMessage(tabId, { type: 'rescan' });
            items = res?.items || [];
            addMediaItems(tabId, items, res?.pageUrl);
          } catch (e2) {
            throw new Error(
              `このページでは検出できません: ${e2.message || e.message || e}`,
            );
          }
        }
        sendResponse({ ok: true, items: listTabMedia(tabId) });
        return;
      }
      if (msg?.type === 'openConsole') {
        const tabId = msg.tabId;
        if (tabId == null) throw new Error('tabId required');
        const r = await openConsoleWithMedia(tabId, msg.pageUrl, {
          tab: msg.tab || 'candidates',
        });
        sendResponse({ ok: true, ...r });
        return;
      }
      if (msg?.type === 'openConsoleQueue') {
        const settings = await getSettings();
        await chrome.tabs.create({
          url: `${settings.uiBase}/?tab=queue`,
        });
        sendResponse({ ok: true });
        return;
      }
      if (msg?.type === 'enqueue') {
        const r = await enqueueUrl(msg.url, { quality: msg.quality });
        sendResponse({ ok: true, result: r });
        return;
      }
      if (msg?.type === 'enqueueBatch') {
        const settings = await getSettings();
        const r = await api.enqueueBatch(uniqueMediaItems((msg.urls || []).map((url) => ({ url }))).map((m) => m.url), {
          quality: msg.quality || settings.quality,
        });
        notify('vdl batch', `enqueued ${(r.jobs || []).length} job(s)`);
        sendResponse({ ok: true, result: r });
        return;
      }
      if (msg?.type === 'candidates') {
        const r = await api.candidates(msg.url);
        await chrome.storage.session.set({
          lastCandidates: {
            page_url: r.page_url || msg.url,
            candidates: uniqueMediaItems(r.candidates || []),
            at: Date.now(),
          },
        });
        sendResponse({ ok: true, result: r });
        return;
      }
      if (msg?.type === 'status') {
        const [health, status, queue] = await Promise.all([
          api.health().catch(() => null),
          api.status().catch(() => null),
          api.queue({ limit: 12 }).catch(() => null),
        ]);
        sendResponse({ ok: true, health, status, queue });
        return;
      }
      sendResponse({ ok: false, error: 'unknown message' });
    } catch (e) {
      sendResponse({ ok: false, error: e.message || String(e) });
    }
  })();
  return true;
});

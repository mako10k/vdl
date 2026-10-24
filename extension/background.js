/**
 * vdl service worker — media detection, context menus, enqueue, UI bridge.
 */
import { api, getSettings } from './api.js';
import { Capture } from './capture.js';
import { classifyFromContentType, classifyMediaUrl, rankKind } from './media.js';

const MENU = {
  PAGE: 'vdl-enqueue-page',
  LINK: 'vdl-enqueue-link',
  MEDIA: 'vdl-enqueue-media',
  SCRAPE: 'vdl-scrape-page',
  DETECT: 'vdl-detect-page',
  OPEN_UI: 'vdl-open-console',
};

const capture = new Capture({
  storage: chrome.storage,
  check: (urls) => api.submitted(urls),
  submit: async (urls, quality) => {
    const settings = await getSettings();
    return api.enqueueBatch(urls, { quality: quality || settings.quality });
  },
  changed: async (tabId) => {
    await updateBadge(tabId);
    chrome.runtime.sendMessage({ type: 'captureChanged' }).catch(() => {});
  },
  failed: (error) => notify('vdl auto capture', error.message || String(error)),
});

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

async function addMediaItems(tabId, items, pageUrl) {
  const media = (items || []).map((m) => ({ ...m, kind: m.kind || classifyMediaUrl(m.url) }))
    .filter((m) => m.kind);
  await capture.add(tabId, media, pageUrl);
}

function listTabMedia(tabId) {
  return capture.list(tabId).sort(
    (a, b) => rankKind(a.kind) - rankKind(b.kind) || a.url.localeCompare(b.url));
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

async function fetchCandidates(url) {
  const result = await api.candidates(url);
  await capture.checkSubmitted((result.candidates || []).map((m) => m.url));
  const candidates = capture.filter(result.candidates || []);
  return { ...result, candidates, count: candidates.length };
}

async function enqueueUrl(url, { quality } = {}) {
  if (!url || !/^https?:\/\//i.test(url)) {
    throw new Error(`http(s) URL のみ対応: ${url || '(empty)'}`);
  }
  const settings = await getSettings();
  const q = quality || settings.quality || 'best';
  try {
    const batch = await capture.enqueue([url], q);
    const r = { ...batch, job: batch.jobs?.[0] };
    const jobId = r?.job?.id;
    if (!jobId) return r;
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
  void capture.clear(tabId);
});

chrome.tabs.onActivated.addListener(({ tabId }) => {
  void capture.ready.then(() => updateBadge(tabId));
});

// Network capture (HLS / progressive media)
chrome.webRequest.onCompleted.addListener(
  async (details) => {
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
    await addMediaItems(
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
    await capture.ready;
    const tabId = tab?.id;
    if (info.menuItemId === MENU.PAGE) {
      const url = tab?.url || info.pageUrl;
      if (tabId != null) {
        try {
          const res = await chrome.tabs.sendMessage(tabId, { type: 'rescan' });
          if (res?.items) await addMediaItems(tabId, res.items, res.pageUrl || url);
        } catch {
          /* ignore */
        }
        if (listTabMedia(tabId).length) {
          notify('vdl', `検出 ${listTabMedia(tabId).length} 件 — ポップアップで選択`);
          return;
        }
      }
      if (url) {
        const r = await fetchCandidates(url);
        const candidates = capture.filter(r.candidates || []);
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
      const r = await fetchCandidates(url);
      await chrome.storage.session.set({
        lastCandidates: {
          page_url: r.page_url || url,
          candidates: capture.filter(r.candidates || []),
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
        if (res?.items) await addMediaItems(tabId, res.items, res.pageUrl || tab.url);
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
        if (res?.items) await addMediaItems(tabId, res.items, res.pageUrl || tab.url);
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
      await capture.ready;
      if (msg?.type === 'mediaFound') {
        const tabId = sender.tab?.id;
        if (tabId != null) {
          await addMediaItems(tabId, msg.items, msg.pageUrl || sender.tab?.url);
        }
        sendResponse({ ok: true });
        return;
      }
      if (msg?.type === 'getDetected') {
        const tabId = msg.tabId ?? sender.tab?.id;
        if (tabId != null) {
          await capture.checkSubmitted(capture.list(tabId).map((m) => m.url)).catch(() => {});
        }
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
          await capture.clear(tabId);
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
          await addMediaItems(tabId, items, res?.pageUrl);
        } catch (e) {
          // try inject content script once
          try {
            await chrome.scripting.executeScript({
              target: { tabId, allFrames: true },
              files: ['content.js'],
            });
            const res = await chrome.tabs.sendMessage(tabId, { type: 'rescan' });
            items = res?.items || [];
            await addMediaItems(tabId, items, res?.pageUrl);
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
        const r = await capture.enqueue(msg.urls || [], msg.quality || settings.quality);
        notify('vdl batch', `enqueued ${(r.jobs || []).length} job(s)`);
        sendResponse({ ok: true, result: r });
        return;
      }
      if (msg?.type === 'candidates') {
        const r = await fetchCandidates(msg.url);
        await chrome.storage.session.set({
          lastCandidates: {
            page_url: r.page_url || msg.url,
            candidates: capture.filter(r.candidates || []),
            at: Date.now(),
          },
        });
        sendResponse({ ok: true, result: r });
        return;
      }
      if (msg?.type === 'getCaptureState') {
        sendResponse({ ok: true, enabled: capture.enabled, submitted: [...capture.submitted] });
        return;
      }
      if (msg?.type === 'setAutoCapture') {
        const enabled = await capture.setEnabled(msg.enabled);
        sendResponse({ ok: true, enabled });
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

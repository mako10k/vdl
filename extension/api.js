/** Shared client for local vdl FastAPI (default http://127.0.0.1:18765). */

const DEFAULTS = {
  apiBase: 'http://127.0.0.1:18765',
  uiBase: 'http://127.0.0.1:18766',
  quality: 'best',
};

export async function getSettings() {
  const stored = await chrome.storage.sync.get(DEFAULTS);
  return {
    apiBase: (stored.apiBase || DEFAULTS.apiBase).replace(/\/$/, ''),
    uiBase: (stored.uiBase || DEFAULTS.uiBase).replace(/\/$/, ''),
    quality: stored.quality || DEFAULTS.quality,
  };
}

export async function setSettings(partial) {
  await chrome.storage.sync.set(partial);
}

async function req(path, init = {}) {
  const { apiBase } = await getSettings();
  const url = `${apiBase}${path.startsWith('/') ? path : `/${path}`}`;
  let res;
  try {
    res = await fetch(url, {
      signal: AbortSignal.timeout(15000),
      ...init,
      headers: {
        'Content-Type': 'application/json',
        ...(init.headers || {}),
      },
    });
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    throw new Error(
      `vdl API に接続できません (${apiBase}). バックエンド起動を確認: ${msg}`,
    );
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail || JSON.stringify(j);
    } catch {
      /* ignore */
    }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  if (res.status === 204) return null;
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) return res.json();
  return res.text();
}

export const api = {
  submitted: (urls) => req('/api/submitted-urls', {
    method: 'POST', body: JSON.stringify({ urls }),
  }),
  health: () => req('/api/health'),
  status: () => req('/api/status'),
  queue: (params = {}) => {
    const q = new URLSearchParams();
    if (params.all_done) q.set('all_done', 'true');
    if (params.limit) q.set('limit', String(params.limit));
    const s = q.toString();
    return req(`/api/queue${s ? `?${s}` : ''}`);
  },
  enqueue: (body) =>
    req('/api/enqueue', { method: 'POST', body: JSON.stringify(body) }),
  enqueueBatch: (urls, opts = {}) =>
    req('/api/enqueue/batch', {
      method: 'POST',
      body: JSON.stringify({ urls, ...opts }),
    }),
  candidates: (url) =>
    req('/api/candidates', {
      method: 'POST',
      body: JSON.stringify({ url }),
    }),
  registry: () => req('/api/registry'),
  bridgePush: (body) =>
    req('/api/bridge/push', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  bridgePull: (token, consume = false) => {
    const q = new URLSearchParams({ token });
    if (consume) q.set('consume', 'true');
    return req(`/api/bridge/pull?${q}`);
  },
};

export { DEFAULTS };

import { getSettings } from './api.js';

const $ = (id) => document.getElementById(id);

let activeTabId = null;
let pageUrl = '';
/** @type {Array<{url:string,kind:string,source:string,title?:string}>} */
let detected = [];
/** @type {Array<object>} */
let candidates = [];

function showMsg(text) {
  const el = $('msg');
  el.textContent = text;
  el.classList.remove('hidden');
  $('err').classList.add('hidden');
}

function showErr(text) {
  const el = $('err');
  el.textContent = text;
  el.classList.remove('hidden');
  $('msg').classList.add('hidden');
}

function clearFlash() {
  $('msg').classList.add('hidden');
  $('err').classList.add('hidden');
}

function send(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (res) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false, error: chrome.runtime.lastError.message });
        return;
      }
      resolve(res || { ok: false, error: 'no response' });
    });
  });
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, '&quot;');
}

function renderJobs(queue) {
  const root = $('jobs');
  const jobs = queue?.jobs || [];
  if (!jobs.length) {
    root.innerHTML = '<div class="empty">ジョブなし</div>';
    return;
  }
  root.innerHTML = jobs
    .map(
      (j) => `
    <div class="job">
      <span>#${j.id}</span>
      <span class="status ${j.status}">${j.status}</span>
      <span class="progress" title="${escapeAttr(j.progress_label || '')}">${escapeHtml(
        j.kind + (j.progress_label ? ' · ' + j.progress_label : ''),
      )}</span>
    </div>`,
    )
    .join('');
}

function renderDetected(items) {
  detected = items || [];
  $('detectMeta').textContent = detected.length ? `${detected.length} 件` : '';
  const list = $('detectList');
  $('btnEnqueueDetected').disabled = !detected.length;
  if (!detected.length) {
    list.innerHTML =
      '<div class="empty">未検出 — 動画を再生してから「再検出」、またはネットワーク待ち</div>';
    return;
  }
  list.innerHTML = detected
    .map((m, i) => {
      const skip = m.kind === 'dash';
      return `
      <label class="cand ${skip ? 'done' : ''}">
        <input type="checkbox" data-di="${i}" ${skip ? 'disabled' : 'checked'} />
        <div>
          <div class="url"><strong>[${escapeHtml(m.kind)}]</strong> ${escapeHtml(m.url)}</div>
          <div class="meta">${escapeHtml(m.source || '')}${
            m.title ? ' · ' + escapeHtml(m.title) : ''
          }${skip ? ' · DASH 未対応' : ''}</div>
        </div>
      </label>`;
    })
    .join('');
}

function isDefaultSelected(c) {
  if (c.selected_by_default != null) return !!c.selected_by_default;
  return !c.downloaded && !c.known && !c.queued;
}

function candidateStateLabel(c) {
  if (c.state) return c.state;
  if (c.downloaded) return `済 id=${c.download_id}`;
  if (c.queued) return 'キュー中';
  if (c.known) return `登録 id=${c.download_id || '?'}`;
  return '新規';
}

function renderCandidates(data) {
  const section = $('candSection');
  candidates = data?.candidates || [];
  if (!candidates.length) {
    section.classList.add('hidden');
    return;
  }
  section.classList.remove('hidden');
  const nNew = candidates.filter(isDefaultSelected).length;
  $('candMeta').textContent = `${candidates.length} (新規 ${nNew}) · ${data.page_url || ''}`;
  const list = $('candList');
  list.innerHTML = candidates
    .map((c) => {
      const def = isDefaultSelected(c);
      const past = !def;
      return `
      <label class="cand ${past ? 'done' : ''}">
        <input type="checkbox" data-idx="${c.index}" ${def ? 'checked' : ''} />
        <div>
          <div class="url"><strong>#${c.index}</strong> [${escapeHtml(c.kind)}] ${escapeHtml(
            c.url,
          )}</div>
          <div class="meta">${escapeHtml(candidateStateLabel(c))} · ${escapeHtml(
            c.source || '',
          )}${c.title ? ' · ' + escapeHtml(c.title) : ''}</div>
        </div>
      </label>`;
    })
    .join('');
}

async function refreshStatus() {
  const res = await send({ type: 'status' });
  const conn = $('conn');
  if (!res.ok || !res.health) {
    conn.textContent = 'API offline';
    conn.className = 'badge off';
    if (res.error) showErr(res.error);
    return;
  }
  conn.textContent = 'API ok';
  conn.className = 'badge on';

  const st = res.status || {};
  const sup = st.supervisor?.running;
  const worker = st.worker?.running;
  $('supBadge').textContent = sup ? 'sup · on' : 'sup · off';
  $('supBadge').className = `badge ${sup ? 'on' : 'off'}`;
  $('workerBadge').textContent = worker ? 'worker · on' : 'worker · off';
  $('workerBadge').className = `badge ${worker ? 'on' : 'off'}`;

  const counts = st.queue_counts || res.queue?.counts || {};
  const parts = Object.entries(counts)
    .filter(([, v]) => v)
    .map(([k, v]) => `${k}=${v}`);
  $('countsBadge').textContent = parts.length ? parts.join(' ') : 'queue empty';
  renderJobs(res.queue);
}

async function loadDetected() {
  if (activeTabId == null) {
    renderDetected([]);
    return;
  }
  const res = await send({ type: 'getDetected', tabId: activeTabId });
  if (res.ok) renderDetected(res.items || []);
}

async function onDetect() {
  clearFlash();
  if (activeTabId == null) return showErr('タブを取得できません');
  $('btnDetect').disabled = true;
  try {
    const res = await send({ type: 'rescanTab', tabId: activeTabId });
    if (!res.ok) throw new Error(res.error || 'detect failed');
    renderDetected(res.items || []);
    showMsg(`検出 ${(res.items || []).length} 件`);
  } catch (e) {
    showErr(e.message || String(e));
  } finally {
    $('btnDetect').disabled = false;
  }
}

async function onClearDetect() {
  if (activeTabId == null) return;
  await send({ type: 'clearDetected', tabId: activeTabId });
  renderDetected([]);
}

async function onOpenConsole() {
  clearFlash();
  if (activeTabId == null) return showErr('タブなし');
  // ensure latest scan
  await send({ type: 'rescanTab', tabId: activeTabId }).catch(() => null);
  const res = await send({
    type: 'openConsole',
    tabId: activeTabId,
    pageUrl,
    tab: 'candidates',
  });
  if (!res.ok) return showErr(res.error || 'open failed');
  showMsg(`Console を開きました (${res.count} 件)`);
  window.close();
}

function selectedDetectedUrls() {
  const boxes = [...document.querySelectorAll('#detectList input[type=checkbox]:checked')];
  return boxes
    .map((b) => detected[Number(b.dataset.di)])
    .filter((m) => m && m.kind !== 'dash')
    .map((m) => m.url);
}

async function onEnqueueDetected() {
  clearFlash();
  const urls = selectedDetectedUrls();
  if (!urls.length) return showErr('選択がありません（DASH は未対応）');
  $('btnEnqueueDetected').disabled = true;
  try {
    const res = await send({
      type: 'enqueueBatch',
      urls,
      quality: $('quality').value,
      openQueue: true,
    });
    if (!res.ok) throw new Error(res.error || 'batch failed');
    showMsg(`enqueued ${(res.result.jobs || []).length} job(s) → Console キュー`);
    await refreshStatus();
  } catch (e) {
    showErr(e.message || String(e));
  } finally {
    $('btnEnqueueDetected').disabled = false;
  }
}

async function onEnqueue() {
  clearFlash();
  const url = $('url').value.trim();
  if (!url) return showErr('URL を入力してください');
  $('btnEnqueue').disabled = true;
  try {
    const res = await send({
      type: 'enqueue',
      url,
      quality: $('quality').value,
      openQueue: true,
    });
    if (!res.ok) throw new Error(res.error || 'enqueue failed');
    const id = res.result?.job?.id;
    showMsg(`queued job #${id}`);
    await refreshStatus();
  } catch (e) {
    showErr(e.message || String(e));
  } finally {
    $('btnEnqueue').disabled = false;
  }
}

async function onScrape() {
  clearFlash();
  const url = $('url').value.trim();
  if (!url) return showErr('URL を入力してください');
  $('btnScrape').disabled = true;
  try {
    const res = await send({ type: 'candidates', url });
    if (!res.ok) throw new Error(res.error || 'scrape failed');
    renderCandidates(res.result);
    showMsg(`${res.result.count ?? res.result.candidates?.length ?? 0} candidate(s)`);
  } catch (e) {
    showErr(e.message || String(e));
  } finally {
    $('btnScrape').disabled = false;
  }
}

async function onEnqueueSelected() {
  clearFlash();
  const boxes = [...document.querySelectorAll('#candList input[type=checkbox]:checked')];
  const idxs = new Set(boxes.map((b) => Number(b.dataset.idx)));
  const urls = candidates.filter((c) => idxs.has(c.index) && !c.downloaded).map((c) => c.url);
  if (!urls.length) return showErr('選択がありません');
  $('btnEnqueueSelected').disabled = true;
  try {
    const res = await send({
      type: 'enqueueBatch',
      urls,
      quality: $('quality').value,
      openQueue: true,
    });
    if (!res.ok) throw new Error(res.error || 'batch failed');
    showMsg(`enqueued ${(res.result.jobs || []).length} job(s)`);
    await refreshStatus();
  } catch (e) {
    showErr(e.message || String(e));
  } finally {
    $('btnEnqueueSelected').disabled = false;
  }
}

async function initTab() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab?.id != null) activeTabId = tab.id;
    if (tab?.url && /^https?:/i.test(tab.url)) {
      pageUrl = tab.url;
      $('url').value = tab.url;
    }
  } catch {
    /* ignore */
  }
  const sess = await chrome.storage.session.get(['lastCandidates', 'pendingScrapeUrl']);
  if (sess.pendingScrapeUrl && !$('url').value) {
    $('url').value = sess.pendingScrapeUrl;
  }
  if (sess.lastCandidates?.candidates?.length) {
    renderCandidates(sess.lastCandidates);
  }
}

async function boot() {
  const settings = await getSettings();
  $('quality').value = settings.quality || 'best';
  $('openUi').href = settings.uiBase || 'http://127.0.0.1:18766';
  $('openOpts').addEventListener('click', (e) => {
    e.preventDefault();
    chrome.runtime.openOptionsPage();
  });

  $('btnDetect').addEventListener('click', () => void onDetect());
  $('btnClearDetect').addEventListener('click', () => void onClearDetect());
  $('btnOpenConsole').addEventListener('click', () => void onOpenConsole());
  $('btnEnqueueDetected').addEventListener('click', () => void onEnqueueDetected());
  $('btnSelectDetectAll').addEventListener('click', () => {
    document.querySelectorAll('#detectList input[type=checkbox]').forEach((b) => {
      if (!b.disabled) b.checked = true;
    });
  });
  $('btnEnqueue').addEventListener('click', () => void onEnqueue());
  $('btnScrape').addEventListener('click', () => void onScrape());
  $('btnRefresh').addEventListener('click', () => void refreshStatus());
  $('btnOpenQueue').addEventListener('click', () => void send({ type: 'openConsoleQueue' }));
  $('btnEnqueueSelected').addEventListener('click', () => void onEnqueueSelected());
  $('btnSelectNew').addEventListener('click', () => {
    document.querySelectorAll('#candList input[type=checkbox]').forEach((b) => {
      const idx = Number(b.dataset.idx);
      const c = candidates.find((x) => x.index === idx);
      b.checked = c ? isDefaultSelected(c) : false;
    });
  });
  $('url').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') void onEnqueue();
  });

  await initTab();
  await loadDetected();
  // auto rescan once when popup opens
  if (activeTabId != null) {
    void send({ type: 'rescanTab', tabId: activeTabId }).then((res) => {
      if (res.ok) renderDetected(res.items || []);
    });
  }
  await refreshStatus();
}

boot();

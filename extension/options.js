import { DEFAULTS, getSettings, setSettings, api } from './api.js';

const $ = (id) => document.getElementById(id);

async function load() {
  const s = await getSettings();
  $('apiBase').value = s.apiBase || DEFAULTS.apiBase;
  $('uiBase').value = s.uiBase || DEFAULTS.uiBase;
  $('quality').value = s.quality || DEFAULTS.quality;
}

async function save() {
  const apiBase = $('apiBase').value.trim().replace(/\/$/, '') || DEFAULTS.apiBase;
  const uiBase = $('uiBase').value.trim().replace(/\/$/, '') || DEFAULTS.uiBase;
  const quality = $('quality').value || 'best';
  await setSettings({ apiBase, uiBase, quality });
  // host_permissions are fixed in manifest for default ports;
  // custom ports may need user to reload + optional host access.
  $('status').textContent = 'saved';
  setTimeout(() => {
    $('status').textContent = '';
  }, 1500);
}

async function test() {
  await save();
  $('status').textContent = 'testing…';
  try {
    const h = await api.health();
    $('status').textContent = h?.status === 'ok' ? 'API ok' : JSON.stringify(h);
  } catch (e) {
    $('status').textContent = e.message || String(e);
  }
}

$('save').addEventListener('click', () => void save());
$('test').addEventListener('click', () => void test());
load();

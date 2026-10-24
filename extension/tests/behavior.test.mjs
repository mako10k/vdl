import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mediaUrlKey, uniqueMediaItems } from '../media.js';

let messageHandler;
let menuHandler;
const opened = [];
const requests = [];
const session = {};
const badges = new Map();
const event = () => ({ addListener() {} });
globalThis.chrome = {
  runtime: { sendMessage: async () => {}, onInstalled: event(), onMessage: { addListener(fn) { messageHandler = fn; } } },
  contextMenus: { onClicked: { addListener(fn) { menuHandler = fn; } } },
  tabs: { onRemoved: event(), onActivated: event(),
    create: async (tab) => opened.push(tab),
    sendMessage: async () => ({ items: [] }) },
  action: { setBadgeText: async ({ tabId, text }) => badges.set(tabId, text), setBadgeBackgroundColor: async () => {} },
  webRequest: { onCompleted: event() },
  notifications: { create() {} },
  storage: { local: { get: async (defaults) => defaults, set: async () => {} }, sync: { get: async (defaults) => defaults },
    session: { get: async (defaults) => defaults, set: async (data) => Object.assign(session, data) } },
};
globalThis.fetch = async (url, init) => {
  const body = JSON.parse(init?.body || '{}');
  requests.push({ url, body });
  const data = url.endsWith('/api/candidates') ? {
    candidates: [{ url: 'https://example.test/a.mp4' }, { url: 'https://example.test/a.mp4#dup' }],
  } : url.endsWith('/api/submitted-urls') ? { urls: [] } : { jobs: (body.urls || []).map((url) => ({ id: 1, payload: { url } })) };
  return { ok: true, status: 200, headers: new Headers({ 'Content-Type': 'application/json' }),
    json: async () => data };
};
await import('../background.js');
const send = (msg) => new Promise((resolve) => messageHandler(msg, {}, resolve));

test('obvious duplicates collapse while signed URLs remain distinct', () => {
  assert.equal(mediaUrlKey('https://EXAMPLE.test:443/a.mp4#one'), 'https://example.test/a.mp4');
  const items = uniqueMediaItems([
    { url: 'https://example.test/a.mp4#one' }, { url: 'https://EXAMPLE.test:443/a.mp4#two' },
    { url: 'https://example.test/a.mp4?token=1' }, { url: 'https://example.test/a.mp4?token=2' },
    { url: 'https://example.test/A.mp4' }, { url: 'http://example.test/a.mp4' },
  ]);
  assert.equal(items.length, 5);
});

test('detection and batch enqueue deduplicate before display and submission', async () => {
  await send({ type: 'mediaFound', items: [] });
  await new Promise((resolve) => messageHandler({ type: 'mediaFound', items: [
    { url: 'https://example.test/a.mp4#one' }, { url: 'https://example.test/a.mp4#two' },
  ] }, { tab: { id: 1 } }, resolve));
  const detected = await send({ type: 'getDetected', tabId: 1 });
  assert.equal(detected.items.length, 1);
  assert.equal(badges.get(1), "1");
  const response = await send({ type: 'enqueueBatch', openQueue: true,
    urls: ['https://example.test/a.mp4', 'https://example.test/a.mp4#dup'] });
  assert.equal(response.ok, true);
  assert.equal(requests.at(-1).body.urls.length, 1);
  assert.equal(opened.length, 0);
  assert.equal((await send({ type: 'getDetected', tabId: 1 })).items.length, 0);
  assert.equal(badges.get(1), '');
});

test('enqueue and page extraction never open the Web console', async () => {
  assert.equal((await send({ type: 'enqueue', url: 'https://example.test/a.mp4', openQueue: true })).ok, true);
  await menuHandler({ menuItemId: 'vdl-enqueue-page' }, { id: 2, url: 'https://example.test/page' });
  assert.equal(session.lastCandidates.candidates.length, 0);
  assert.equal(opened.length, 0);
  assert.equal((await send({ type: 'openConsoleQueue' })).ok, true);
  assert.equal(opened.length, 1);
  assert.match(opened[0].url, /tab=queue/);
});


test('server candidates also hide previously submitted URLs', async () => {
  const result = await send({ type: 'candidates', url: 'https://example.test/page' });
  assert.equal(result.ok, true);
  assert.equal(result.result.candidates.length, 0);
  assert.equal(result.result.count, 0);
});

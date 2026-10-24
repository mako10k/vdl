import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Capture } from '../capture.js';

function fixture(options = {}) {
  const local = { submittedUrls: [], autoCapture: false, ...options.local };
  const session = { detectedTabs: [], ...options.session };
  const calls = [], errors = [], changes = [];
  const storage = Object.fromEntries([['local', local], ['session', session]].map(([key, state]) =>
    [key, { get: async (defaults) => ({ ...defaults, ...state }), set: async (values) => Object.assign(state, structuredClone(values)) }]));
  const capture = new Capture({ storage,
    submit: options.submit || (async (urls) => { calls.push(urls); return { jobs: urls.map((url) => ({ payload: { url } })) }; }),
    check: options.check || (async () => ({ urls: [] })),
    changed: async (id) => changes.push(id), failed: (error) => errors.push(error),
  });
  return { capture, local, session, calls, errors, changes, storage };
}
const item = (name) => ({ url: `https://example.test/${name}.mp4`, kind: 'video' });
const flush = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };

test('successful manual receipts disappear everywhere and survive worker restart', async () => {
  const f = fixture();
  await f.capture.add(1, [item('a'), item('b')]);
  await f.capture.add(2, [item('a')]);
  await f.capture.enqueue([item('a').url]);
  assert.deepEqual(f.capture.list(1), [{ ...item('b'), pageUrl: undefined }]);
  assert.equal(f.capture.list(2).length, 0);
  const restarted = new Capture({ ...f.capture, storage: f.storage });
  await restarted.ready;
  await restarted.add(3, [item('a')]);
  assert.equal(restarted.list(3).length, 0);
  assert.equal(restarted.list(1).length, 1);
});

test('auto mode waits one stable second and duplicate observations do not reset it', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture();
  await f.capture.setEnabled(true);
  await f.capture.add(1, [item('a')]);
  t.mock.timers.tick(700); await flush();
  await f.capture.add(1, [item('b')]);
  t.mock.timers.tick(700); await flush();
  assert.equal(f.calls.length, 0);
  await f.capture.add(1, [item('a'), item('b')]);
  t.mock.timers.tick(299); await flush();
  assert.equal(f.calls.length, 0);
  t.mock.timers.tick(1); await flush();
  assert.deepEqual(f.calls, [[item('a').url, item('b').url]]);
  assert.equal(f.capture.list(1).length, 0);
  assert.equal(f.local.autoCapture, true);
});

test('OFF cancels waiting captures and defaults to disabled', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture();
  await f.capture.add(1, [item('a')]);
  t.mock.timers.tick(2000); await flush();
  assert.equal(f.calls.length, 0);
  await f.capture.setEnabled(true);
  t.mock.timers.tick(999);
  await f.capture.setEnabled(false);
  t.mock.timers.tick(2000); await flush();
  assert.equal(f.calls.length, 0);
  assert.equal(f.capture.list(1).length, 1);
});

test('simultaneous manual and automatic captures submit a URL once across tabs', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture();
  await f.capture.setEnabled(true);
  await f.capture.add(1, [item('a')]);
  await f.capture.add(2, [item('a')]);
  t.mock.timers.tick(1000);
  await Promise.all([f.capture.enqueue([item('a').url]), flush()]);
  await f.capture.serial;
  assert.equal(f.calls.length, 1);
  assert.equal(f.capture.list(1).length + f.capture.list(2).length, 0);
});

test('failed and partially skipped submissions keep unsent items visible', async () => {
  let fail = true;
  const f = fixture({ submit: async () => {
    if (fail) throw Error('offline');
    return { jobs: [{ payload: { url: item('a').url } }], skipped_html: [item('b').url] };
  }});
  await f.capture.add(1, [item('a'), item('b')]);
  await assert.rejects(f.capture.enqueue([item('a').url, item('b').url]), /offline/);
  assert.equal(f.capture.list(1).length, 2);
  fail = false;
  await f.capture.enqueue([item('a').url, item('b').url]);
  assert.equal(f.capture.list(1)[0].url, item('b').url);
});

test('backend submission history is excluded before capture, unsupported DASH is never auto queued', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture({ check: async (urls) => ({ urls: urls.filter((url) => url === item('a').url) }) });
  await f.capture.setEnabled(true);
  await f.capture.add(1, [item('a'), { url: 'https://example.test/a.mpd', kind: 'dash' }]);
  assert.equal(f.capture.list(1).length, 1);
  t.mock.timers.tick(1000); await flush();
  assert.equal(f.calls.length, 0);
});

test('worker restoration reschedules stable pending detections', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture({ local: { autoCapture: true }, session: { detectedTabs: [[7, [item('a')]]] } });
  await f.capture.ready;
  t.mock.timers.tick(1000); await flush();
  assert.equal(f.calls.length, 1);
});

test('offline history check prevents automatic submission and keeps detection visible', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let attempts = 0;
  const f = fixture({ check: async () => { attempts++; throw Error('history offline'); } });
  await f.capture.setEnabled(true);
  await f.capture.add(1, [item('a')]);
  t.mock.timers.tick(1000); await flush();
  assert.equal(f.calls.length, 0);
  assert.equal(f.capture.list(1).length, 1);
  assert.equal(f.errors.length, 1);
  assert.ok(attempts >= 2);
});

test('clearing or closing a tab cancels its waiting capture', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture();
  await f.capture.setEnabled(true);
  await f.capture.add(1, [item('a')]);
  await f.capture.clear(1);
  t.mock.timers.tick(1000); await flush();
  assert.equal(f.calls.length, 0);
});

import { mediaUrlKey, uniqueMediaItems } from './media.js';

/** Submission receipts survive browser restarts; detections survive worker suspension. */
export class Capture {
  constructor({ storage, submit, check, changed, failed, delay = 1000 }) {
    Object.assign(this, { storage, submit, check, changed, failed, delay });
    this.tabs = new Map();
    this.submitted = new Set();
    this.timers = new Map();
    this.enabled = false;
    this.serial = Promise.resolve();
    this.ready = this.restore();
  }

  async restore() {
    const [local, session] = await Promise.all([
      this.storage.local.get({ submittedUrls: [], autoCapture: false }),
      this.storage.session.get({ detectedTabs: [] }),
    ]);
    this.submitted = new Set(local.submittedUrls);
    this.enabled = local.autoCapture === true;
    for (const [id, items] of session.detectedTabs) {
      this.tabs.set(id, new Map(items.map((m) => [mediaUrlKey(m.url), m])));
      if (this.enabled) this.schedule(id);
    }
  }

  list(id) {
    return [...(this.tabs.get(id)?.values() || [])].filter((m) =>
      !this.submitted.has(mediaUrlKey(m.url)) && !m.queued && !m.downloaded);
  }

  filter(items = []) {
    return uniqueMediaItems(items).filter((m) =>
      !this.submitted.has(mediaUrlKey(m.url)) && !m.queued && !m.downloaded);
  }

  async checkSubmitted(urls) {
    const before = this.submitted.size;
    for (let offset = 0; offset < urls.length; offset += 1000) {
      const result = await this.check(urls.slice(offset, offset + 1000));
      for (const url of result.urls || []) this.submitted.add(mediaUrlKey(url));
    }
    await this.storage.local.set({ submittedUrls: [...this.submitted] });
    if (before !== this.submitted.size) {
      for (const id of this.tabs.keys()) await this.changed(id);
    }
  }

  async saveTabs() {
    await this.storage.session.set({ detectedTabs: [...this.tabs].map(([id, map]) => [id, [...map.values()]]) });
  }

  async add(id, items, pageUrl) {
    await this.ready;
    if (id == null || id < 0) return;
    if (!this.tabs.has(id)) this.tabs.set(id, new Map());
    const map = this.tabs.get(id);
    let added = false;
    const fresh = this.filter(items).filter((m) => !map.has(mediaUrlKey(m.url)));
    try {
      await this.checkSubmitted(fresh.map((m) => m.url));
    } catch {
      // Show detections while offline; enqueue checks again and fails closed.
    }
    for (const m of this.filter(fresh)) {
      const key = mediaUrlKey(m.url);
      if (!map.has(key)) {
        map.set(key, { ...m, pageUrl });
        added = true;
      }
    }
    if (added) {
      await this.saveTabs();
      await this.changed(id);
      if (this.enabled) this.schedule(id);
    }
  }

  schedule(id) {
    clearTimeout(this.timers.get(id));
    this.timers.set(id, setTimeout(() => {
      this.timers.delete(id);
      if (!this.enabled) return;
      const urls = this.list(id).filter((m) => ['video', 'm3u8', 'audio'].includes(m.kind)).map((m) => m.url);
      if (urls.length) void this.enqueue(urls, undefined, true).catch((e) => this.failed(e));
    }, this.delay));
  }

  async setEnabled(enabled) {
    await this.ready;
    this.enabled = enabled === true;
    await this.storage.local.set({ autoCapture: this.enabled });
    for (const timer of this.timers.values()) clearTimeout(timer);
    this.timers.clear();
    if (this.enabled) for (const id of this.tabs.keys()) this.schedule(id);
    return this.enabled;
  }

  enqueue(urls, quality, automatic = false) {
    const task = this.serial.then(async () => {
      await this.ready;
      if (automatic && !this.enabled) return { jobs: [] };
      await this.checkSubmitted(urls);
      if (automatic && !this.enabled) return { jobs: [] };
      const fresh = this.filter(urls.map((url) => ({ url })));
      if (!fresh.length) {
        for (const id of this.tabs.keys()) await this.changed(id);
        return { jobs: [] };
      }
      const result = await this.submit(fresh.map((m) => m.url), quality);
      // Only actual job receipts count, never skipped URLs or failed requests.
      for (const job of result.jobs || []) {
        const url = job.payload?.url;
        if (url) this.submitted.add(mediaUrlKey(url));
      }
      await this.storage.local.set({ submittedUrls: [...this.submitted] });
      for (const [id, map] of this.tabs) {
        for (const key of map.keys()) if (this.submitted.has(key)) map.delete(key);
        await this.changed(id);
      }
      await this.saveTabs();
      return result;
    });
    this.serial = task.catch(() => {});
    return task;
  }

  async clear(id) {
    await this.ready;
    clearTimeout(this.timers.get(id));
    this.timers.delete(id);
    this.tabs.delete(id);
    await this.saveTabs();
    await this.changed(id);
  }
}

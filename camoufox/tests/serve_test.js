// serve.js's cookie import and context housekeeping, against a fake
// Playwright context that behaves as Firefox does: it refuses some cookies
// with an error, silently drops one whose expiry lies more than 400 days
// ahead, and keeps one cookie per (name, domain, path).
//
// Run by tests/test_serve_js.py on the playwright package's own node.
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('path');

const serve = require(path.join(__dirname, '..', 'serve.js'));

const DAY = 24 * 3600;

class FakeContext {
  constructor(jar = []) {
    this.jar = jar.map((c) => ({ path: '/', ...c }));
    this.batches = 0;
  }
  async cookies() {
    return this.jar.map((c) => ({ ...c }));
  }
  async addCookies(list) {
    this.batches++;
    // all or nothing, as Playwright's addCookies is
    for (const c of list) {
      if (!c.name) throw new Error('Cookie should have a name');
      if ((c.name.length + String(c.value || '').length) > 4096) throw new Error('Cookie is too big');
    }
    const now = Date.now() / 1000;
    for (const c of list) {
      if (typeof c.expires === 'number' && c.expires > now + 400 * DAY) continue; // Firefox: silently gone
      let domain = c.domain;
      let p = c.path || '/';
      if (!domain && c.url) {
        const u = new URL(c.url);
        domain = u.hostname;
        p = c.path || u.pathname.slice(0, u.pathname.lastIndexOf('/') + 1) || '/';
      }
      const row = { name: c.name, value: String(c.value || ''), domain, path: p, expires: c.expires === undefined ? -1 : c.expires };
      this.jar = this.jar.filter((x) => !(x.name === row.name && x.domain === row.domain && x.path === row.path));
      this.jar.push(row);
    }
  }
}

test('a cookie far in the future is capped to 400 days and kept', async () => {
  const ctx = new FakeContext();
  const far = Math.floor(Date.now() / 1000) + 3000 * DAY;
  const r = await serve.addCookies(ctx, { cookies: [{ name: 'a', value: '1', domain: '.shop.test', path: '/', expires: far }] });
  assert.deepStrictEqual(r, { added: 1, dropped: 0, skipped: 0 });
  const kept = ctx.jar.find((c) => c.name === 'a');
  assert.ok(kept && kept.expires <= Date.now() / 1000 + 400 * DAY);
  // without the cap Firefox would have dropped it silently
  assert.ok(serve.clean({ expires: far }, Math.floor(Date.now() / 1000)).expires < far);
});

test('Chromium-only keys and nulls are dropped before Firefox sees them', () => {
  const c = serve.clean({ name: 'a', value: '1', domain: 'x', partitionKey: 'k', sourceScheme: 'Secure', sourcePort: 443, sameSite: null }, 0);
  assert.deepStrictEqual(c, { name: 'a', value: '1', domain: 'x' });
});

test('a refused cookie is dropped even when an older one of the same name is there', async () => {
  const ctx = new FakeContext([{ name: 'sid', value: 'old', domain: 'shop.test', path: '/' }]);
  const big = 'x'.repeat(5000);
  const r = await serve.addCookies(ctx, { cookies: [{ name: 'sid', value: big, domain: 'shop.test', path: '/' }] });
  assert.deepStrictEqual(r, { added: 0, dropped: 1, skipped: 0 });
  assert.strictEqual(ctx.jar.find((c) => c.name === 'sid').value, 'old');
  // the same with the old cookie at "/" and the import at another path
  const ctx2 = new FakeContext([{ name: 'sid', value: 'old', domain: 'shop.test', path: '/' }]);
  const r2 = await serve.addCookies(ctx2, { cookies: [{ name: 'sid', value: big, domain: 'shop.test', path: '/a/' }] });
  assert.deepStrictEqual(r2, { added: 0, dropped: 1, skipped: 0 });
});

test('the batch falls back to one by one; each refusal is counted', async () => {
  const ctx = new FakeContext();
  const r = await serve.addCookies(ctx, {
    cookies: [
      { name: 'a', value: '1', domain: 'shop.test', path: '/' },
      { name: '', value: '2', domain: 'shop.test', path: '/' },
      { name: 'b', value: '3', url: 'https://shop.test/x/y' },
      null,
    ],
  });
  assert.deepStrictEqual(r, { added: 2, dropped: 2, skipped: 0 });
  assert.ok(ctx.batches >= 2);
  assert.deepStrictEqual(ctx.jar.find((c) => c.name === 'b').path, '/x/');
});

test('an overwrite with the same value counts as added; a new value replaces the old', async () => {
  const ctx = new FakeContext([{ name: 'sid', value: 'v', domain: 'shop.test', path: '/' }]);
  assert.deepStrictEqual(await serve.addCookies(ctx, { cookies: [{ name: 'sid', value: 'v', domain: 'shop.test', path: '/' }] }),
    { added: 1, dropped: 0, skipped: 0 });
  assert.deepStrictEqual(await serve.addCookies(ctx, { cookies: [{ name: 'sid', value: 'w', domain: 'shop.test', path: '/' }] }),
    { added: 1, dropped: 0, skipped: 0 });
  assert.strictEqual(ctx.jar.find((c) => c.name === 'sid').value, 'w');
  // a domain cookie (".shop.test") is matched without its dot
  const dot = new FakeContext();
  assert.deepStrictEqual(await serve.addCookies(dot, { cookies: [{ name: 'd', value: '1', domain: '.shop.test', path: '/' }] }),
    { added: 1, dropped: 0, skipped: 0 });
});

test('skipExisting leaves a cookie the browser already has as it is', async () => {
  const ctx = new FakeContext([{ name: 's', value: 'live', domain: 'shop.test', path: '/' }]);
  const r = await serve.addCookies(ctx, {
    skipExisting: true,
    cookies: [{ name: 's', value: 'saved', domain: 'shop.test', path: '/' }, { name: 't', value: 'saved', domain: 'shop.test', path: '/' }],
  });
  assert.deepStrictEqual(r, { added: 1, dropped: 0, skipped: 1 });
  assert.strictEqual(ctx.jar.find((c) => c.name === 's').value, 'live');
});

class FakeBrowserContext {
  constructor(pages) {
    this._pages = pages;
    this.closed = false;
  }
  pages() {
    return this.closed ? [] : new Array(this._pages);
  }
  async close() {
    this.closed = true;
  }
}

test('housekeeping never touches the default context and closes idle client contexts', async () => {
  const def = new FakeBrowserContext(0);
  const busy = new FakeBrowserContext(1);
  const idle = new FakeBrowserContext(0);
  // first seen page-less: it gets its idle time
  let r = await serve.prune([def, busy, idle], { idleSeconds: 0.02 });
  assert.strictEqual(r.closed, 0);
  await new Promise((ok) => setTimeout(ok, 40));
  r = await serve.prune([def, busy, idle], { idleSeconds: 0.02 });
  assert.strictEqual(r.closed, 1);
  assert.ok(idle.closed && !busy.closed && !def.closed);
  // within the idle time a page-less context stays
  const fresh = new FakeBrowserContext(0);
  r = await serve.prune([def, fresh], { idleSeconds: 60 });
  assert.strictEqual(r.closed, 0);
  // no client for a while: every client context goes, the default one stays
  r = await serve.prune([def, busy, fresh], { idleSeconds: 60, closeAll: true });
  assert.strictEqual(r.closed, 2);
  assert.ok(!def.closed && busy.closed && fresh.closed);
});

// The Camoufox browser's Playwright server, and the launcher's one client.
//
//   node serve.js <playwright driver package dir>
//
// Runs on the node and the playwright-core that the playwright Python
// package ships (the launcher's own Playwright; no second driver). It:
//
// 1. reads ONE JSON line on stdin: {"options": <firefox.launchServer options>}
//    (a persistent, shared browser: _userDataDir + _sharedBrowser);
// 2. launches the server and connects one in-process client to it;
// 3. writes {"event":"listening","port","wsPath","pid","version"} on stdout;
// 4. then answers JSON-line requests {"id","cmd","args"} with
//    {"id","ok":true,"result"} or {"id","ok":false,"error"}:
//      ping            contexts[0].cookies() (answered by Firefox's parent
//                      process, so a silent browser shows here)
//      cookies.get     contexts[0].cookies()
//      cookies.add     {cookies, skipExisting?} -> {added, dropped}: all at
//                      once, else one by one (Firefox refuses some cookies
//                      Chromium takes); each one refused is counted
//      contexts.prune  {idleSeconds, closeAll} -> {closed}: closes every
//                      context but the default one that has had no page for
//                      idleSeconds, or all of them with closeAll
//      close           closes the server (and Firefox) and exits
// 5. writes {"event":"closed"} and exits when the browser goes away (it
//    crashed, or a client closed the default context).
//
// stdout carries ONLY these lines: every log goes to stderr.

'use strict';

const path = require('path');
const readline = require('readline');

const out = process.stdout;
const send = (obj) => out.write(JSON.stringify(obj) + '\n');

let pw = null;
let server = null;
let client = null;
let closing = false;
const lastBusy = new WeakMap(); // context -> ms when it last had a page

function defaultContext() {
  const ctxs = client.contexts();
  if (!ctxs.length) throw new Error('the browser has no default context');
  return ctxs[0];
}

const CHROMIUM_ONLY = ['partitionKey', 'sourceScheme', 'sourcePort'];

// Firefox silently discards a cookie whose expiry lies more than 400 days
// ahead (addCookies still succeeds), as Chromium caps such a date to 400
// days: cap it the same way, so the cookie is kept.
const MAX_AGE_S = 400 * 24 * 3600;

function clean(c, now) {
  const o = {};
  for (const [k, v] of Object.entries(c || {})) {
    if (!CHROMIUM_ONLY.includes(k) && v !== null && v !== undefined) o[k] = v;
  }
  if (typeof o.expires === 'number' && o.expires > now + MAX_AGE_S) o.expires = now + MAX_AGE_S - 60;
  return o;
}

const keyOf = (c) => [c.name, c.domain, c.path].join('\u0000');
const bare = (domain) => String(domain || '').replace(/^\./, '');

function hostOf(c) {
  try { return new URL(c.url).hostname; } catch (e) { return ''; }
}

// The path Playwright gives a cookie set by url: the url's directory.
function urlPath(c) {
  try {
    const u = new URL(c.url).pathname;
    return u.slice(0, u.lastIndexOf('/') + 1) || '/';
  } catch (e) {
    return '/';
  }
}

async function addCookies(ctx, args) {
  const given = Array.isArray(args.cookies) ? args.cookies : [];
  const now = Math.floor(Date.now() / 1000);
  let cookies = given.filter((c) => c && typeof c === 'object').map((c) => clean(c, now));
  let dropped = given.length - cookies.length;
  let skipped = 0;
  if (args.skipExisting) {
    // Session cookies put back after a restart: one the browser already has
    // stays as it is.
    const have = new Set((await ctx.cookies()).map(keyOf));
    const missing = cookies.filter((c) => !have.has(keyOf(c)));
    skipped = cookies.length - missing.length;
    cookies = missing;
  }
  if (!cookies.length) return { added: 0, dropped, skipped };
  let batch = true;
  try {
    await ctx.addCookies(cookies);
  } catch (e) {
    batch = false;
    console.error('serve.js: adding ' + cookies.length + ' cookies at once failed (' + String(e.message).split('\n')[0] + '); one by one');
  }
  if (!batch) {
    for (const c of cookies) {
      try {
        await ctx.addCookies([c]);
      } catch (e) { /* refused: counted below */ }
    }
  }
  // Firefox may take a cookie without an error and still not keep it: count
  // what the browser really holds afterwards, value included (a refused
  // cookie must not count because an older one of the same name is there).
  // A cookie given by url is matched by name and that url's host.
  const held = (name, domain, p, value) => [name, domain, p, value].join('\u0000');
  const have = new Set((await ctx.cookies()).map((c) => held(c.name, bare(c.domain), c.path, c.value)));
  let added = 0;
  for (const c of cookies) {
    const domain = bare(c.domain || hostOf(c));
    const p = c.path || (c.url ? urlPath(c) : '/');
    const value = c.value === undefined ? '' : String(c.value);
    if (have.has(held(c.name, domain, p, value)) || have.has(held(c.name, domain, '/', value))) added++;
    else dropped++;
  }
  return { added, dropped, skipped };
}

async function prune(ctxs, args) {
  const idleMs = Math.max(0, Number(args.idleSeconds || 60) * 1000);
  const now = Date.now();
  let closed = 0;
  for (const ctx of ctxs.slice(1)) {
    if (!args.closeAll) {
      if (ctx.pages().length > 0) {
        lastBusy.set(ctx, now);
        continue;
      }
      if (!lastBusy.has(ctx)) lastBusy.set(ctx, now);
      if (now - lastBusy.get(ctx) < idleMs) continue;
    }
    try {
      await ctx.close();
      closed++;
    } catch (e) {
      console.error('serve.js: closing a context failed: ' + String(e.message).split('\n')[0]);
    }
  }
  return { closed, open: ctxs.length - closed };
}

async function shutdown(code) {
  if (closing) return;
  closing = true;
  try {
    if (server) await server.close();
  } catch (e) { /* the browser may be gone already */ }
  send({ event: 'closed' });
  process.exit(code);
}

async function handle(req) {
  switch (req.cmd) {
    case 'ping':
      return { cookies: (await defaultContext().cookies()).length };
    case 'cookies.get':
      return await defaultContext().cookies();
    case 'cookies.add':
      return await addCookies(defaultContext(), req.args || {});
    case 'contexts.prune':
      return await prune(client.contexts(), req.args || {});
    case 'close':
      setImmediate(() => shutdown(0));
      return { closing: true };
    default:
      throw new Error('unknown command ' + req.cmd);
  }
}

async function main(options) {
  server = await pw.firefox.launchServer(options);
  const ws = server.wsEndpoint();
  const u = new URL(ws);
  const proc = server.process();
  server.on('close', () => shutdown(0));
  client = await pw.firefox.connect(ws, { timeout: 60000 });
  client.on('disconnected', () => shutdown(0));
  send({ event: 'listening', port: Number(u.port), wsPath: u.pathname, pid: proc ? proc.pid : null, version: client.version() });
}

function run() {
  // Playwright and anything else that logs write to stderr, never stdout.
  for (const k of ['log', 'info', 'debug']) console[k] = (...a) => console.error(...a);
  const driverPackage = process.argv[2];
  try {
    pw = require(path.join(driverPackage, 'index.js'));
  } catch (e) {
    console.error('serve.js: cannot load Playwright from ' + driverPackage + ': ' + e.message);
    process.exit(2);
  }
  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  let started = false;
  rl.on('line', (line) => {
    if (!line.trim()) return;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch (e) {
      console.error('serve.js: a line that is not JSON');
      return;
    }
    if (!started) {
      started = true;
      main(msg.options || {}).catch((e) => {
        console.error('serve.js: launch failed: ' + e.message);
        send({ event: 'failed', error: String(e.message || e).split('\n').slice(0, 6).join('\n') });
        process.exit(1);
      });
      return;
    }
    Promise.resolve()
      .then(() => handle(msg))
      .then((result) => send({ id: msg.id, ok: true, result }))
      .catch((e) => send({ id: msg.id, ok: false, error: String(e.message || e).split('\n')[0] }));
  });
  // The launcher went away: take the browser with us.
  rl.on('close', () => shutdown(0));
  process.on('SIGTERM', () => shutdown(0));
}

if (require.main === module) run();

module.exports = { clean, addCookies, prune, MAX_AGE_S };

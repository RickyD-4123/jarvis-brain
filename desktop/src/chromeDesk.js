'use strict';
// The desk window: Jarvis's page in Google Chrome's app mode (no tabs, no address bar), because voice
// input (webkitSpeechRecognition) only works in real Chrome, not in Electron or WebView2.
//
// It is a separate Chrome instance with its own profile (%APPDATA%\Jarvis\chrome-desk), launched and
// controlled by this app over a private DevTools pipe (--remote-debugging-pipe: file handles shared
// with this process only, no network port). Through that pipe the app enforces the same rules as the
// Electron window:
//   - every page load in the window is checked; only http://127.0.0.1:<port> is allowed, an http(s)
//     link elsewhere is cancelled and opened in your default browser, anything else is dropped;
//   - popups (window.open, target=_blank) never stay open in the app;
//   - fullscreen_on_start opens it fullscreen; F11 toggles (Chrome's own), Esc leaves fullscreen;
//   - closing the window closes this Chrome instance, and the app quits with it.
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const policy = require('./policy');

const CHROME_PATHS = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  path.join(process.env.LOCALAPPDATA || '', 'Google', 'Chrome', 'Application', 'chrome.exe'),
];

function findChrome(env = process.env) {
  for (const p of [env.JARVIS_CHROME, ...CHROME_PATHS]) {
    if (p && fs.existsSync(p)) return p;
  }
  return null;
}

/** A minimal Chrome DevTools Protocol client over the pipe (messages are JSON, NUL-separated). */
class Cdp {
  constructor(writable, readable) {
    this.out = writable;
    this.nextId = 1;
    this.pending = new Map();
    this.handlers = new Map();
    let buf = '';
    readable.setEncoding('utf8');
    readable.on('data', (chunk) => {
      buf += chunk;
      let i;
      while ((i = buf.indexOf('\0')) >= 0) {
        const raw = buf.slice(0, i);
        buf = buf.slice(i + 1);
        let msg;
        try { msg = JSON.parse(raw); } catch { continue; }
        if (msg.id && this.pending.has(msg.id)) {
          const { resolve, reject } = this.pending.get(msg.id);
          this.pending.delete(msg.id);
          if (msg.error) reject(new Error(`${msg.error.message} (${msg.error.code})`));
          else resolve(msg.result || {});
        } else if (msg.method) {
          for (const fn of this.handlers.get(msg.method) || []) fn(msg.params || {}, msg.sessionId);
        }
      }
    });
    const fail = () => {
      for (const { reject } of this.pending.values()) reject(new Error('Chrome closed'));
      this.pending.clear();
    };
    readable.on('close', fail);
    readable.on('error', fail);
    writable.on('error', fail);
  }

  send(method, params = {}, sessionId) {
    const id = this.nextId++;
    const msg = { id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      try {
        this.out.write(`${JSON.stringify(msg)}\0`);
      } catch (e) {
        this.pending.delete(id);
        reject(e);
      }
    });
  }

  on(method, fn) {
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(fn);
  }
}

// Runs in the page (main world) at every load: Esc asks the app to leave fullscreen. The page's own
// Esc handling (clearing the message box) still runs; this listener only adds the window part.
const ESC_SCRIPT = `addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && typeof __jarvisEsc === 'function') __jarvisEsc('');
}, true);`;

/**
 * Open the desk. Resolves a handle { proc, focus(), close(), exitFullscreen(), events } once the
 * window is up and its guards are in place.
 * opts: { chrome, url, origin, profileDir, fullscreen, openExternal(url), onClosed(), headless, log(msg) }
 */
async function launchChromeDesk(opts) {
  const log = opts.log || (() => {});
  fs.mkdirSync(opts.profileDir, { recursive: true });
  const args = [
    `--user-data-dir=${opts.profileDir}`,
    '--remote-debugging-pipe',
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-sync',
    '--disable-features=Translate,MediaRouter',
    '--disable-background-mode',
  ];
  if (opts.headless) args.push('--headless=new', opts.url);
  else args.push(`--app=${opts.url}`);
  if (opts.fullscreen && !opts.headless) args.push('--start-fullscreen');

  const proc = spawn(opts.chrome, args, { stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'], windowsHide: false });
  const cdp = new Cdp(proc.stdio[3], proc.stdio[4]);
  const events = { external: [], blocked: [], closedPopups: 0 };
  const attached = new Set();
  let mainTarget = null;
  let mainSession = null;
  let closing = false;

  proc.once('exit', () => {
    closing = true;
    if (opts.onClosed) opts.onClosed();
  });

  async function closeBrowser() {
    if (closing) return;
    closing = true;
    try { await Promise.race([cdp.send('Browser.close'), new Promise((r) => setTimeout(r, 3000))]); } catch { /* already gone */ }
    if (proc.exitCode === null) {
      try { proc.kill(); } catch { /* gone */ }
    }
  }

  async function exitFullscreen() {
    if (!mainTarget) return false;
    const { windowId, bounds } = await cdp.send('Browser.getWindowForTarget', { targetId: mainTarget });
    if (bounds && bounds.windowState === 'fullscreen') {
      await cdp.send('Browser.setWindowBounds', { windowId, bounds: { windowState: 'normal' } });
      return true;
    }
    return false;
  }

  cdp.on('Fetch.requestPaused', (p, sessionId) => {
    const url = p.request && p.request.url;
    const verdict = policy.decide(url, opts.origin);
    const isPopup = sessionId !== mainSession;
    if (verdict === 'allow' && !(isPopup && url !== 'about:blank' && !policy.isLocal(url, opts.origin))) {
      cdp.send('Fetch.continueRequest', { requestId: p.requestId }, sessionId).catch(() => {});
      return;
    }
    cdp.send('Fetch.failRequest', { requestId: p.requestId, errorReason: 'BlockedByClient' }, sessionId).catch(() => {});
    if (verdict === 'external') {
      events.external.push(url);
      log(`opened in your default browser: ${url}`);
      opts.openExternal(url);
    } else {
      events.blocked.push(url);
      log(`blocked: ${String(url).slice(0, 200)}`);
    }
    if (isPopup && p.frameId) {
      cdp.send('Target.closeTarget', { targetId: p.frameId }).catch(() => {});
      events.closedPopups += 1;
    }
  });

  cdp.on('Runtime.bindingCalled', (p) => {
    if (p.name === '__jarvisEsc') exitFullscreen().catch(() => {});
  });

  cdp.on('Target.detachedFromTarget', (p) => {
    if (p.targetId === mainTarget || p.sessionId === mainSession) {
      log('desk window closed');
      closeBrowser();
    }
  });

  async function setupPage(sessionId, info, waiting) {
    if (attached.has(info.targetId)) return;
    attached.add(info.targetId);
    const isMain = !mainTarget && (policy.isLocal(info.url, opts.origin) || !info.openerId);
    if (isMain) {
      mainTarget = info.targetId;
      mainSession = sessionId;
    }
    try {
      await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*', resourceType: 'Document', requestStage: 'Request' }] }, sessionId);
      if (isMain) {
        await cdp.send('Runtime.enable', {}, sessionId);                  // bindings need the Runtime domain on
        await cdp.send('Runtime.addBinding', { name: '__jarvisEsc' }, sessionId);
        await cdp.send('Page.enable', {}, sessionId);
        await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: ESC_SCRIPT }, sessionId);
        await cdp.send('Runtime.evaluate', { expression: ESC_SCRIPT }, sessionId).catch(() => {});
      } else if (!policy.isLocal(info.url, opts.origin) && info.url && info.url !== 'about:blank') {
        // A popup that already has a foreign address: hand it over and close it.
        const verdict = policy.decide(info.url, opts.origin);
        if (verdict === 'external') { events.external.push(info.url); opts.openExternal(info.url); }
        await cdp.send('Target.closeTarget', { targetId: info.targetId }).catch(() => {});
        events.closedPopups += 1;
      }
    } finally {
      if (waiting) await cdp.send('Runtime.runIfWaitingForDebugger', {}, sessionId).catch(() => {});
    }
  }

  cdp.on('Target.attachedToTarget', (p) => {
    if (p.targetInfo.type !== 'page') {
      if (p.waitingForDebugger) cdp.send('Runtime.runIfWaitingForDebugger', {}, p.sessionId).catch(() => {});
      return;
    }
    setupPage(p.sessionId, p.targetInfo, p.waitingForDebugger).catch((e) => log(`page setup failed: ${e.message}`));
  });

  // Every new page is attached paused (waitForDebuggerOnStart) so its guards are in place before it
  // loads anything; the window that already exists is attached right after.
  await cdp.send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
  const deadline = Date.now() + 15000;
  while (!mainTarget && Date.now() < deadline) {
    const { targetInfos } = await cdp.send('Target.getTargets');
    for (const t of targetInfos.filter((x) => x.type === 'page' && !attached.has(x.targetId))) {
      const { sessionId } = await cdp.send('Target.attachToTarget', { targetId: t.targetId, flatten: true });
      await setupPage(sessionId, t, false);
    }
    if (!mainTarget) await new Promise((r) => setTimeout(r, 200));
  }
  if (!mainTarget) {
    await closeBrowser();
    throw new Error('Chrome started but the desk window never appeared');
  }

  return {
    proc,
    events,
    cdp,
    exitFullscreen,
    focus: () => cdp.send('Target.activateTarget', { targetId: mainTarget }).catch(() => {}),
    navigate: (url) => cdp.send('Page.navigate', { url }, mainSession),
    evaluate: (expression, userGesture = false) => cdp.send('Runtime.evaluate', { expression, returnByValue: true, userGesture }, mainSession),
    windowState: async () => (await cdp.send('Browser.getWindowForTarget', { targetId: mainTarget })).bounds.windowState,
    close: closeBrowser,
  };
}

module.exports = { findChrome, launchChromeDesk, Cdp };

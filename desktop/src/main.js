'use strict';
// Jarvis desktop app, main process.
//
//   1. one instance only (a second launch just brings the desk window forward)
//   2. first run: the data folder gets its files from the committed templates, never overwriting
//   3. the brain: the Claude Code CLI on this computer must be found and signed in, else a setup message
//   4. the backend starts on a free 127.0.0.1 port; nothing opens until GET /health answers
//   5. the desk opens in a Chrome app window (voice input needs real Chrome) or, without Chrome, in a
//      locked-down Electron window; fullscreen when config.json's fullscreen_on_start is true (default)
//   6. closing the desk, the power button or quitting stops the backend and its children
const { app, dialog, shell } = require('electron');
const fs = require('fs');
const path = require('path');

const backend = require('./backend');
const claude = require('./claude');
const firstrun = require('./firstrun');
const policy = require('./policy');
const { resolvePaths } = require('./paths');
const { findChrome, launchChromeDesk } = require('./chromeDesk');

const SMOKE = process.env.JARVIS_SMOKE_TEST === '1';

app.setName('Jarvis');
if (process.env.JARVIS_APP_DIR) app.setPath('userData', path.join(process.env.JARVIS_APP_DIR, 'electron'));
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  let paths = null;
  let handle = null;            // the backend we started (null when attached to one already running)
  let desk = null;              // { kind: 'chrome' | 'electron', focus(), close() }
  let quitting = false;
  let logStream = null;

  const log = (msg) => {
    const line = `${new Date().toISOString()} ${msg}\n`;
    if (logStream) logStream.write(line);
    if (!app.isPackaged) process.stdout.write(line);
  };

  function readConfig(home) {
    try {
      return JSON.parse(fs.readFileSync(path.join(home, 'config.json'), 'utf8'));
    } catch {
      return {};
    }
  }

  async function ensureClaude() {
    for (;;) {
      const st = await claude.checkClaude();
      log(`claude: found=${st.found} exe=${st.exe || '-'} loggedIn=${st.loggedIn} ${st.detail}`);
      if (st.found && st.loggedIn !== false) return st;
      const problem = !st.found
        ? "Jarvis's brain is the Claude Code app on this computer, and it isn't installed (the claude command wasn't found)."
        : "Claude Code is installed but isn't signed in to your Claude account.";
      const { response } = await dialog.showMessageBox({
        type: 'info',
        title: 'Jarvis: one-time setup',
        message: problem,
        detail: `${claude.SETUP_STEPS}\n\nJarvis uses your own Claude subscription through Claude Code. It never stores your login and needs no API key.`,
        buttons: ['Retry', 'Quit'],
        defaultId: 0,
        cancelId: 1,
        noLink: true,
      });
      if (response === 1) return null;
    }
  }

  async function fail(message, detail) {
    log(`error: ${message} ${detail || ''}`);
    await dialog.showMessageBox({ type: 'error', title: 'Jarvis', message, detail: detail || '', buttons: ['Quit'] });
    shutdown();
  }

  async function openDesk(port, fullscreen) {
    const origin = policy.localOrigin(port);
    const url = `${origin}/`;
    const chrome = findChrome();
    if (chrome) {
      const d = await launchChromeDesk({
        chrome, url, origin, fullscreen, headless: SMOKE,
        profileDir: path.join(paths.appDir, 'chrome-desk'),
        openExternal: (u) => { if (policy.isExternalOk(u)) shell.openExternal(u); },
        onClosed: () => { log('desk (Chrome) closed'); shutdown(); },
        log,
      });
      return { kind: 'chrome', focus: d.focus, close: d.close, evaluate: d.evaluate };
    }
    log('Google Chrome not found: using the Electron window (voice input needs Chrome)');
    const { createDeskWindow, loadLocal } = require('./window');
    const win = createDeskWindow({ origin, fullscreen, title: `Jarvis ${app.getVersion()}` });
    win.on('closed', () => { log('desk (Electron) closed'); shutdown(); });
    await loadLocal(win, url, origin);
    return {
      kind: 'electron',
      focus: () => { if (win.isMinimized()) win.restore(); win.show(); win.focus(); },
      close: async () => { if (!win.isDestroyed()) win.destroy(); },
    };
  }

  async function start() {
    paths = resolvePaths({ packaged: app.isPackaged, resourcesPath: process.resourcesPath, appData: app.getPath('appData') });
    fs.mkdirSync(paths.appDir, { recursive: true });
    logStream = fs.createWriteStream(path.join(paths.appDir, 'desktop.log'), { flags: 'a' });
    log(`--- Jarvis ${app.getVersion()} (${paths.mode}) home=${paths.home}`);

    const seeded = firstrun.seed(paths.templates, paths.home);
    if (seeded.created.length) log(`first run: created ${seeded.created.length} file(s) from templates`);

    const st = await ensureClaude();
    if (!st) return shutdown();

    const config = readConfig(paths.home);
    const fullscreen = config.fullscreen_on_start !== false;

    // Dev mode shares the repo's data with the web version: if that is already running, use it
    // rather than start a second backend on the same files.
    let port = null;
    if (paths.mode === 'dev' && !process.env.JARVIS_HOME && await backend.health(backend.PREFERRED_PORT)) {
      port = backend.PREFERRED_PORT;
      log('the web version is already running on 4700 with the same data: opening the desk on it');
    } else {
      port = await backend.freePort();
      handle = backend.startBackend({
        backend: paths.backend,
        home: paths.home,
        setHome: path.resolve(paths.home) !== path.resolve(paths.repo || ''),
        port,
        claudeExe: st.exe,
        searchPath: st.path,
        version: app.getVersion(),
        logFile: path.join(paths.home, 'data', 'jarvis.log'),
      });
      handle.exited.then((code) => {
        log(`backend exited (${code})`);
        handle = null;
        shutdown();
      });
      try {
        await backend.waitForHealth(handle, 120000);
      } catch (e) {
        return fail('Jarvis could not start.', `${e.message}\n\nDetails are in ${path.join(paths.home, 'data', 'jarvis.log')}`);
      }
      log(`backend healthy on 127.0.0.1:${port}`);
    }
    try {
      desk = await openDesk(port, fullscreen);
    } catch (e) {
      return fail('Jarvis could not open its window.', e.message);
    }
    if (SMOKE) {
      // JARVIS_SMOKE_TEST=1 (for testing a build): desk opened headless; check the page loaded, then quit.
      await new Promise((r) => setTimeout(r, 3000));
      const title = desk.evaluate ? (await desk.evaluate('document.title + " | " + location.origin')).result.value : desk.kind;
      log(`smoke test: desk=${desk.kind} page="${title}" backend=${port}`);
      shutdown();
    }
  }

  async function shutdown() {
    if (quitting) return;
    quitting = true;
    try {
      if (desk) await desk.close();
    } catch { /* already closed */ }
    try {
      if (handle) await backend.stopBackend(handle);
    } catch (e) {
      log(`stopping the backend: ${e.message}`);
    }
    log('quit');
    app.exit(0);
  }

  app.on('second-instance', () => {
    if (desk) desk.focus();
  });
  // Defence in depth: no window this app ever makes may run a <webview> or open another window.
  app.on('web-contents-created', (_e, contents) => {
    contents.on('will-attach-webview', (event) => event.preventDefault());
  });
  app.on('window-all-closed', () => { /* the desk decides; a setup dialog closing must not quit early */ });
  app.on('before-quit', (event) => {
    if (!quitting) {
      event.preventDefault();
      shutdown();
    }
  });
  app.whenReady().then(start).catch((e) => fail('Jarvis could not start.', e.stack || e.message));
}

'use strict';
// Run by electron-window.test.js inside Electron: builds the real desk window (src/window.js) against
// a local test server and tries every way out. Prints one JSON line with what happened, then quits.
const { app } = require('electron');
const path = require('path');
const { createDeskWindow, loadLocal } = require(path.join(__dirname, '..', '..', 'src', 'window'));
const { startTestServer, sleep, until } = require(path.join(__dirname, '..', 'helpers'));

app.whenReady().then(async () => {
  const out = { external: [], blocked: [] };
  const { srv, origin } = await startTestServer();
  const win = createDeskWindow({
    origin, fullscreen: false, show: false,
    openExternal: (u) => out.external.push(u),
    onBlocked: (u) => out.blocked.push(u),
  });
  const wc = win.webContents;
  try {
    // 1. Loading a non-local address is refused outright.
    try {
      await loadLocal(win, 'https://example.com/', origin);
      out.loadForeign = 'loaded';
    } catch (e) {
      out.loadForeign = e.message;
    }
    // 2. The local page loads, without Node in it.
    await loadLocal(win, `${origin}/`, origin);
    out.local = wc.getURL();
    out.pageHasNode = await wc.executeJavaScript('typeof require !== "undefined" || typeof process !== "undefined"');
    out.prefs = {
      contextIsolation: wc.getLastWebPreferences().contextIsolation,
      nodeIntegration: wc.getLastWebPreferences().nodeIntegration,
      sandbox: wc.getLastWebPreferences().sandbox,
    };
    // 3. The page navigating itself away: cancelled, handed to the default browser.
    await win.loadURL(`${origin}/go-external`);
    await until(() => out.external.includes('https://example.com/navigated'), 5000);
    await sleep(300);
    out.afterNavigate = wc.getURL();
    // 4. A redirect from the local server to another site.
    await win.loadURL(`${origin}/redir`).catch((e) => { out.redirectError = e.code || e.message; });
    await until(() => out.external.includes('https://example.com/redirected'), 5000);
    out.afterRedirect = wc.getURL();
    // 5. A popup.
    await win.loadURL(`${origin}/`);
    await wc.executeJavaScript('window.open("https://example.com/popup"); 1', true);
    await until(() => out.external.includes('https://example.com/popup'), 5000);
    out.windowCount = require('electron').BrowserWindow.getAllWindows().length;
    // 6. file: (not http) is dropped, not opened anywhere.
    await win.loadURL(`${origin}/go-file`);
    await sleep(1000);
    out.afterFile = wc.getURL();
  } catch (e) {
    out.error = e.stack || e.message;
  }
  process.stdout.write(`RESULT ${JSON.stringify(out)}\n`);
  srv.close();
  app.exit(0);
});

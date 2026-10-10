'use strict';
// The Electron desk window: used when Google Chrome isn't installed (voice input needs Chrome, so
// there you type; speech output still works). Locked down: context isolation and the sandbox on, no
// Node in the page, only the backend's origin may load, anything else goes to your default browser.
const { BrowserWindow, shell, session } = require('electron');
const policy = require('./policy');

function openExternalSafe(url) {
  if (policy.isExternalOk(url)) shell.openExternal(url);
}

/** Apply the navigation, popup and permission rules to a window's contents. Exported for the tests. */
function guard(win, origin, { openExternal = openExternalSafe, onBlocked = () => {} } = {}) {
  const wc = win.webContents;
  const check = (event, url) => {
    if (policy.isLocal(url, origin)) return;
    event.preventDefault();
    if (policy.decide(url, origin) === 'external') openExternal(url);
    else onBlocked(url);
  };
  wc.on('will-frame-navigate', (event) => check(event, event.url));     // any frame, the page itself included
  wc.on('will-redirect', (event) => check(event, event.url));           // e.g. /oauth/start -> Google sign-in
  wc.setWindowOpenHandler(({ url }) => {
    if (policy.decide(url, origin) === 'external') openExternal(url);
    else onBlocked(url);
    return { action: 'deny' };
  });
  wc.on('will-attach-webview', (event) => event.preventDefault());
  // Microphone (and nothing else) for the local page only.
  wc.session.setPermissionRequestHandler((contents, permission, callback, details) => {
    callback(permission === 'media' && policy.isLocal(details.requestingUrl || contents.getURL(), origin));
  });
  wc.session.setPermissionCheckHandler((contents, permission, requestingOrigin) => permission === 'media' && requestingOrigin === origin);
}

/** Load a URL only if it is the backend's own origin. Refuses (throws) anything else. */
function loadLocal(win, url, origin) {
  if (!policy.isLocal(url, origin)) throw new Error(`refused to load a non-local address: ${url}`);
  return win.loadURL(url);
}

/** Esc leaves fullscreen, F11 toggles it. The key still reaches the page (Esc also clears the message box). */
function fullscreenKeys(win) {
  win.webContents.on('before-input-event', (_event, input) => {
    if (input.type !== 'keyDown') return;
    if (input.key === 'Escape' && win.isFullScreen()) win.setFullScreen(false);
    else if (input.key === 'F11') win.setFullScreen(!win.isFullScreen());
  });
}

function createDeskWindow({ origin, fullscreen, title, show = true, partition, openExternal, onBlocked }) {
  const win = new BrowserWindow({
    width: 1400,
    height: 900,
    show,
    fullscreen: !!fullscreen,
    title: title || 'Jarvis',
    autoHideMenuBar: true,
    backgroundColor: '#05080d',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      nodeIntegrationInWorker: false,
      nodeIntegrationInSubFrames: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      spellcheck: false,
      partition: partition || undefined,
    },
  });
  win.removeMenu();
  guard(win, origin, { openExternal: openExternal || openExternalSafe, onBlocked });
  fullscreenKeys(win);
  return win;
}

module.exports = { createDeskWindow, guard, loadLocal, fullscreenKeys, openExternalSafe, session };

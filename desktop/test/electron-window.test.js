'use strict';
// The app refuses to load a non-local origin (Electron window).
const test = require('node:test');
const assert = require('node:assert');
const { spawnSync } = require('child_process');
const path = require('path');

test('the Electron desk window refuses every non-local origin', { timeout: 120000 }, () => {
  const electron = require('electron');                      // the binary's path, when required from Node
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;
  const r = spawnSync(electron, [path.join(__dirname, 'electron', 'nav-check.js')], { env, encoding: 'utf8', timeout: 110000 });
  const line = String(r.stdout || '').split('\n').find((l) => l.startsWith('RESULT '));
  assert.ok(line, `no result from Electron:\n${r.stdout}\n${r.stderr}`);
  const out = JSON.parse(line.slice(7));
  assert.strictEqual(out.error, undefined, out.error);
  const origin = new URL(out.local).origin;

  assert.match(out.loadForeign, /refused to load a non-local address/);
  assert.strictEqual(out.pageHasNode, false, 'no require/process in the page');
  assert.deepStrictEqual(out.prefs, { contextIsolation: true, nodeIntegration: false, sandbox: true });
  for (const u of ['https://example.com/navigated', 'https://example.com/redirected', 'https://example.com/popup']) {
    assert.ok(out.external.includes(u), `${u} handed to the default browser`);
  }
  for (const after of [out.afterNavigate, out.afterRedirect, out.afterFile]) {
    assert.ok(after === '' || new URL(after).origin === origin, `window stayed local (was ${after})`);
  }
  assert.strictEqual(out.windowCount, 1, 'no popup window opened');
  // file: is stopped by Chromium itself (an http page may not open local files) or by the guard: either way
  // the window stays local (checked above) and nothing is handed to the OS.
  assert.ok(!out.external.some((u) => u.startsWith('file:')), 'file: link not handed to the OS');
});

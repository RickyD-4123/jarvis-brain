'use strict';
// The app refuses to load a non-local origin (the Chrome desk window, the one used when Chrome is
// installed). Runs Chrome headless with its own throwaway profile. Skipped without Chrome.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { findChrome, launchChromeDesk } = require('../src/chromeDesk');
const { startTestServer, sleep, until } = require('./helpers');

const chrome = findChrome();

test('the Chrome desk window keeps to the local origin', { skip: !chrome && 'Google Chrome not installed', timeout: 120000 }, async (t) => {
  const { srv, origin } = await startTestServer();
  const profileDir = fs.mkdtempSync(path.join(os.tmpdir(), 'jarvis-chrome-'));
  const opened = [];
  let closed = false;
  const desk = await launchChromeDesk({
    chrome, url: `${origin}/`, origin, profileDir, fullscreen: false, headless: true,
    openExternal: (u) => opened.push(u), onClosed: () => { closed = true; },
  });
  t.after(async () => {
    await desk.close();
    srv.close();
    await sleep(500);
    fs.rmSync(profileDir, { recursive: true, force: true });
  });
  const href = async () => (await desk.evaluate('location.href')).result.value;
  await until(async () => (await href()) === `${origin}/`, 10000);

  // A direct load of another site: never loaded, handed to the default browser.
  await desk.navigate('https://example.com/direct').catch(() => {});
  await until(() => opened.includes('https://example.com/direct'), 5000);
  assert.ok(opened.includes('https://example.com/direct'));
  assert.ok((await href()).startsWith(origin) || (await href()).startsWith('chrome-error:'), await href());

  // The page navigating itself away.
  await desk.navigate(`${origin}/go-external`);
  await until(() => opened.includes('https://example.com/navigated'), 5000);
  assert.ok(opened.includes('https://example.com/navigated'));

  // A redirect from the local server to another site (Google sign-in works this way).
  await desk.navigate(`${origin}/redir`).catch(() => {});
  await until(() => opened.includes('https://example.com/redirected'), 5000);
  assert.ok(opened.includes('https://example.com/redirected'));

  // A popup: opened in the default browser, and the popup itself closed.
  await desk.navigate(`${origin}/`);
  await sleep(300);
  await desk.evaluate('window.open("https://example.com/popup"); 1', true);
  await until(() => opened.includes('https://example.com/popup'), 5000);
  assert.ok(opened.includes('https://example.com/popup'));
  const pages = await until(async () => {
    const { targetInfos } = await desk.cdp.send('Target.getTargets');
    const p = targetInfos.filter((x) => x.type === 'page');
    return p.length === 1 ? p : null;
  }, 5000);
  assert.ok(pages, 'the popup was closed');

  // file: is dropped, never handed to the OS.
  await desk.navigate(`${origin}/go-file`);
  await sleep(1000);
  assert.ok(!opened.some((u) => u.startsWith('file:')));
  assert.ok((await href()).startsWith(origin), 'still on the local page');

  assert.strictEqual(closed, false);
});

// Shows a real (fullscreen) window for a few seconds, so it only runs when asked: JARVIS_UI_TESTS=1 npm test
test('fullscreen_on_start opens the desk fullscreen and Esc leaves fullscreen',
  { skip: (!chrome && 'Google Chrome not installed') || (process.env.JARVIS_UI_TESTS !== '1' && 'set JARVIS_UI_TESTS=1 (opens a visible window)'), timeout: 60000 }, async (t) => {
    const { srv, origin } = await startTestServer();
    const profileDir = fs.mkdtempSync(path.join(os.tmpdir(), 'jarvis-chrome-'));
    const desk = await launchChromeDesk({ chrome, url: `${origin}/`, origin, profileDir, fullscreen: true, openExternal: () => {}, onClosed: () => {} });
    t.after(async () => {
      await desk.close();
      srv.close();
      await sleep(500);
      fs.rmSync(profileDir, { recursive: true, force: true });
    });
    assert.strictEqual(await until(async () => (await desk.windowState()) === 'fullscreen' && 'fullscreen', 5000), 'fullscreen');
    await until(async () => (await desk.evaluate('typeof __jarvisEsc')).result.value === 'function', 5000);
    await desk.evaluate('dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); 1');
    assert.strictEqual(await until(async () => (await desk.windowState()) === 'normal' && 'normal', 5000), 'normal');
  });

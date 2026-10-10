'use strict';
// Starts the real backend (dev mode: the repo's server.py through desktop/backend/run_backend.py)
// against a throwaway data folder, exactly as the app does. Needs Python and Claude Code installed.
const test = require('node:test');
const assert = require('node:assert');
const { execFileSync } = require('child_process');
const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');

const backend = require('../src/backend');
const claude = require('../src/claude');
const firstrun = require('../src/firstrun');
const { resolvePaths } = require('../src/paths');

const REPO = path.resolve(__dirname, '..', '..');

function lanAddress() {
  for (const list of Object.values(os.networkInterfaces())) {
    for (const a of list || []) if (a.family === 'IPv4' && !a.internal) return a.address;
  }
  return null;
}

function canConnect(host, port) {
  return new Promise((resolve) => {
    const s = net.connect({ host, port, timeout: 1500 }, () => { s.destroy(); resolve(true); });
    s.on('error', () => resolve(false));
    s.on('timeout', () => { s.destroy(); resolve(false); });
  });
}

test('backend starts on a free 127.0.0.1 port, passes the health check, and stops cleanly', { timeout: 180000 }, async (t) => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'jarvis-home-'));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  firstrun.seed(REPO, home);
  const paths = resolvePaths({ packaged: false, appData: os.tmpdir(), env: {} });
  const st = await claude.checkClaude();
  assert.ok(st.found, 'Claude Code must be installed for this test');

  const port = await backend.freePort(0);
  const handle = backend.startBackend({
    backend: paths.backend, home, setHome: true, port, claudeExe: st.exe, searchPath: st.path,
    version: '9.9.9-test', logFile: path.join(home, 'data', 'jarvis.log'),
  });
  t.after(() => backend.stopBackend(handle));
  const h = await backend.waitForHealth(handle, 120000);
  assert.deepStrictEqual(h, { ok: true, app: 'jarvis', version: '9.9.9-test' });

  // Bound to loopback only.
  const lan = lanAddress();
  if (lan) assert.strictEqual(await canConnect(lan, port), false, `not reachable on ${lan}`);
  // The backend's own guards still apply on the new port: a foreign Origin can't POST.
  const foreign = await new Promise((resolve) => {
    const req = require('http').request({ host: '127.0.0.1', port, method: 'POST', path: '/chat',
      headers: { 'Content-Type': 'application/json', Origin: 'http://evil.example', 'Content-Length': 2 } }, (res) => { res.resume(); resolve(res.statusCode); });
    req.on('error', () => resolve(null));
    req.end('{}');
  });
  assert.strictEqual(foreign, 403);
  // Its files went to the data folder, not the repo.
  assert.ok(fs.existsSync(path.join(home, 'graph-data.js')), 'graph written into the data folder');
  assert.ok(fs.readFileSync(path.join(home, 'data', 'jarvis.log'), 'utf8').includes('Knowledge Galaxy on http://127.0.0.1:' + port));

  await backend.stopBackend(handle);
  assert.notStrictEqual(handle.child.exitCode, null, 'backend process has exited');
  assert.strictEqual(await backend.health(port), null);
});

test('inside the app, the internship safety settings are exactly the shipped ones', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'jarvis-home-'));
  try {
    firstrun.seed(REPO, home);
    const code = 'import json; from internship_apply import load_config, LOCAL_CONFIG_PATH; c = load_config(); '
      + 'print(json.dumps({k: c[k] for k in ("dry_run", "filling_enabled", "allow_approved_submit", "manual_only_domains", '
      + '"max_applications_per_day", "max_llm_calls_per_run", "max_llm_calls_per_day", "max_requests_per_run")} | {"local": LOCAL_CONFIG_PATH}))';
    const inApp = JSON.parse(execFileSync('python', ['-B', '-c', code], { cwd: REPO, env: { ...process.env, JARVIS_HOME: home }, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }));
    assert.strictEqual(inApp.local, path.join(home, 'internship_apply', 'config.local.yaml'));
    delete inApp.local;
    assert.deepStrictEqual(inApp, {
      dry_run: true, filling_enabled: false, allow_approved_submit: false, manual_only_domains: ['myworkdayjobs.com'],
      max_applications_per_day: 5, max_llm_calls_per_run: 20, max_llm_calls_per_day: 60, max_requests_per_run: 100,
    });
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

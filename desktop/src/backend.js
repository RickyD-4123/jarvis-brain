'use strict';
// The Jarvis backend as a child process: a free port on 127.0.0.1 only, a health check before any
// window opens, and a clean stop (the backend's own /shutdown, then the whole process tree) on quit.
const { spawn, execFile } = require('child_process');
const fs = require('fs');
const http = require('http');
const net = require('net');
const path = require('path');

const PREFERRED_PORT = 4700;   // the web version's port: Google sign-in links registered for it keep working

function portFree(port) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once('error', () => resolve(false));
    srv.listen({ port, host: '127.0.0.1', exclusive: true }, () => srv.close(() => resolve(true)));
  });
}

/** 4700 when it's free, else any free port the OS hands out (always on 127.0.0.1). */
async function freePort(preferred = PREFERRED_PORT) {
  if (preferred && await portFree(preferred)) return preferred;
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once('error', reject);
    srv.listen({ port: 0, host: '127.0.0.1' }, () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function request(port, method, route, body) {
  return new Promise((resolve) => {
    const data = body === undefined ? null : Buffer.from(JSON.stringify(body));
    const headers = { Host: `127.0.0.1:${port}` };
    if (data) Object.assign(headers, { 'Content-Type': 'application/json', 'Content-Length': data.length, Origin: `http://127.0.0.1:${port}` });
    const req = http.request({ host: '127.0.0.1', port, method, path: route, headers, timeout: 3000 }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        let json = null;
        try { json = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { /* not JSON */ }
        resolve({ status: res.statusCode, json });
      });
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => req.destroy());
    if (data) req.write(data);
    req.end();
  });
}

/** GET /health -> the parsed reply when it is a Jarvis backend, else null. */
async function health(port) {
  const r = await request(port, 'GET', '/health');
  return r && r.status === 200 && r.json && r.json.ok === true && r.json.app === 'jarvis' ? r.json : null;
}

function openLog(file) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  try {
    if (fs.statSync(file).size > 5 * 1024 * 1024) fs.renameSync(file, `${file}.1`);
  } catch { /* no log yet */ }
  return fs.createWriteStream(file, { flags: 'a' });
}

/**
 * Start the backend. Returns { child, port, exited (a promise of the exit code) }.
 * opts: { backend: {command, args, cwd}, home, setHome, port, claudeExe, searchPath, version, logFile }
 */
function startBackend(opts) {
  const env = { ...process.env, JARVIS_PORT: String(opts.port), JARVIS_VERSION: opts.version || 'dev', PYTHONUNBUFFERED: '1' };
  if (opts.setHome) env.JARVIS_HOME = opts.home;
  else delete env.JARVIS_HOME;
  if (opts.claudeExe) env.JARVIS_CLAUDE = opts.claudeExe;
  if (opts.searchPath) env.PATH = opts.searchPath;
  delete env.ANTHROPIC_API_KEY;                                         // the subscription, never an API key
  delete env.ANTHROPIC_AUTH_TOKEN;
  const child = spawn(opts.backend.command, opts.backend.args, {
    cwd: opts.backend.cwd, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (opts.logFile) {
    const log = openLog(opts.logFile);
    log.write(`--- started by the desktop app ${new Date().toISOString()} port ${opts.port}\n`);
    child.stdout.pipe(log, { end: false });
    child.stderr.pipe(log, { end: false });
    child.once('exit', () => setTimeout(() => log.end(), 200));
  } else {
    child.stdout.resume();
    child.stderr.resume();
  }
  const exited = new Promise((resolve) => {
    child.once('exit', (code) => resolve(code));
    child.once('error', (err) => resolve(`spawn failed: ${err.message}`));
  });
  return { child, port: opts.port, exited };
}

/** Poll /health until it answers or the backend exits. Resolves the health reply, rejects on failure. */
async function waitForHealth(handle, timeoutMs = 60000) {
  let done = null;
  handle.exited.then((code) => { done = code; });
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    if (done !== null) throw new Error(`the backend stopped before it was ready (exit ${done})`);
    const h = await health(handle.port);
    if (h) return h;
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`the backend didn't answer on 127.0.0.1:${handle.port} within ${timeoutMs / 1000} s`);
}

function killTree(pid) {
  return new Promise((resolve) => {
    if (process.platform !== 'win32') {
      try { process.kill(pid, 'SIGKILL'); } catch { /* gone */ }
      resolve();
      return;
    }
    execFile('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true }, () => resolve());
  });
}

/** Ask the backend to stop (its own /shutdown), then make sure the whole process tree is gone. */
async function stopBackend(handle, graceMs = 6000) {
  if (!handle || handle.child.exitCode !== null) return;
  await request(handle.port, 'POST', '/shutdown', {});
  const stopped = await Promise.race([handle.exited.then(() => true), new Promise((r) => setTimeout(() => r(false), graceMs))]);
  if (!stopped) await killTree(handle.child.pid);                      // with any claude child still running
}

module.exports = { freePort, portFree, health, startBackend, waitForHealth, stopBackend, request, PREFERRED_PORT };

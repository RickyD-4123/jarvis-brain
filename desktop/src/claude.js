'use strict';
// Jarvis's brain is the Claude Code CLI already installed on this computer, signed in with your Claude
// subscription. Nothing is bundled: no CLI, no credentials, no API client. This module finds the
// executable (also when the app is started from the Start menu, whose PATH can be older or shorter
// than your terminal's) and asks it whether it is signed in.
const { execFile } = require('child_process');
const fs = require('fs');
const path = require('path');

const IS_WIN = process.platform === 'win32';

/** PATH as the registry has it now (user + machine), so a Claude Code installed after login is found. */
function registryPath() {
  if (!IS_WIN) return Promise.resolve('');
  const read = (key) => new Promise((resolve) => {
    execFile('reg', ['query', key, '/v', 'Path'], { windowsHide: true, timeout: 5000 }, (err, out) => {
      const m = !err && /Path\s+REG_(?:EXPAND_)?SZ\s+(.*)/i.exec(out || '');
      resolve(m ? m[1].trim().replace(/%([^%]+)%/g, (_, v) => process.env[v] || `%${v}%`) : '');
    });
  });
  return Promise.all([read('HKCU\\Environment'),
    read('HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment')]).then((xs) => xs.filter(Boolean).join(';'));
}

/** The PATH to search and to hand to the backend: the app's own PATH plus the registry's, de-duplicated. */
async function effectivePath(env = process.env) {
  const parts = [...String(env.PATH || env.Path || '').split(path.delimiter), ...(await registryPath()).split(';')];
  const seen = new Set();
  return parts.filter((p) => p && !seen.has(p.toLowerCase()) && seen.add(p.toLowerCase())).join(path.delimiter);
}

/** npm's claude.cmd shim -> the real claude.exe beside it (what server.find_claude does too). */
function unshim(file) {
  if (/\.(cmd|ps1|bat)$/i.test(file)) {
    const exe = path.join(path.dirname(file), 'node_modules', '@anthropic-ai', 'claude-code', 'bin', 'claude.exe');
    if (fs.existsSync(exe)) return exe;
  }
  return file;
}

function candidates(searchPath, env = process.env) {
  const names = IS_WIN ? ['claude.exe', 'claude.cmd'] : ['claude'];
  const out = [];
  if (env.JARVIS_CLAUDE) out.push(env.JARVIS_CLAUDE);
  for (const dir of searchPath.split(path.delimiter)) for (const n of names) out.push(path.join(dir, n));
  const home = env.USERPROFILE || env.HOME || '';
  out.push(path.join(env.APPDATA || '', 'npm', 'claude.cmd'),
    path.join(home, '.local', 'bin', IS_WIN ? 'claude.exe' : 'claude'),
    path.join(env.LOCALAPPDATA || '', 'Programs', 'claude', 'claude.exe'));
  return out;
}

/** The claude executable, or null. */
function findClaude(searchPath, env = process.env) {
  for (const c of candidates(searchPath, env)) {
    try {
      if (c && fs.statSync(c).isFile()) return unshim(c);
    } catch { /* not there */ }
  }
  return null;
}

/**
 * { found, exe, loggedIn (true | false | null when it can't tell), detail, path }.
 * Runs `claude auth status` with any API key removed from the environment, as the backend does,
 * so "signed in" means signed in with your subscription.
 */
async function checkClaude(env = process.env) {
  const searchPath = await effectivePath(env);
  const exe = findClaude(searchPath, env);
  if (!exe) return { found: false, exe: null, loggedIn: false, detail: 'claude not found', path: searchPath };
  const childEnv = { ...env, PATH: searchPath };
  delete childEnv.ANTHROPIC_API_KEY;
  delete childEnv.ANTHROPIC_AUTH_TOKEN;
  return new Promise((resolve) => {
    // A .cmd shim with no claude.exe beside it can only run through cmd.exe (Node refuses it directly).
    const [cmd, args] = /\.(cmd|bat)$/i.test(exe) ? ['cmd.exe', ['/d', '/c', exe, 'auth', 'status']] : [exe, ['auth', 'status']];
    execFile(cmd, args, { env: childEnv, windowsHide: true, timeout: 30000 }, (err, stdout) => {
      let status = null;
      try {
        status = JSON.parse(String(stdout || '').trim());
      } catch { /* older CLI or unexpected output */ }
      if (status && typeof status.loggedIn === 'boolean') {
        resolve({ found: true, exe, loggedIn: status.loggedIn, detail: status.authMethod || '', path: searchPath });
      } else {
        resolve({ found: true, exe, loggedIn: err ? false : null, detail: err ? String(err.message).slice(0, 200) : 'unknown', path: searchPath });
      }
    });
  });
}

const SETUP_STEPS = [
  '1. Install Claude Code (once). In PowerShell:  irm https://claude.ai/install.ps1 | iex',
  '   (or, if you use Node.js:  npm install -g @anthropic-ai/claude-code)',
  '2. In a new PowerShell window run:  claude',
  '   and sign in with your Claude account (your Pro/Max subscription; no API key needed).',
  '3. Come back here and press Retry.',
].join('\n');

module.exports = { checkClaude, findClaude, effectivePath, unshim, SETUP_STEPS };

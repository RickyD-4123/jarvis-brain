'use strict';
// Finding the Claude Code CLI the way the installed app must: with a PATH that doesn't include it.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const claude = require('../src/claude');

test('the npm shim is resolved to the real claude.exe', (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jarvis-claude-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const shim = path.join(dir, 'claude.cmd');
  const exe = path.join(dir, 'node_modules', '@anthropic-ai', 'claude-code', 'bin', 'claude.exe');
  fs.mkdirSync(path.dirname(exe), { recursive: true });
  fs.writeFileSync(shim, '@echo off');
  fs.writeFileSync(exe, 'MZ');
  assert.strictEqual(claude.unshim(shim), exe);
  assert.strictEqual(claude.findClaude(dir, {}), exe);
});

test('nothing found when claude is nowhere', (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jarvis-claude-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  assert.strictEqual(claude.findClaude(dir, { APPDATA: dir, USERPROFILE: dir, LOCALAPPDATA: dir }), null);
});

test('found and signed in from a Start-menu style PATH (system folders only)', { timeout: 60000 }, async (t) => {
  const real = await claude.checkClaude();
  if (!real.found) return t.skip('Claude Code is not installed on this computer');
  const st = await claude.checkClaude({ ...process.env, PATH: 'C:\\Windows\\system32;C:\\Windows' });
  assert.ok(st.found, 'found via the registry PATH or the usual install folders');
  assert.match(st.exe, /claude(\.exe)?$/i);
  assert.notStrictEqual(st.loggedIn, false, `signed in (${st.detail})`);
});

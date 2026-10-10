'use strict';
// The build check fails when a secret or personal file is planted in the installer. Builds small fake
// installers (nested 7z archives, like electron-builder's NSIS installer -> app-64.7z) with the 7-Zip
// that comes with electron-builder, then runs scripts/check_build.py on them.
const test = require('node:test');
const assert = require('node:assert');
const { spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const DESKTOP = path.resolve(__dirname, '..');
const SEVEN_ZIP = [process.env.JARVIS_7ZIP, path.join(DESKTOP, 'node_modules', 'electron-winstaller', 'vendor', '7z.exe'),
  'C:\\Program Files\\7-Zip\\7z.exe'].find((p) => p && fs.existsSync(p));

function write(root, rel, text) {
  fs.mkdirSync(path.dirname(path.join(root, rel)), { recursive: true });
  fs.writeFileSync(path.join(root, rel), text);
}

/** A fake installer: outer.7z holding $PLUGINSDIR/app-64.7z holding the app files. */
function fakeInstaller(dir, files) {
  const app = path.join(dir, 'app');
  const outer = path.join(dir, 'outer');
  write(app, 'Jarvis.exe', 'MZ fake');
  write(app, 'resources/templates/config.example.json', '{"google_client_secret": "TODO"}');
  write(app, 'resources/templates/.env.example', 'ANTHROPIC_API_KEY=\n');
  write(app, 'resources/templates/internship_apply/profile.example.yaml', 'personal:\n  name: "TODO: your full name"\n');
  write(app, 'resources/templates/internship_apply/config.local.example.yaml', 'target_roles: [TODO]\n');
  write(app, 'resources/backend/_internal/internship_apply/config.yaml', 'dry_run: true\n');
  for (const [rel, text] of Object.entries(files)) write(app, rel, text);
  fs.mkdirSync(path.join(outer, '$PLUGINSDIR'), { recursive: true });
  const z1 = spawnSync(SEVEN_ZIP, ['a', '-y', path.join(outer, '$PLUGINSDIR', 'app-64.7z'), '.'], { cwd: app });
  assert.strictEqual(z1.status, 0, String(z1.stderr));
  const installer = path.join(dir, 'Jarvis-Setup-test.7z');
  const z2 = spawnSync(SEVEN_ZIP, ['a', '-y', installer, '.'], { cwd: outer });
  assert.strictEqual(z2.status, 0, String(z2.stderr));
  return installer;
}

function check(installer) {
  const r = spawnSync('python', ['-B', path.join(DESKTOP, 'scripts', 'check_build.py'), installer], { encoding: 'utf8' });
  return { code: r.status, out: `${r.stdout}${r.stderr}` };
}

const skip = !SEVEN_ZIP && 'needs 7-Zip: run npm install in desktop/ first';

test('a clean installer passes and its contents are listed', { skip }, (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jarvis-bc-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const r = check(fakeInstaller(dir, {}));
  assert.strictEqual(r.code, 0, r.out);
  assert.match(r.out, /Installer contents: \d+ files/);
  const listing = fs.readFileSync(path.join(dir, 'Jarvis-Setup-test.7z.contents.txt'), 'utf8');
  assert.match(listing, /app-64\.7z!\/resources\/templates\/internship_apply\/profile\.example\.yaml/);
});

const fakeKey = 'sk-ant-' + 'api03-' + 'Q'.repeat(40);         // built here, so no key-shaped text is committed
const PLANTS = {
  '.env': ['resources/backend/.env', 'ANTHROPIC_API_KEY=x\n'],
  'profile.yaml': ['resources/userdata/internship_apply/data/profile.yaml', 'personal:\n  name: Jane Doe\n'],
  'config.local.yaml': ['resources/backend/_internal/internship_apply/config.local.yaml', 'dry_run: false\n'],
  'listings.db': ['resources/backend/_internal/internship_apply/data/listings.db', 'SQLite format 3'],
  'output file': ['resources/backend/_internal/internship_apply/output/acme_intern/resume.pdf', '%PDF'],
  'log': ['resources/backend/data/jarvis.log', 'started\n'],
  'Google token': ['resources/secrets/google_token.dpapi', 'xx'],
  'config.json': ['resources/backend/config.json', '{"google_client_secret": "x"}'],
  'key in a text file': ['resources/backend/_internal/settings.txt', `key = ${fakeKey}\n`],
};

for (const [what, [rel, text]] of Object.entries(PLANTS)) {
  test(`the build check fails on a planted ${what}`, { skip }, (t) => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jarvis-bc-'));
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const r = check(fakeInstaller(dir, { [rel]: text }));
    assert.strictEqual(r.code, 1, r.out);
    assert.match(r.out, /FAILED/);
    assert.ok(r.out.includes(rel), `names the file:\n${r.out}`);
  });
}

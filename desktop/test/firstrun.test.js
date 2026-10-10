'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const firstrun = require('../src/firstrun');

const REPO = path.resolve(__dirname, '..', '..');

function tmpHome(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jarvis-home-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

test('first run creates every data file from the committed templates', (t) => {
  const home = tmpHome(t);
  const { created } = firstrun.seed(REPO, home);
  for (const [tpl, mine] of firstrun.FILES) {
    const dest = path.join(home, mine);
    assert.ok(fs.existsSync(dest), mine);
    assert.ok(fs.readFileSync(dest).equals(fs.readFileSync(path.join(REPO, tpl))), `${mine} is a copy of ${tpl}`);
  }
  for (const d of firstrun.DIRS) assert.ok(fs.statSync(path.join(home, d)).isDirectory(), d);
  assert.ok(created.length >= firstrun.FILES.length);
  assert.ok(fs.readdirSync(path.join(home, 'notes')).length > 0, 'sample notes copied into a new notes folder');
  // Templates hold no personal values: every personal field starts out as TODO.
  const cfg = JSON.parse(fs.readFileSync(path.join(home, 'config.json'), 'utf8'));
  assert.strictEqual(cfg.google_client_secret, 'TODO');
  assert.strictEqual(cfg.fullscreen_on_start, true);
});

test('first run never overwrites a file that already exists', (t) => {
  const home = tmpHome(t);
  const mine = {
    'config.json': '{"model": "mine", "google_client_secret": "keep-me"}',
    '.env': 'MY_SETTING=1\n',
    'internship_apply/data/profile.yaml': 'personal:\n  name: "Jane Doe"\n',
    'internship_apply/config.local.yaml': 'target_roles: [Mine]\n',
    'notes/my note.md': '# Mine\n',
  };
  for (const [rel, text] of Object.entries(mine)) {
    fs.mkdirSync(path.dirname(path.join(home, rel)), { recursive: true });
    fs.writeFileSync(path.join(home, rel), text);
  }
  const { created, kept } = firstrun.seed(REPO, home);
  for (const [rel, text] of Object.entries(mine)) {
    assert.strictEqual(fs.readFileSync(path.join(home, rel), 'utf8'), text, `${rel} untouched`);
  }
  assert.ok(kept.some((p) => p.endsWith('profile.yaml')));
  assert.ok(created.some((p) => p.endsWith('manual_urls.txt')), 'only the missing file is created');
  assert.deepStrictEqual(fs.readdirSync(path.join(home, 'notes')), ['my note.md'], 'sample notes not mixed into your notes');
  // Running it again changes nothing.
  assert.deepStrictEqual(firstrun.seed(REPO, home).created, []);
});

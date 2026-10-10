'use strict';
// npm run migrate-data: copy your existing files from the repo (where the web version keeps them)
// into the installed app's data folder, %APPDATA%\Jarvis\home.
//
// Never overwrites your data: a file is copied only when the app's copy is missing or is still the
// untouched template it was created from on first run. Run it with Jarvis closed.
const fs = require('fs');
const path = require('path');
const firstrun = require('../src/firstrun');

const repo = path.resolve(__dirname, '..', '..');
const home = process.env.JARVIS_HOME || path.join(process.env.APPDATA || '', 'Jarvis', 'home');

// [file in the repo, the same file in the app's data folder, template it may still equal]
const templateOf = Object.fromEntries(firstrun.FILES.map(([tpl, mine]) => [mine, tpl]));
const FILES = ['config.json', 'internship_apply/config.local.yaml', 'internship_apply/data/profile.yaml',
  'internship_apply/data/manual_urls.txt', 'internship_apply/data/listings.db', 'internship_apply/data/llm_calls.db',
  'secrets/google_token.dpapi', 'data/cards.json', 'data/reminders.json', 'data/standing.json', 'data/schedules.json'];
const DIRS = ['notes', 'internship_apply/output'];

const same = (a, b) => fs.existsSync(a) && fs.existsSync(b) && fs.readFileSync(a).equals(fs.readFileSync(b));
let copied = 0;
let kept = 0;

function copy(rel) {
  const src = path.join(repo, rel);
  const dest = path.join(home, rel);
  if (!fs.existsSync(src)) return;
  const tpl = templateOf[rel] ? path.join(repo, templateOf[rel]) : null;
  if (fs.existsSync(dest) && !(tpl && same(dest, tpl))) {
    kept += 1;
    console.log(`kept (already yours): ${rel}`);
    return;
  }
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.copyFileSync(src, dest);
  copied += 1;
  console.log(`copied: ${rel}`);
}

function walk(rel) {
  const dir = path.join(repo, rel);
  if (!fs.existsSync(dir)) return;
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const r = path.join(rel, e.name);
    if (e.isDirectory()) walk(r);
    else if (e.isFile()) copy(r);
  }
}

if (path.resolve(home) === repo) {
  console.log('JARVIS_HOME is the repo itself: nothing to migrate.');
  process.exit(0);
}
console.log(`from ${repo}\n  to ${home}\n`);
FILES.forEach(copy);
DIRS.forEach(walk);
console.log(`\n${copied} copied, ${kept} kept. Your files in the repo are unchanged.`);

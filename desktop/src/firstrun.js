'use strict';
// First run: give the data folder everything Jarvis needs, copied from the committed templates.
// Never overwrites: a file that already exists is left exactly as it is, every time.
const fs = require('fs');
const path = require('path');

// [template (relative to the templates folder), your copy (relative to the data folder)]
const FILES = [
  ['config.example.json', 'config.json'],
  ['.env.example', '.env'],
  ['internship_apply/profile.example.yaml', 'internship_apply/data/profile.yaml'],
  ['internship_apply/config.local.example.yaml', 'internship_apply/config.local.yaml'],
  ['internship_apply/manual_urls.example.txt', 'internship_apply/data/manual_urls.txt'],
];
const DIRS = ['data', 'secrets', 'notes', 'internship_apply/data', 'internship_apply/output', 'internship_apply/logs'];

function copyIfMissing(src, dest, created, kept) {
  if (fs.existsSync(dest)) {
    kept.push(dest);
    return;
  }
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.copyFileSync(src, dest, fs.constants.COPYFILE_EXCL);             // EXCL: fails rather than overwrite
  created.push(dest);
}

function copyTreeIfMissing(srcDir, destDir, created, kept) {
  for (const entry of fs.readdirSync(srcDir, { withFileTypes: true })) {
    const s = path.join(srcDir, entry.name);
    const d = path.join(destDir, entry.name);
    if (entry.isDirectory()) copyTreeIfMissing(s, d, created, kept);
    else if (entry.isFile() && entry.name.endsWith('.md')) copyIfMissing(s, d, created, kept);
  }
}

/** Create missing data files from templates. Returns { created: [paths], kept: [paths] }. */
function seed(templates, home) {
  const created = [];
  const kept = [];
  const notesExisted = fs.existsSync(path.join(home, 'notes'));
  for (const d of DIRS) fs.mkdirSync(path.join(home, d), { recursive: true });
  for (const [tpl, mine] of FILES) {
    const src = path.join(templates, tpl);
    if (fs.existsSync(src)) copyIfMissing(src, path.join(home, mine), created, kept);
  }
  // The sample notes, only into a brand-new notes folder (never mixed into notes you already have).
  const notesSrc = path.join(templates, 'notes');
  if (!notesExisted && fs.existsSync(notesSrc) && path.resolve(notesSrc) !== path.resolve(home, 'notes')) {
    copyTreeIfMissing(notesSrc, path.join(home, 'notes'), created, kept);
  }
  return { created, kept };
}

module.exports = { seed, FILES, DIRS };

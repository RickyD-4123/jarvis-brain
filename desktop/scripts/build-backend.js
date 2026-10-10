'use strict';
// npm run build:backend: freeze the Python backend with PyInstaller into build/backend/jarvis-backend/
// (Python itself included, so the installed app needs no Python on the computer).
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const desktop = path.resolve(__dirname, '..');
const python = process.env.JARVIS_PYTHON || 'python';
const run = (args) => spawnSync(python, args, { cwd: desktop, stdio: 'inherit', windowsHide: true });

const probe = spawnSync(python, ['-m', 'PyInstaller', '--version'], { encoding: 'utf8', windowsHide: true });
if (probe.status !== 0) {
  console.error('PyInstaller is missing. Install the build tools once:\n  python -m pip install -r desktop/requirements-build.txt');
  process.exit(1);
}
const deps = spawnSync(python, ['-c', 'import yaml, docx, fpdf, playwright'], { encoding: 'utf8', windowsHide: true });
if (deps.status !== 0) {
  console.error('Jarvis\'s Python packages are missing. Install them once:\n  python -m pip install -r requirements.txt');
  process.exit(1);
}

fs.rmSync(path.join(desktop, 'build', 'backend'), { recursive: true, force: true });
const r = run(['-m', 'PyInstaller', '--noconfirm', '--clean',
  '--distpath', path.join('build', 'backend'),
  '--workpath', path.join('build', 'pyinstaller-work'),
  path.join('backend', 'jarvis-backend.spec')]);
if (r.status !== 0) process.exit(r.status || 1);
const exe = path.join(desktop, 'build', 'backend', 'jarvis-backend', 'jarvis-backend.exe');
if (!fs.existsSync(exe)) {
  console.error(`PyInstaller finished but ${exe} is missing`);
  process.exit(1);
}
console.log(`backend built: ${exe}`);

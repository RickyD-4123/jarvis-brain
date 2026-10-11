"""Update the desktop app from the code folder: "update the app", then Install.

1. Bump the patch version in desktop/package.json (and its lockfile), so the installer and GET /health
   show the new version.
2. npm run dist in desktop/: builds the backend exe and the installer, then the build check (which fails
   if anything personal or secret is in the installer). A failure puts the old version number back.
3. Commit the version bump ("Desktop app vX.Y.Z"; the pre-commit hook runs) and make sure the installed
   app's config knows where the code folder is (source_dir), so it can build itself too.
4. A desk card with Install. Install starts a small helper outside Jarvis (via WMI, so closing Jarvis
   doesn't take it down) that waits for Jarvis to close, runs the installer silently over the old version
   (your data in %APPDATA%\\Jarvis\\home is untouched), and starts the new one. Run from the desktop app,
   Jarvis then closes himself; from the web version, close the desktop app first if it's open.

Only your own words at the desk (or the button on the card) start or install an update.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

BUILD_TIMEOUT = 45 * 60
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
VERSION_RE = re.compile(r'("version"\s*:\s*")(\d+)\.(\d+)\.(\d+)(")')


def bump(version):
    major, minor, patch = (int(x) for x in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def installed_app_exe():
    """Where the desktop app's Jarvis.exe lives: next to this backend when packaged, else the default."""
    if getattr(sys, "frozen", False):                    # <install>/resources/backend/jarvis-backend.exe
        exe = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(sys.executable))), "Jarvis.exe")
        if os.path.isfile(exe):
            return exe
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Jarvis", "Jarvis.exe")


class AppUpdater:
    def __init__(self, source, cards, packaged_home=None, run=None, launch=None, app_running=None, frozen=None,
                 logs_dir=None):
        """source: the git code folder (None: unavailable). packaged_home: the desktop app's data folder.
        run(cmd, cwd, timeout, log_path) -> exit code; launch(script_path); app_running() -> bool: for tests."""
        self.source = os.path.realpath(source) if source else None
        self.cards = cards
        self.packaged_home = packaged_home or os.path.join(os.environ.get("APPDATA", ""), "Jarvis", "home")
        self.run = run or self._run
        self.launch = launch or self._launch
        self.app_running = app_running or self._app_running
        self.frozen = getattr(sys, "frozen", False) if frozen is None else frozen
        self.logs = os.path.realpath(logs_dir or os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                                                              "Jarvis", "builds"))
        self.state, self.lock, self.thread = {}, threading.Lock(), None

    def status(self):
        with self.lock:
            return dict(self.state)

    # ---------------------------------------------------------------- build

    def start(self):
        if not self.source or not os.path.isfile(os.path.join(self.source, "desktop", "package.json")):
            return "I can't find the desktop app's code, sir. Set source_dir in config.json and ask me again."
        with self.lock:
            if self.state.get("status") == "building":
                return "I'm already building the app, sir. It takes a few minutes."
            self.state = {"status": "building", "started": time.time()}
        self.thread = threading.Thread(target=self._build, daemon=True)
        self.thread.start()
        return ("Building the desktop app now, sir: a new version number, the backend, the installer and its "
                "checks. It takes several minutes. I'll put a card up when it's ready to install.")

    def _build(self):
        desk = os.path.join(self.source, "desktop")
        pkg, lock = os.path.join(desk, "package.json"), os.path.join(desk, "package-lock.json")
        originals = {}
        try:
            for p in (pkg, lock):
                if os.path.isfile(p):
                    with open(p, encoding="utf-8", newline="") as f:
                        originals[p] = f.read()
            m = VERSION_RE.search(originals[pkg])
            if not m:
                raise RuntimeError("desktop/package.json has no version")
            old = ".".join(m.group(2, 3, 4))
            new = bump(old)
            self._write(pkg, VERSION_RE.sub(lambda m: m.group(1) + new + m.group(5), originals[pkg], count=1))
            if lock in originals:                         # the lockfile's own two copies, at the top
                text, n = originals[lock], 0

                def swap(m):
                    nonlocal n
                    n += 1
                    return m.group(1) + new + m.group(5) if n <= 2 and ".".join(m.group(2, 3, 4)) == old else m.group(0)
                self._write(lock, VERSION_RE.sub(swap, text))
            os.makedirs(self.logs, exist_ok=True)
            log_path = os.path.join(self.logs, f"app-{new}.log")
            with self.lock:
                self.state.update(version=new, old=old, log=log_path)
            if not os.path.isdir(os.path.join(desk, "node_modules")):
                if self.run(["cmd", "/c", "npm", "ci"], desk, BUILD_TIMEOUT, log_path):
                    raise RuntimeError("npm ci failed")
            if self.run(["cmd", "/c", "npm", "run", "dist"], desk, BUILD_TIMEOUT, log_path):
                raise RuntimeError(f"the build failed: the log is {log_path}")
            installer = os.path.join(desk, "dist", f"Jarvis-Setup-{new}.exe")
            if not os.path.isfile(installer):
                raise RuntimeError(f"the build finished but {os.path.basename(installer)} isn't there")
            files = [os.path.relpath(p, self.source).replace(os.sep, "/") for p in originals]
            c = subprocess.run(["git", "commit", "-q", "-m", f"Desktop app v{new}\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
                                "--", *files], cwd=self.source, capture_output=True, text=True, creationflags=NO_WINDOW)
            committed = c.returncode == 0
            self._remember_source()
            with self.lock:
                self.state.update(status="ready", installer=installer, committed=committed, finished=time.time())
            self.cards.add("app", "App update ready", f"Version {new} is built and checked (it was {old}). "
                           "Install closes me, installs it over the old version and opens me again; your data stays.",
                           "normal", ref={"app": self.status()})
        except Exception as e:
            for p, text in originals.items():             # the version only moves on with a good build
                self._write(p, text)
            with self.lock:
                self.state.update(status="failed", error=str(e), finished=time.time())
            self.cards.add("app", "App update failed", f"I couldn't build the app: {e}", "normal",
                           ref={"app": self.status()})

    @staticmethod
    def _write(path, text):
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def _remember_source(self):
        """The installed app reads its own config.json: tell it where the code is (only if it's not set).
        Its agent memory starts from the web version's, if it has none yet (one Jarvis, one set of notes)."""
        mine = os.path.join(self.source, "data", "agent-memory")
        theirs = os.path.join(self.packaged_home, "data", "agent-memory")
        if os.path.isdir(self.packaged_home) and os.path.isdir(mine) and not any(
                f.endswith(".md") and os.path.getsize(os.path.join(theirs, f)) for f in (os.listdir(theirs) if os.path.isdir(theirs) else [])):
            shutil.copytree(mine, theirs, dirs_exist_ok=True)
        cfg = os.path.join(self.packaged_home, "config.json")
        if not os.path.isfile(cfg):
            return
        with open(cfg, encoding="utf-8") as f:
            data = json.load(f)
        current = data.get("source_dir")
        if isinstance(current, str) and current and not current.upper().startswith("TODO") and os.path.isdir(current):
            return
        data["source_dir"] = self.source
        tmp = cfg + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, cfg)

    @staticmethod
    def _run(cmd, cwd, timeout, log_path):
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"\n$ {' '.join(cmd)}\n")
            log.flush()
            try:
                return subprocess.run(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, timeout=timeout,
                                      creationflags=NO_WINDOW).returncode
            except subprocess.TimeoutExpired:
                log.write(f"timed out after {timeout} s\n")
                return 1

    # ---------------------------------------------------------------- install

    def install(self):
        """Returns {"ok", "say", "shutdown"}: shutdown means Jarvis should close himself now."""
        s = self.status()
        if s.get("status") != "ready":
            return {"ok": False, "say": "There's no app update ready to install, sir. Say \"update the app\" first."}
        if not os.path.isfile(s["installer"]):
            return {"ok": False, "say": "The installer has gone missing, sir. Say \"update the app\" to build it again."}
        if not self.frozen and self.app_running():
            return {"ok": False, "say": "The desktop app is open, sir. Close it, then press Install again."}
        exe = installed_app_exe()
        log = os.path.join(self.logs, f"install-{s['version']}.log")
        script = os.path.join(self.logs, f"install-{s['version']}.ps1")
        q = lambda p: "'" + p.replace("'", "''") + "'"       # a PowerShell single-quoted string
        with open(script, "w", encoding="utf-8") as f:
            f.write("$ErrorActionPreference = 'Continue'\n"
                    f"$log = {q(log)}\n"
                    "\"$(Get-Date -Format s) waiting for Jarvis to close\" | Out-File $log -Append -Encoding utf8\n"
                    "$deadline = (Get-Date).AddMinutes(3)\n"
                    "while ((Get-Process -Name 'Jarvis','jarvis-backend' -ErrorAction SilentlyContinue) -and "
                    "((Get-Date) -lt $deadline)) { Start-Sleep -Milliseconds 500 }\n"
                    "Get-Process -Name 'jarvis-backend' -ErrorAction SilentlyContinue | Stop-Process -Force\n"
                    f"$p = Start-Process -FilePath {q(s['installer'])} -ArgumentList '/S' -Wait -PassThru\n"
                    "\"$(Get-Date -Format s) installer exit $($p.ExitCode)\" | Out-File $log -Append -Encoding utf8\n"
                    f"$exe = {q(exe)}\n"
                    "if (-not (Test-Path $exe)) { $exe = Join-Path $env:LOCALAPPDATA 'Programs\\Jarvis\\Jarvis.exe' }\n"
                    "Start-Process -FilePath $exe\n"
                    "\"$(Get-Date -Format s) started $exe\" | Out-File $log -Append -Encoding utf8\n")
        self.launch(script)
        with self.lock:
            self.state["status"] = "installing"
        if self.frozen:
            return {"ok": True, "shutdown": True,
                    "say": f"Installing version {s['version']}, sir. I'll close now and be back in a minute."}
        return {"ok": True, "shutdown": False,
                "say": f"Installing version {s['version']} of the desktop app, sir. It will open by itself when it's done."}

    @staticmethod
    def _launch(script):
        """Start the helper through WMI: a process created that way isn't tied to Jarvis's process tree."""
        cmdline = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{script}"'
        ps = ("Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine = '"
              + cmdline.replace("'", "''") + "'} | Out-Null")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, creationflags=NO_WINDOW,
                       timeout=60)

    @staticmethod
    def _app_running():
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Jarvis.exe", "/NH"], capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout
        return "Jarvis.exe" in out

"""Local hands: Jarvis opens apps, opens pages in your browser, and manages files in your workspace.

Deliberately NOT a free-form shell. The same model reads your email and documents;
a shell it could type into would let any email run commands on this machine. Instead:

  open_app      launches an app from your Start menu by its Windows app id (explorer.exe
                shell:AppsFolder\\<id>). The model names the app; it never supplies a command line.
  open_url      opens an http(s) page in your real Chrome (already signed in to Google, so a fresh
                Doc opens straight away). After a turn has read outside content, only Google links.
  list_folder, make_folder, move_path, copy_path, rename_path, open_path
                files, only inside the workspace folders in config.json ("workspace_dirs";
                the root set in server.py, WORKSPACE_ROOT). Nothing is overwritten or deleted, and
                open_path never opens anything executable.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse

WINDOWS = sys.platform == "win32"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CHROME_PATHS = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")]
TRUSTED_HOSTS = ("docs.google.com", "drive.google.com", "mail.google.com", "calendar.google.com",
                 "sheets.google.com", "slides.google.com")
# Files open with their normal app; these never do (they would run code).
NEVER_OPEN = {".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
              ".msi", ".msp", ".scr", ".lnk", ".url", ".reg", ".hta", ".cpl", ".jar", ".py", ".pyw", ".sh", ".appref-ms"}
ALIASES = {"vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
           "explorer": "file explorer", "files": "file explorer", "browser": "google chrome", "chrome": "google chrome",
           "edge": "microsoft edge", "calc": "calculator", "zoom": "zoom workplace", "terminal": "terminal"}
SIMILAR = {"visual studio code": "Cursor"}


class HandError(Exception):
    """A local action that can't or won't be done; the message is spoken, so keep it plain."""


class Hands:
    def __init__(self, config_path, default_root, dry_run=False):
        """dry_run records what would be launched instead of launching it (for tests)."""
        self.config_path, self.default_root, self.dry_run = config_path, default_root, dry_run
        self.launched, self._apps, self.lock = [], None, threading.Lock()

    # -- apps
    def apps(self, refresh=False):
        """{lowercase name: (display name, app id)} from the Start menu, minus uninstallers and web links."""
        with self.lock:
            if self._apps is None or refresh:
                found = {}
                if WINDOWS:
                    out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                                          "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
                                         capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW).stdout
                    try:
                        rows = json.loads(out or "[]")
                    except ValueError:
                        rows = []
                    for row in rows if isinstance(rows, list) else [rows]:
                        name, appid = str(row.get("Name", "")), str(row.get("AppID", ""))
                        if (name and appid and not re.search(r"uninstall|support|help|readme|release notes", name, re.I)
                                and not re.match(r"https?:", appid, re.I)):
                            found[name.lower()] = (name, appid)
                self._apps = found
            return dict(self._apps)

    def _launch(self, argv):
        if self.dry_run:
            self.launched.append(argv)
            return
        subprocess.Popen(argv, close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))

    def open_app(self, name):
        want = ALIASES.get(str(name).strip().lower(), str(name).strip().lower())
        apps = self.apps()
        hit = apps.get(want) or next((v for k, v in apps.items() if k.startswith(want)), None) \
            or next((v for k, v in apps.items() if want in k), None)
        if not hit:
            apps = self.apps(refresh=True)
            hit = apps.get(want) or next((v for k, v in apps.items() if want in k), None)
        if not hit:
            near = SIMILAR.get(want)
            raise HandError(f"{name} isn't installed on this computer." +
                            (f" {near} is, if you'd like that instead." if near and near.lower() in apps else ""))
        display, appid = hit
        self._launch(["explorer.exe", f"shell:AppsFolder\\{appid}"])
        return {"id": appid, "app": display, "summary": f"opened {display}"}

    # -- the browser
    @staticmethod
    def check_url(url):
        u = urllib.parse.urlsplit(str(url).strip())
        if u.scheme not in ("http", "https") or not u.netloc:
            raise HandError("I only open web pages that start with http or https.")
        return urllib.parse.urlunsplit(u)

    def chrome(self):
        return next((p for p in CHROME_PATHS if os.path.isfile(p)), None)

    def open_url(self, url, new_window=False):
        url = self.check_url(url)
        exe = self.chrome()
        if exe:
            self._launch([exe, "--new-window", url] if new_window else [exe, url])
            browser = "Chrome"
        elif WINDOWS:
            if not self.dry_run:
                os.startfile(url)                         # the default browser
            else:
                self.launched.append(["startfile", url])
            browser = "your default browser"
        else:
            raise HandError("There's no browser I can open here.")
        return {"id": url, "link": url, "summary": f"opened in {browser}", "host": urllib.parse.urlsplit(url).netloc}

    # -- the workspace
    def roots(self):
        """The workspace roots: the primary root first (relative paths start there), then any extra
        folders listed in config.json's "workspace_dirs"."""
        try:
            with open(self.config_path, encoding="utf-8") as f:
                dirs = json.load(f).get("workspace_dirs") or []
        except (OSError, ValueError):
            dirs = []
        os.makedirs(self.default_root, exist_ok=True)
        roots = [os.path.realpath(self.default_root)]
        for d in dirs:
            r = os.path.realpath(os.path.expandvars(os.path.expanduser(str(d)))) if str(d).strip() else ""
            if r and os.path.isdir(r) and r.lower() not in [x.lower() for x in roots]:
                roots.append(r)
        return roots

    def resolve(self, path, must_exist=True):
        """An absolute path inside a workspace root, or HandError. Relative paths start in the first root."""
        roots = self.roots()
        raw = os.path.expandvars(os.path.expanduser(str(path or "").strip().strip('"')))
        if not os.path.isabs(raw):
            # A relative path that starts with a root's own name ("University", "University/Calculus")
            # means that root itself, unless a real sub-folder by that name exists.
            first, _, rest = raw.replace("\\", "/").lstrip("/").partition("/")
            for r in roots:
                if first and first.lower() == os.path.basename(r).lower() and not os.path.exists(os.path.join(roots[0], first)):
                    raw = os.path.join(r, rest)
                    break
        full = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(roots[0], raw))
        if not any(full == r or full.startswith(r + os.sep) for r in roots):
            raise HandError("That's outside your workspace folders, so I won't touch it. "
                            "Add the folder to workspace_dirs in config.json if you want me to.")
        if must_exist and not os.path.exists(full):
            raise HandError(f"There's no {os.path.basename(full) or 'such folder'} in your workspace.")
        return full

    def show(self, full):
        for r in self.roots():
            if full == r or full.startswith(r + os.sep):
                return os.path.join(os.path.basename(r), os.path.relpath(full, r)).replace(os.sep, "/").rstrip("/.")
        return full

    def list_folder(self, path=""):
        full = self.resolve(path or self.roots()[0])
        if not os.path.isdir(full):
            raise HandError("That's a file, not a folder.")
        items = []
        for name in sorted(os.listdir(full), key=str.lower)[:200]:
            p = os.path.join(full, name)
            items.append({"name": name, "folder": os.path.isdir(p),
                          "size": None if os.path.isdir(p) else os.path.getsize(p)})
        return {"folder": self.show(full), "items": items, "roots": [self.show(r) for r in self.roots()]}

    def make_folder(self, path):
        full = self.resolve(path, must_exist=False)
        if os.path.exists(full):
            raise HandError(f"{self.show(full)} already exists.")
        os.makedirs(full)
        return {"id": full, "summary": f"created {self.show(full)}"}

    def _destination(self, src, dest):
        target = self.resolve(dest, must_exist=False)
        if os.path.isdir(target):
            target = os.path.join(target, os.path.basename(src))
            self.resolve(target, must_exist=False)
        if os.path.exists(target):
            raise HandError(f"{self.show(target)} already exists, and I never overwrite.")
        if target == src or target.startswith(src + os.sep):
            raise HandError("I can't move a folder inside itself.")
        if not os.path.isdir(os.path.dirname(target)):
            raise HandError(f"There's no folder {self.show(os.path.dirname(target))} to put it in.")
        return target

    def move_path(self, source, destination):
        src = self.resolve(source)
        dst = self._destination(src, destination)
        shutil.move(src, dst)
        return {"id": dst, "summary": f"moved {self.show(src)} to {self.show(dst)}"}

    def copy_path(self, source, destination):
        src = self.resolve(source)
        dst = self._destination(src, destination)
        (shutil.copytree if os.path.isdir(src) else shutil.copy2)(src, dst)
        return {"id": dst, "summary": f"copied {self.show(src)} to {self.show(dst)}"}

    def rename_path(self, path, new_name):
        src = self.resolve(path)
        new_name = str(new_name).strip()
        if not new_name or re.search(r'[\\/:*?"<>|]', new_name) or new_name in (".", ".."):
            raise HandError("That isn't a valid file name.")
        return self.move_path(src, os.path.join(os.path.dirname(src), new_name))

    def open_path(self, path):
        full = self.resolve(path)
        if os.path.isfile(full) and os.path.splitext(full)[1].lower() in NEVER_OPEN:
            raise HandError("That file would run a program, so I won't open it.")
        if self.dry_run:
            self.launched.append(["open", full])
        elif WINDOWS:
            os.startfile(full)
        else:
            raise HandError("Opening files is only set up on Windows.")
        return {"id": full, "summary": f"opened {self.show(full)}"}

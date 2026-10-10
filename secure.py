"""Owner-only files: config.json, the Google token and every local state file.

On Windows, chmod 600 only toggles the read-only flag, so "readable by my account only" is done
with the file's access-control list: inheritance removed, one entry left (you, full control).
Locked folders pass that on to every file created inside them. On other systems, chmod 600/700.
Called at boot and after every write.
"""
import getpass
import os
import stat
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
WINDOWS = sys.platform == "win32"


def principal():
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{getpass.getuser()}" if domain else getpass.getuser()


def lock(path):
    """Make path readable and writable by this OS account only. True on success."""
    if not os.path.exists(path):
        return False
    is_dir = os.path.isdir(path)
    if not WINDOWS:
        os.chmod(path, 0o700 if is_dir else 0o600)
        return True
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    grant = principal() + (":(OI)(CI)F" if is_dir else ":F")
    # Drop inherited entries and reset ours...
    subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", grant], capture_output=True, text=True, creationflags=flags)
    # ...then remove every OTHER explicit entry (e.g. a "Users: read" someone added), allow or deny.
    me = principal().lower()
    for entry in entries(path):
        who = entry.split(":(", 1)[0].strip()
        if who and who.lower() != me:
            subprocess.run(["icacls", path, "/remove", who], capture_output=True, text=True, creationflags=flags)
    return is_locked(path)                                # trust the result on disk, not the exit code


def entries(path):
    """The access entries on path, as icacls prints them (Windows), or the mode (elsewhere)."""
    if not WINDOWS:
        return [oct(stat.S_IMODE(os.stat(path).st_mode))]
    out = subprocess.run(["icacls", path], capture_output=True, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    lines = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith("Successfully")]
    if lines:
        lines[0] = lines[0][len(path):].strip() if lines[0].startswith(path) else lines[0].split(" ", 1)[-1].strip()
    return [l for l in lines if l]


def is_locked(path):
    """True if only this account can read path (Windows: a single entry, for us; else mode 600/700)."""
    if not os.path.exists(path):
        return True                                       # nothing there, nothing to read
    if not WINDOWS:
        return stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0
    ents = entries(path)
    me = principal().lower()
    return bool(ents) and all(e.lower().startswith(me + ":") for e in ents)


def secret_paths(root=ROOT):
    """Everything that must be owner-only: config (and any copies of it), secrets/ and data/."""
    paths = []
    for name in sorted(os.listdir(root)):
        if name.startswith("config.json"):
            paths.append(os.path.join(root, name))
    for folder in ("secrets", "data"):
        d = os.path.join(root, folder)
        if os.path.isdir(d):
            paths.append(d)
            for dp, dirs, files in os.walk(d):
                paths += [os.path.join(dp, x) for x in dirs + files]
    return paths


def lock_all(root=ROOT):
    """Lock every secret path (folders first, so new files inherit). Returns [(path, ok)]."""
    for folder in ("secrets", "data"):
        os.makedirs(os.path.join(root, folder), exist_ok=True)
    return [(p, lock(p)) for p in secret_paths(root)]

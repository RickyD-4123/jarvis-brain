"""Lists everything inside the built installer and fails if anything personal or secret is in it.

    python scripts/check_build.py                       # the newest dist/Jarvis-Setup-*.exe
    python scripts/check_build.py path/to/Setup.exe     # a given installer (or any .7z/.zip, or a folder)

It unpacks every layer (the NSIS installer -> app-64.7z -> app.asar, and the frozen backend's
PyInstaller archive) into a temp folder, writes the full listing next to the installer
(<installer>.contents.txt), and fails on:
  - files that must never ship: .env (any *.env / .env.*), config.json, config.local.yaml,
    profile.yaml, listings.db / any .db, manual_urls.txt, personal_strings.local.txt, secrets/,
    *.dpapi, *.pem, *.key, token files, logs (*.log, logs/, applications.jsonl) and
    internship_apply/output/. The committed templates (*.example.*) are allowed.
  - text (and the backend's compiled modules) holding a key or token pattern, or any string from
    personal_strings.local.txt (same patterns as scripts/check_no_personal_data.py).
Uses the full 7-Zip that electron-builder installs (node_modules/electron-winstaller/vendor/7z.exe).
"""
import glob
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

DESKTOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(DESKTOP)
sys.path.insert(0, os.path.join(REPO, "scripts"))
import check_no_personal_data as scan  # noqa: E402

# The full 7-Zip (7z.exe + 7z.dll) is needed: the cut-down 7za can't open NSIS installers. electron-builder
# brings one along (its electron-winstaller dependency); an installed 7-Zip or JARVIS_7ZIP also work.
SEVEN_ZIP_CANDIDATES = [os.environ.get("JARVIS_7ZIP", ""),
                        os.path.join(DESKTOP, "node_modules", "electron-winstaller", "vendor", "7z.exe"),
                        r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"]
SEVEN_ZIP = next((p for p in SEVEN_ZIP_CANDIDATES if p and os.path.isfile(p)), None)
TEXT_EXT = {".js", ".cjs", ".mjs", ".json", ".py", ".txt", ".yaml", ".yml", ".html", ".htm", ".md", ".env", ".ini",
            ".cfg", ".toml", ".csv", ".xml", ".ps1", ".bat", ".cmd", ".sh", ".conf", ""}
MAX_TEXT = 8 * 1024 * 1024

# (reason, regex on the forward-slash path, case-insensitive)
FORBIDDEN = [
    (".env file", r"(?:^|/)(?:\.env(?:\.(?!example$)[^/]+)?|[^/]+\.env)$"),
    ("config.json (keys)", r"(?:^|/)config\.json(?:\.[^/]+)?$"),
    ("config.local.yaml", r"(?:^|/)config\.local\.(?!example\.)[^/]+$"),
    ("profile.yaml", r"(?:^|/)profile\.ya?ml$"),
    ("database", r"\.(?:db|sqlite3?|db-journal|db-wal)$"),
    ("manual_urls.txt", r"(?:^|/)manual_urls\.txt$"),
    ("personal strings list", r"(?:^|/)personal_strings\.local\.txt$"),
    ("secrets folder", r"(?:^|/)secrets/"),
    ("DPAPI secret", r"\.dpapi$"),
    ("private key", r"\.(?:pem|key|p12|pfx)$|(?:^|/)id_(?:rsa|ed25519)$"),
    ("token file", r"(?:^|/)[^/]*token[^/]*\.(?:json|txt|dpapi|token)$|\.token$"),
    ("log file", r"\.log(?:\.\d+)?$|(?:^|/)applications\.jsonl$|(?:^|/)internship_apply/logs/"),
    ("internship output", r"(?:^|/)internship_apply/output/"),
    ("Jarvis data folder", r"(?:^|/)home/(?:data|secrets)/"),
]
FORBIDDEN_RES = [(why, re.compile(rx, re.I)) for why, rx in FORBIDDEN]
# Libraries carry the bare PEM header as a constant (cryptography's ssh.py: _SK_START = b"-----BEGIN
# OPENSSH PRIVATE KEY-----"), so in shipped code a private key only counts with a base64 body after it.
KEY_BODY = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----\s*[A-Za-z0-9+/=]{40,}")


def seven_zip(*args):
    if not SEVEN_ZIP:
        sys.exit("7-Zip not found: run npm install in desktop/ (or install 7-Zip from 7-zip.org).")
    return subprocess.run([SEVEN_ZIP, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def extract_archive(path, dest):
    r = seven_zip("x", "-y", f"-o{dest}", path)
    return r.returncode == 0


def read_asar(path):
    """{name: bytes} for every file in an Electron app.asar (Chromium pickle header + JSON index)."""
    with open(path, "rb") as f:
        data = f.read()
    header_size = struct.unpack_from("<I", data, 4)[0]
    json_len = struct.unpack_from("<I", data, 12)[0]
    header = json.loads(data[16:16 + json_len].decode("utf-8"))
    base = 8 + header_size
    out = {}

    def walk(node, prefix):
        for name, entry in node.get("files", {}).items():
            p = f"{prefix}{name}"
            if "files" in entry:
                walk(entry, p + "/")
            elif entry.get("unpacked"):
                out[p] = None                                     # lives in app.asar.unpacked/, listed from disk
            elif "offset" in entry:
                start = base + int(entry["offset"])
                out[p] = data[start:start + int(entry["size"])]
    walk(header, "")
    return out


def pyinstaller_modules(exe):
    """{module name: source-ish bytes} from a PyInstaller exe's PYZ archive, or {} if it can't be read."""
    try:
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    except ImportError:
        return {}
    try:
        carchive = CArchiveReader(exe)
    except Exception:
        return {}
    out = {}
    for name in carchive.toc:
        if not name.endswith(".pyz"):
            continue
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pyz")
        try:
            tmp.write(carchive.extract(name))
            tmp.close()
            pyz = ZlibArchiveReader(tmp.name)
            for mod in pyz.toc:
                try:
                    out[mod] = pyz.extract(mod, raw=True) or b""
                except TypeError:                                 # older PyInstaller: no raw=
                    out[mod] = b""
                except Exception:
                    out[mod] = b""
        finally:
            os.unlink(tmp.name)
    return out


def collect(target, work):
    """[(path inside the installer, bytes or None)] for every file, unpacking nested archives."""
    entries = []

    def add_tree(root, prefix):
        for dp, _dirs, files in os.walk(root):
            for fn in files:
                full = os.path.join(dp, fn)
                rel = prefix + os.path.relpath(full, root).replace(os.sep, "/")
                low = fn.lower()
                if low.endswith((".7z", ".zip")) and os.path.getsize(full) > 0:
                    sub = tempfile.mkdtemp(dir=work)
                    if extract_archive(full, sub):
                        entries.append((rel, None))
                        add_tree(sub, rel + "!/")
                        continue
                if low == "app.asar":
                    entries.append((rel, None))
                    try:
                        for name, data in read_asar(full).items():
                            entries.append((f"{rel}!/{name}", data))
                    except Exception as e:
                        entries.append((f"{rel}!/<unreadable: {e}>", None))
                    continue
                if low == "jarvis-backend.exe":
                    entries.append((rel, None))
                    for mod, data in pyinstaller_modules(full).items():   # a module, shown as its file
                        entries.append((f"{rel}!/{mod.replace('.', '/')}.py", data))
                    continue
                data = None
                ext = os.path.splitext(low)[1]
                if ext in TEXT_EXT and os.path.getsize(full) <= MAX_TEXT:
                    with open(full, "rb") as f:
                        data = f.read()
                entries.append((rel, data))

    if os.path.isdir(target):
        add_tree(target, "")
    else:
        top = tempfile.mkdtemp(dir=work)
        if not extract_archive(target, top):
            sys.exit(f"Could not open {target} with 7-Zip.")
        add_tree(top, "")
    return entries


def check(entries, strings):
    problems = []
    for rel, data in entries:
        path = rel.replace("\\", "/")
        inner = path.split("!/")[-1]
        for why, rx in FORBIDDEN_RES:
            if rx.search(inner) or rx.search(path):
                problems.append(f"{rel}: {why}")
                break
        if data:
            text = data.decode("utf-8", "replace") if not isinstance(data, str) else data
            for name, rx in scan.KEY_RES:
                m = rx.search(text)
                if m and name == "private key":
                    m = KEY_BODY.search(text)
                if m:
                    problems.append(f"{rel}: {name} ({scan.mask(m.group())})")
            low = text.lower()
            for s in strings:
                if s.lower() in low:
                    problems.append(f"{rel}: personal string ({scan.mask(s)})")
    return problems


def newest_installer():
    found = sorted(glob.glob(os.path.join(DESKTOP, "dist", "Jarvis-Setup-*.exe")), key=os.path.getmtime)
    return found[-1] if found else None


def main(argv):
    target = argv[0] if argv else newest_installer()
    if not target or not os.path.exists(target):
        print("No installer found: build one with npm run dist (or pass a path).")
        return 2
    work = tempfile.mkdtemp(prefix="jarvis-build-check-")
    try:
        entries = collect(target, work)
        strings = scan.personal_strings() or []
        problems = check(entries, strings)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    listing = f"{target}.contents.txt" if os.path.isfile(target) else os.path.join(target, "..", "contents.txt")
    try:
        with open(listing, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(rel for rel, _ in entries)) + "\n")
    except OSError:
        listing = None
    tops = {}
    for rel, _ in entries:                               # a summary: files per top folder (resources/ one deeper)
        parts = rel.split("app-64.7z!/", 1)[-1].replace("!/", "/").split("/")
        key = "/".join(parts[:2] if parts[0] == "resources" and len(parts) > 2 else parts[:1])
        tops[key] = tops.get(key, 0) + 1
    print(f"Installer contents: {len(entries)} files in {os.path.basename(target)}"
          + (f" (full list: {listing})" if listing else ""))
    for k in sorted(tops)[:40]:
        print(f"  {tops[k]:6d}  {k}")
    if len(tops) > 40:
        print(f"  ... and {len(tops) - 40} more folders")
    if problems:
        print(f"\nFAILED: {len(problems)} file(s) must not ship:")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"\nOK: no secrets, personal files, logs or data in the installer "
          f"({len(strings)} personal strings + key/token patterns checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

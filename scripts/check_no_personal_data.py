"""Fails if a file git would commit holds personal data or a secret.

    python scripts/check_no_personal_data.py            # every tracked file plus anything staged
    python scripts/check_no_personal_data.py --staged   # only what the next commit adds (the pre-commit hook)

What it looks for:
  - every string listed in personal_strings.local.txt (git-ignored; one per line, # comments,
    matched case-insensitively): your name, email, phone, school, employers, username...
  - generic patterns: email addresses, phone numbers, API key / token formats, private keys, Google
    OAuth client ids, and absolute home-folder paths (C:\\Users\\<name>, /home/<name>, /Users/<name>).

Obviously fake data is allowed: addresses at example.com / *.example, 555-01xx phone numbers, and
the UK drama range +44 7700 900xxx. The staged copy of each file is scanned (what would really be
committed), not the working copy. Matches are printed masked, so the report itself leaks nothing.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STRINGS_FILE = os.path.join(ROOT, "personal_strings.local.txt")

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
FAKE_EMAIL_RE = re.compile(r"@(?:[\w-]+\.)*(?:example(?:\.com|\.org|\.net)?|test|invalid|localhost)$", re.I)
PHONE_RE = re.compile(r"(?<![\w.])(?:\+\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w.])"
                      r"|(?<![\w.])\+\d{1,3}[ .-]\d{3,4}[ .-]\d{3}[ .-]?\d{3,4}(?![\w.])")
FAKE_PHONE_RE = re.compile(r"555[ .-]?01\d\d|7700[ .-]?900")
KEY_PATTERNS = [
    ("Anthropic/OpenAI key", r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}"),
    ("GitHub token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{30,}"),
    ("Google OAuth secret", r"\bGOCSPX-[A-Za-z0-9_-]{10,}"),
    ("Google access token", r"\bya29\.[A-Za-z0-9_-]{20,}"),
    ("Google refresh token", r"(?<![\w/])1//[A-Za-z0-9_-]{20,}"),
    ("Google OAuth client id", r"\b\d{6,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com"),
    ("AWS access key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("Slack token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("private key", r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
]
KEY_RES = [(name, re.compile(rx)) for name, rx in KEY_PATTERNS]
# A home folder with a real-looking name. Placeholders (you, USERNAME, <name>, Public...) are fine.
HOME_RE = re.compile(r"(?i)(?:[a-z]:[\\/]+users[\\/]+|/home/|/Users/)(?!(?:public|default|all users|you|your-?name|"
                     r"username|user|me|example|jane|runner|\$|%|<|\{))[A-Za-z][^\\/\s\"'`<>]{1,40}")


def git(*args, binary=False):
    out = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, check=True)
    return out.stdout if binary else out.stdout.decode("utf-8", "replace")


def files_to_scan(staged_only):
    if staged_only:
        names = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0")
    else:
        names = git("ls-files", "--cached", "-z").split("\0")
    return [n for n in names if n]


def staged_text(rel):
    """The index copy of a file (what a commit would hold), or None for binary files."""
    try:
        data = git("show", f":{rel}", binary=True)
    except subprocess.CalledProcessError:              # deleted from the index: nothing to commit
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", "replace")


def personal_strings(path=STRINGS_FILE):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return [s for s in (line.strip() for line in f) if s and not s.startswith("#")]


def mask(s):
    s = s.strip()
    return s[:2] + "*" * max(len(s) - 4, 3) + s[-2:] if len(s) > 5 else "*" * len(s)


def scan_text(rel, text, strings):
    """[(file, line, kind, masked match)] for one file's text."""
    hits = []
    lowered = [(s, s.lower()) for s in strings]
    for n, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        for s, ls in lowered:
            if ls in low:
                hits.append((rel, n, "personal string", mask(s)))
        for m in EMAIL_RE.finditer(line):
            if not FAKE_EMAIL_RE.search(m.group()):
                hits.append((rel, n, "email address", mask(m.group())))
        for m in PHONE_RE.finditer(line):
            if not FAKE_PHONE_RE.search(m.group()):
                hits.append((rel, n, "phone number", mask(m.group())))
        for name, rx in KEY_RES:
            for m in rx.finditer(line):
                hits.append((rel, n, name, mask(m.group())))
        for m in HOME_RE.finditer(line):
            hits.append((rel, n, "home-folder path", mask(m.group())))
    return hits


def scan(staged_only=False):
    """(hits, files scanned, number of personal strings or None when the list file is missing)."""
    strings = personal_strings()
    hits, files = [], files_to_scan(staged_only)
    for rel in files:
        if rel == "personal_strings.local.txt":
            hits.append((rel, 0, "the personal strings list itself is staged", ""))
            continue
        text = staged_text(rel)
        if text is not None:
            hits.extend(scan_text(rel, text, strings or []))
    return hits, len(files), None if strings is None else len(strings)


def main(argv):
    staged_only = "--staged" in argv
    try:
        hits, n_files, n_strings = scan(staged_only)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"check_no_personal_data: can't list files with git ({e}). Run it inside the repo.")
        return 2
    if n_strings is None:
        print("check_no_personal_data: personal_strings.local.txt not found, so only generic patterns were checked. "
              "Create it (git-ignored) with one personal string per line: your name, email, phone, school...")
    for rel, n, kind, shown in hits:
        print(f"{rel}:{n}: {kind}" + (f" ({shown})" if shown else ""))
    if hits:
        print(f"\nFAILED: {len(hits)} possible personal data or secret hit(s) in {n_files} file(s). "
              "Move the value into an ignored local file (config.local.yaml, config.json, data/...) and use a "
              "TODO or obviously fake value (Jane Doe, jane@example.com, 555-0100) in committed files.")
        return 1
    print(f"OK: {n_files} {'staged' if staged_only else 'tracked'} file(s) clean "
          f"({n_strings or 0} personal strings + generic patterns).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

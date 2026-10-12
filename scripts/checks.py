"""Run the checks Apply runs, the way Apply runs them, on THIS folder, and print only what fails.

    python scripts/checks.py            # compile, both test suites, page check, preflight, personal-data scan

Same code as Jarvis's own checks (selfbuild.Builder.checks): the changed Python compiles, both unit-test
suites pass, a test server on a scratch copy of $JARVIS_TEST_HOME serves the page with no script errors,
preflight passes, and no personal data is in the code. One difference: Apply forgives preflight failures
that were already there before the change; this reports every failure. Exits 1 if anything fails.
Takes a few minutes: run it once, when the work is done, not while iterating.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import selfbuild  # noqa: E402


def changed_files():
    """Everything that differs from HEAD (staged, unstaged, untracked), like the workbench's pending list."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                              errors="replace").stdout.splitlines()
    return sorted(set(git("diff", "--name-only", "HEAD")) | set(git("ls-files", "--others", "--exclude-standard")))


def main():
    home = os.environ.get("JARVIS_TEST_HOME")
    if not home or not os.path.isdir(home):
        sys.exit("needs $JARVIS_TEST_HOME: a scratch copy of the data folder (never the real one)")
    # Builder's check machinery without its __init__ (which manages build state and clears scratch homes).
    b = selfbuild.Builder.__new__(selfbuild.Builder)
    b.python, b.home, b.custom_checks = sys.executable, os.path.realpath(home), None
    b.homes = tempfile.mkdtemp(prefix="checks-")
    b._baseline_failures = lambda wt, test_home: set()
    started = time.time()
    try:
        results = b.checks(ROOT, changed_files())
    finally:
        shutil.rmtree(b.homes, ignore_errors=True)
    for name, ok, detail in results:
        last = (detail.strip().splitlines() or [""])[-1]
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f": {last}" if ok and "pass" in last else ""))
        if not ok:
            print("    " + "\n    ".join(detail.strip().splitlines()[-40:]))
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\n{len(results) - failed} passed, {failed} failed ({time.time() - started:.0f}s)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

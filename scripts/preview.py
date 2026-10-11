"""See Jarvis as he is in this folder: start a throwaway server from this code and screenshot the desk.

    python scripts/preview.py --out shot.png                          # the desk as it opens
    python scripts/preview.py --out s.png --click "#gear"             # after opening Settings
    python scripts/preview.py --out s.png --type "#q" "hello" --wait 500
    python scripts/preview.py --out s.png --eval "document.body.classList.add('x')" --full
    python scripts/preview.py --serve                                 # keep it running; prints the URL

The steps (--click, --type, --eval, --wait) run in the order given. It needs a data folder to run against:
--home DIR, or $JARVIS_TEST_HOME (a self-build sets it to a scratch copy of the owner's data; never point it
at the real one). Speech recognition and the microphone are faked. Prints script errors and console errors;
exits 1 on a script error, 2 if Chrome or Playwright isn't available.
"""
import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from page_check import FAKE  # noqa: E402  (the same fake microphone and recogniser)


class Step(argparse.Action):
    def __call__(self, parser, ns, values, option_string=None):
        ns.steps = (getattr(ns, "steps", None) or []) + [(self.dest, values)]


def start_server(home):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "JARVIS_HOME": home, "JARVIS_PORT": str(port), "JARVIS_SELF_BUILD": "0",
           "PYTHONIOENCODING": "utf-8"}
    env.pop("CLAUDECODE", None)
    log = open(os.path.join(home, "preview-server.log"), "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-B", "server.py"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(120):
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
                return proc, log, port
        except OSError:
            time.sleep(0.5)
    proc.kill()
    log.close()
    with open(os.path.join(home, "preview-server.log"), encoding="utf-8", errors="replace") as f:
        sys.exit("the server didn't start:\n" + f.read()[-3000:])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--home", default=os.environ.get("JARVIS_TEST_HOME"))
    ap.add_argument("--out", help="screenshot file (.png)")
    ap.add_argument("--serve", action="store_true", help="keep the server running and print its URL")
    ap.add_argument("--size", default="1440x900")
    ap.add_argument("--full", action="store_true", help="the whole page, not just the window")
    ap.add_argument("--click", action=Step, metavar="SELECTOR")
    ap.add_argument("--type", action=Step, nargs=2, metavar=("SELECTOR", "TEXT"))
    ap.add_argument("--eval", action=Step, metavar="JS")
    ap.add_argument("--wait", action=Step, type=int, metavar="MS")
    a = ap.parse_args()
    if not a.home or not os.path.isdir(a.home):
        sys.exit("needs a scratch data folder: --home DIR or $JARVIS_TEST_HOME")
    if os.path.realpath(a.home) == ROOT:
        sys.exit("refusing to run against the code folder's own data: use a scratch copy")
    proc, log, port = start_server(os.path.realpath(a.home))
    url = f"http://127.0.0.1:{port}/"
    try:
        if a.serve:
            print(url, flush=True)
            proc.wait()
            return 0
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("playwright is not installed")
            return 2
        w, h = (int(x) for x in a.size.lower().split("x"))
        errors, console = [], []
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception as e:
                print(f"could not start Chrome: {e}")
                return 2
            page = browser.new_page(viewport={"width": w, "height": h})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
            page.add_init_script(FAKE)
            page.goto(url + "?sleep=600&mute=1")
            page.wait_for_timeout(2500)
            for kind, value in getattr(a, "steps", None) or []:
                if kind == "click":
                    page.click(value, timeout=5000)
                    page.wait_for_timeout(600)
                elif kind == "type":
                    page.fill(value[0], value[1], timeout=5000)
                elif kind == "eval":
                    print("eval ->", page.evaluate(value))
                elif kind == "wait":
                    page.wait_for_timeout(value)
            if a.out:
                page.screenshot(path=a.out, full_page=a.full)
                print("screenshot:", os.path.abspath(a.out))
            browser.close()
        for e in console:
            print("console error:", e)
        for e in errors:
            print("script error:", e)
        print("no script errors" if not errors else f"{len(errors)} script error(s)")
        return 1 if errors else 0
    finally:
        proc.kill()
        proc.wait()
        log.close()


if __name__ == "__main__":
    sys.exit(main())

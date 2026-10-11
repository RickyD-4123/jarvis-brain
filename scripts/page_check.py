"""Load the desk page in headless Chrome and fail on any script error.

    python scripts/page_check.py http://127.0.0.1:4712/

viewer/index.html is one big script: a single mistake (a duplicate const, a typo) silently kills the
whole page, so self-builds run this before they are offered. The microphone and speech recogniser
are faked, so nothing listens and nothing asks for permission. Exits 1 on errors, 2 if Chrome or
Playwright isn't available.
"""
import sys

FAKE = """
window.__recs = [];
class FakeRec { constructor() { window.__recs.push(this); } start() { setTimeout(() => this.onstart && this.onstart(), 0); } abort() {} stop() {} }
window.webkitSpeechRecognition = window.SpeechRecognition = FakeRec;
if (navigator.mediaDevices) navigator.mediaDevices.getUserMedia = async () => {
  const c = new AudioContext(), o = c.createOscillator(), d = c.createMediaStreamDestination(); o.connect(d); o.start(); return d.stream; };
"""


def main(url):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed")
        return 2
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as e:
            print(f"could not start Chrome: {e}")
            return 2
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script(FAKE)
        page.goto(url + ("&" if "?" in url else "?") + "sleep=600&mute=1")
        page.wait_for_timeout(3000)
        ok = page.evaluate("!!document.getElementById('q') && !!document.getElementById('send')")
        if not ok:
            errors.append("the message box (#q) or the Ask button (#send) is missing")
        if not errors:                                     # the voice path still turns speech into a draft
            page.evaluate("""() => { const r = window.__recs[0]; if (!r || !r.onresult) return;
                const res = [{ transcript: 'hey jarvis what time is it' }]; res.isFinal = false;
                r.onresult({ resultIndex: 0, results: [res] }); }""")
            if not page.input_value("#q").strip():
                errors.append("speech no longer reaches the message box")
        browser.close()
    for e in errors:
        print("page error:", e)
    print("page loads cleanly" if not errors else f"{len(errors)} page error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:4700/"))

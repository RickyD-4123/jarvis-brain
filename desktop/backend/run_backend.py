"""The Jarvis backend as the desktop app runs it: server.main() with the port, data folder and
claude executable the Electron main process hands over in the environment.

    JARVIS_PORT     free port on 127.0.0.1 picked by the app
    JARVIS_HOME     per-user data folder (config.json, secrets/, data/, notes/, internship_apply/...)
    JARVIS_CLAUDE   the claude executable the app found (PATH from a Start-menu launch can be shorter)
    JARVIS_VERSION  the app version, reported by GET /health

Dev mode runs this file from the repo with your own Python; the installer ships it frozen by
PyInstaller (jarvis-backend.spec). No idle shutdown: the app decides when the backend stops.
"""
import os
import sys

if not getattr(sys, "frozen", False):                    # dev: import the repo's modules directly
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

if hasattr(sys.stdout, "reconfigure"):                    # the app writes this output to data/jarvis.log
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import server  # noqa: E402


def self_test():
    """jarvis-backend.exe --self-test: prove the frozen backend can reach everything it needs, then exit.
    Prints one JSON line: claude found, internship config + safety switches, Playwright driving Chrome."""
    import json
    out = {"version": server.VERSION, "home": server.HOME}
    try:
        out["claude"] = server.find_claude()
    except SystemExit as e:
        out["claude"] = f"NOT FOUND: {e}"
    from internship_apply import load_config
    cfg = load_config()
    out["internship"] = {k: cfg[k] for k in ("dry_run", "filling_enabled", "allow_approved_submit", "manual_only_domains")}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()
            page.set_content("<title>ok</title>")
            out["playwright"] = f"Chrome {browser.version}: {page.title()}"
            browser.close()
    except Exception as e:                               # noqa: BLE001 - reported, not raised
        out["playwright"] = f"FAILED: {type(e).__name__}: {str(e)[:300]}"
    print(json.dumps(out), flush=True)
    return 0 if not str(out["playwright"]).startswith("FAILED") and "NOT FOUND" not in str(out["claude"]) else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    print(f"--- desktop backend {server.VERSION} pid {os.getpid()} port {server.PORT}", flush=True)
    server.main()

"""Start J.A.R.V.I.S and open his window. Run by the desktop/Start-menu shortcut and by the
jarvis:// link (pythonw.exe, so no console window).

  - Already running? Just open the window.
  - Otherwise start the server in the background, open the window once it answers, and let the
    server stop by itself IDLE_EXIT seconds after the last desk window is closed (or when you press
    the power button). Nothing starts with Windows; nothing is left running for Task Manager.

Output goes to data/jarvis.log (owner-only, like every state file), rotated at 5 MB.
"""
import os
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
URL = "http://127.0.0.1:4700/"
LOG = os.path.join(ROOT, "data", "jarvis.log")
IDLE_EXIT = 180                       # seconds after the last desk window closes
CHROME = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
          r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
          os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")]
os.chdir(ROOT)
sys.path.insert(0, ROOT)


def running():
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", 4700)) == 0


def open_window():
    """Jarvis in his own app-style Chrome window (no tabs or address bar); else the default browser."""
    exe = next((p for p in CHROME if os.path.isfile(p)), None)
    if exe:
        subprocess.Popen([exe, f"--app={URL}"], close_fds=True)
    else:
        os.startfile(URL)


if running():
    open_window()
    sys.exit(0)

os.makedirs(os.path.dirname(LOG), exist_ok=True)
if os.path.exists(LOG) and os.path.getsize(LOG) > 5 * 1024 * 1024:
    os.replace(LOG, LOG + ".1")
log = open(LOG, "a", encoding="utf-8", buffering=1)      # line-buffered, so the log is always current
sys.stdout = sys.stderr = log                             # pythonw has no console to print to

import secure  # noqa: E402

secure.lock(LOG)
import server  # noqa: E402


def open_when_up():
    for _ in range(120):
        if running():
            open_window()
            return
        time.sleep(0.5)


print(f"--- started by start_jarvis.pyw (pid {os.getpid()}) {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
threading.Thread(target=open_when_up, daemon=True).start()
server.main(idle_exit=IDLE_EXIT)

# PyInstaller spec: freezes the Jarvis backend (server.py + internship_apply + Python itself) into
# desktop/build/backend/jarvis-backend/, which electron-builder ships as resources/backend.
# Run through `npm run build:backend` (desktop/scripts/build-backend.js), not by hand.
#
# Only code and the read-only web page go in. Your files (config.json, secrets/, data/, profile.yaml,
# config.local.yaml, listings.db, output/, logs/) live in the per-user data folder and are never bundled;
# desktop/scripts/check_build.py fails the build if any of them, or anything key-shaped, slips in.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

REPO = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
VIEWER = ["index.html", "graph-data.js", "jarvis.ico", "orbitron.woff2"]

datas = [(os.path.join(REPO, "viewer", name), "viewer") for name in VIEWER]
datas += [(os.path.join(REPO, "internship_apply", "config.yaml"), "internship_apply")]   # shipped defaults
# Playwright's driver (node + its JS package). Browsers are NOT bundled: the form filler drives your
# installed Google Chrome (channel="chrome").
datas += collect_data_files("playwright", excludes=["**/__pycache__/**"])

hidden = [m for m in collect_submodules("internship_apply") if ".tests" not in m]
hidden += collect_submodules("playwright") + ["yaml", "docx", "fpdf"]

a = Analysis(
    [os.path.join(SPECPATH, "run_backend.py")],
    pathex=[REPO],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "internship_apply.tests", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="jarvis-backend", console=True,
          icon=os.path.join(REPO, "viewer", "jarvis.ico"), upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="jarvis-backend", upx=False)

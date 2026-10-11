"""appupdate.py against a throwaway git repo, with stand-ins for npm, the installer helper and the running app.

    python -m unittest discover -s tests -t .
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import appupdate

PKG = '{\n  "name": "jarvis-desktop",\n  "version": "0.1.0",\n  "devDependencies": {"electron": "44.4.5"}\n}\n'
LOCK = ('{\n  "name": "jarvis-desktop",\n  "version": "0.1.0",\n  "packages": {\n    "": {\n      "name": "jarvis-desktop",\n'
        '      "version": "0.1.0"\n    },\n    "node_modules/x": {\n      "version": "0.1.0"\n    }\n  }\n}\n')


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


class FakeCards:
    def __init__(self):
        self.cards = []

    def add(self, kind, title, text, priority="normal", ref=None):
        self.cards.append({"kind": kind, "title": title, "text": text, "ref": ref})


class AppUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-appupdate-test-")
        self.src = os.path.join(self.tmp, "src")
        self.desk = os.path.join(self.src, "desktop")
        os.makedirs(os.path.join(self.desk, "node_modules"))
        for name, text in (("package.json", PKG), ("package-lock.json", LOCK)):
            with open(os.path.join(self.desk, name), "w", newline="\n") as f:
                f.write(text)
        git(self.src, "init", "-q")
        git(self.src, "config", "user.name", "Test")
        git(self.src, "config", "user.email", "test@example.com")
        git(self.src, "config", "core.hooksPath", ".git/hooks")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "start")
        self.home = os.path.join(self.tmp, "apphome")
        os.makedirs(self.home)
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump({"model": "claude-code-default", "source_dir": "TODO"}, f)
        self.cards, self.commands, self.launched = FakeCards(), [], []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def updater(self, build_ok=True, running=False, frozen=False):
        def run(cmd, cwd, timeout, log_path):
            self.commands.append(cmd)
            if build_ok and cmd[-1] == "dist":
                os.makedirs(os.path.join(cwd, "dist"), exist_ok=True)
                with open(os.path.join(cwd, "dist", "Jarvis-Setup-0.1.1.exe"), "w") as f:
                    f.write("installer")
            return 0 if build_ok else 1
        return appupdate.AppUpdater(self.src, self.cards, packaged_home=self.home, run=run,
                                    launch=self.launched.append, app_running=lambda: running, frozen=frozen,
                                    logs_dir=os.path.join(self.tmp, "logs"))

    def read(self, name):
        with open(os.path.join(self.desk, name)) as f:
            return f.read()

    def build(self, u):
        self.assertIn("Building the desktop app", u.start())
        u.thread.join(30)
        return u.status()

    def test_build_bumps_commits_and_offers_install(self):
        u = self.updater()
        s = self.build(u)
        self.assertEqual(s["status"], "ready", s.get("error"))
        self.assertEqual(s["version"], "0.1.1")
        self.assertIn('"version": "0.1.1"', self.read("package.json"))
        lock = self.read("package-lock.json")
        self.assertEqual(lock.count('"version": "0.1.1"'), 2, "the lockfile's two own copies")
        self.assertIn('"node_modules/x": {\n      "version": "0.1.0"', lock, "dependencies keep their versions")
        self.assertEqual(self.commands, [["cmd", "/c", "npm", "run", "dist"]])
        self.assertTrue(s["committed"])
        self.assertEqual(git(self.src, "log", "-1", "--format=%s").strip(), "Desktop app v0.1.1")
        with open(os.path.join(self.home, "config.json")) as f:
            self.assertEqual(json.load(f)["source_dir"], os.path.realpath(self.src), "the installed app learns where its code is")
        self.assertEqual(self.cards.cards[-1]["title"], "App update ready")

    def test_failed_build_puts_the_version_back(self):
        u = self.updater(build_ok=False)
        s = self.build(u)
        self.assertEqual(s["status"], "failed")
        self.assertEqual(self.read("package.json"), PKG)
        self.assertEqual(self.read("package-lock.json"), LOCK)
        self.assertEqual(git(self.src, "status", "--porcelain"), "")
        self.assertEqual(self.cards.cards[-1]["title"], "App update failed")

    def test_install_from_the_web_version(self):
        u = self.updater(running=True)
        self.assertFalse(u.install()["ok"], "nothing built yet")
        self.build(u)
        r = u.install()
        self.assertFalse(r["ok"])
        self.assertIn("Close it", r["say"])
        u.app_running = lambda: False
        r = u.install()
        self.assertTrue(r["ok"])
        self.assertFalse(r["shutdown"])
        with open(self.launched[-1], encoding="utf-8") as f:
            script = f.read()
        self.assertIn("Jarvis-Setup-0.1.1.exe", script)
        self.assertIn("'/S'", script)
        self.assertIn("Start-Process -FilePath $exe", script)

    def test_install_from_the_desktop_app_closes_jarvis(self):
        u = self.updater(frozen=True, running=True)
        self.build(u)
        r = u.install()
        self.assertTrue(r["ok"])
        self.assertTrue(r["shutdown"], "the app must close for the installer to replace it")

    def test_bump(self):
        self.assertEqual(appupdate.bump("0.1.9"), "0.1.10")


if __name__ == "__main__":
    unittest.main()

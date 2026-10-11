"""agent.py (work mode) against a throwaway git repo, with a stand-in for Claude Code and for the checks.

    python -m unittest discover -s tests -t .
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import agent


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


class WorkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-work-test-")
        self.src = os.path.join(self.tmp, "src")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.src, "viewer"))
        os.makedirs(self.home)
        git(self.src, "init", "-q")
        git(self.src, "config", "user.name", "Test")
        git(self.src, "config", "user.email", "test@example.com")
        git(self.src, "config", "core.hooksPath", ".git/hooks")
        self.write("viewer/index.html", "<p>hello</p>\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "start")
        self.calls, self.check_results, self.turns_done = [], [], []
        self.edit = None                                  # what the fake agent writes, per call

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.src, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text, root=None):
        with open(os.path.join(root or self.src, rel), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.src, rel), encoding="utf-8") as f:
            return f.read()

    def agent(self, prompt, cwd, rules, timeout, **kw):
        self.calls.append({"prompt": prompt, "cwd": cwd, "rules": rules, **kw})
        kw["on_event"]({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "python -m unittest", "description": "Run the tests"}},
            {"type": "text", "text": "Looking at the page now."}]}})
        if self.edit:
            self.write("viewer/index.html", self.edit, root=cwd)
        return f"Done, sir.\n\nDetails: I edited the page ({len(self.calls)}).", None, []

    def checks(self, wt, changed):
        return self.check_results.pop(0) if self.check_results else [("Fake check", True, "fine")]

    def bench(self, context=lambda: "SUMMARY: they like blue"):
        return agent.Workbench(self.src, self.home, None, self.agent, context=context, workspace=lambda: [self.tmp],
                               python=sys.executable, builds_dir=os.path.join(self.tmp, "builds"), run_checks=self.checks,
                               on_turn=lambda q, a: self.turns_done.append((q, a)))

    def wait(self, w):
        for _ in range(400):
            v = w.view()
            if not v["busy"] and not v["applying"]:
                return v
            time.sleep(0.05)
        self.fail("work mode never finished")

    def test_a_conversation_resumes_one_session_with_context_once(self):
        w = self.bench()
        self.assertTrue(w.send("make the page say hi")["ok"])
        self.wait(w)
        self.assertTrue(w.send("now make it blue")["ok"])
        v = self.wait(w)
        first, second = self.calls
        self.assertFalse(first["resume"])
        self.assertTrue(second["resume"])
        self.assertEqual(first["session"], second["session"])
        self.assertIn("SUMMARY: they like blue", first["prompt"])
        self.assertNotIn("SUMMARY", second["prompt"], "context only starts a session; the session remembers")
        self.assertIn("Your memory", first["rules"], "the same memory system as Claude Code")
        self.assertIn("FIRST PARAGRAPH is read", first["rules"])
        self.assertIn(self.tmp, first["add_dirs"])
        self.assertTrue(os.path.isdir(first["env"]["JARVIS_TEST_HOME"]))
        self.assertTrue(first["cwd"].endswith("workbench"), "he works in his workbench, not the live code")
        self.assertEqual(v["last"]["say"], "Done, sir.")
        self.assertEqual(len(self.turns_done), 2, "turns join the shared conversation")
        kinds = [e["kind"] for e in w.view()["events"]]
        self.assertIn("tool", kinds)
        self.assertIn("text", kinds)
        self.assertTrue(any("Run the tests" in e["text"] for e in w.view()["events"]))

    def test_messages_while_busy_are_queued_and_delivered_next(self):
        gate = threading.Event()
        real = self.agent

        def slow(*a, **k):
            if len(self.calls) == 0:
                gate.wait(5)
            return real(*a, **k)
        w = self.bench()
        w.run_agent = slow
        w.send("first thing")
        r = w.send("and also this")
        self.assertTrue(r["queued"])
        gate.set()
        self.wait(w)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("and also this", self.calls[1]["prompt"])

    def test_changes_wait_in_the_workbench_until_applied(self):
        self.edit = "<p>hello</p>\n<p>blue</p>\n"
        w = self.bench()
        w.send("make it blue")
        v = self.wait(w)
        self.assertEqual(v["pending"], [["M", "viewer/index.html"]])
        self.assertEqual(self.read("viewer/index.html"), "<p>hello</p>\n", "the live code is untouched")
        self.assertIn("Running every check", w.apply())
        v = self.wait(w)
        self.assertIn("Applied and committed", v["last"]["text"])
        self.assertIn("<p>blue</p>", self.read("viewer/index.html"))
        self.assertIn("Work mode: make it blue", git(self.src, "log", "-1", "--format=%B"))
        self.assertEqual(v["pending"], [], "the workbench matches the live code again")
        self.assertEqual(w.history()[-1]["status"], "applied")
        r = w.undo()
        self.assertTrue(r["ok"], r["say"])
        self.assertEqual(self.read("viewer/index.html"), "<p>hello</p>\n")

    def test_failed_checks_go_back_to_him_before_anything_is_applied(self):
        self.edit = "<p>broken</p>\n"
        self.check_results = [[("Page loads", False, "TypeError: boom")], [("Page loads", True, "")]]
        w = self.bench()
        w.send("change it")
        self.wait(w)
        self.edit = "<p>fixed</p>\n"
        w.apply()
        v = self.wait(w)
        self.assertIn("CHECK FAILED: Page loads", self.calls[-1]["prompt"])
        self.assertTrue(self.calls[-1]["resume"], "the fix happens in the same conversation")
        self.assertIn("Applied", v["last"]["text"])
        self.assertEqual(self.read("viewer/index.html"), "<p>fixed</p>\n")

    def test_discard_puts_the_workbench_back(self):
        self.edit = "<p>nope</p>\n"
        w = self.bench()
        w.send("try something")
        self.assertEqual(len(self.wait(w)["pending"]), 1)
        self.assertIn("Discarded", w.discard())
        self.assertEqual(w.view()["pending"], [])
        with open(os.path.join(w.state["worktree"], "viewer", "index.html"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "<p>hello</p>\n")

    def test_workbench_follows_the_live_code_when_nothing_is_pending(self):
        w = self.bench()
        w.send("hello")
        self.wait(w)
        self.write("viewer/index.html", "<p>changed by hand</p>\n")
        w.send("look again")
        self.wait(w)
        with open(os.path.join(w.state["worktree"], "viewer", "index.html"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "<p>changed by hand</p>\n")

    def test_stop_and_new_session(self):
        w = self.bench()
        self.assertIn("wasn't doing anything", w.stop())
        w.send("hello")
        self.wait(w)
        self.assertIn("Fresh start", w.new_session())
        w.send("hello again")
        self.wait(w)
        self.assertNotEqual(self.calls[0]["session"], self.calls[1]["session"])
        self.assertFalse(self.calls[1]["resume"])

    def test_spoken_part_is_the_first_paragraph_without_markdown(self):
        self.assertEqual(agent.first_paragraph("**Done**, sir. It's `fixed`.\n\n```py\nx = 1\n```"), "Done, sir. It's fixed.")


if __name__ == "__main__":
    unittest.main()

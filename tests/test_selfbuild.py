"""selfbuild.py against a real throwaway git repository, with a stand-in for Claude Code and for the checks.

    python -m unittest discover -s tests -t .
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import selfbuild


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


class FakeCards:
    def __init__(self):
        self.cards = []

    def add(self, kind, title, text, priority="normal", ref=None):
        self.cards.append({"kind": kind, "title": title, "text": text, "ref": ref})


class SelfBuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jarvis-selfbuild-test-")
        self.src = os.path.join(self.tmp, "src")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.src, "viewer"))
        os.makedirs(self.home)
        git(self.src, "init", "-q")
        git(self.src, "config", "user.name", "Test")
        git(self.src, "config", "user.email", "test@example.com")
        git(self.src, "config", "core.hooksPath", ".git/hooks")
        self.write("app.py", "x = 1\n")
        self.write("viewer/index.html", "<p>hello</p>\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "start")
        self.write("app.py", "x = 2\n")                  # uncommitted: the build must start from it
        self.write("new.py", "y = 1\n")                  # untracked: carried over too
        self.cards, self.calls, self.check_results, self.kwargs = FakeCards(), [], [], []

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=self.src, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text, root=None):
        path = os.path.join(root or self.src, rel)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.src, rel), encoding="utf-8") as f:
            return f.read()

    def agent(self, prompt, cwd, rules, timeout, **kw):
        self.calls.append(prompt)
        self.kwargs.append(kw)
        with open(os.path.join(cwd, "app.py"), encoding="utf-8") as f:
            self.seen_app = f.read()
        self.seen_new = os.path.isfile(os.path.join(cwd, "new.py"))
        self.write("viewer/index.html", "<p>hello</p>\n<p>a new button</p>\n", root=cwd)
        return ("Edited the page.\nSUMMARY: Added a button.\nSHOW: --click \"#gear\" --bogus x --wait 300", None,
                [["Bash", "python -m unittest"], ["WebSearch", "css grid"]])

    def checks(self, wt, changed):
        self.changed_seen = changed
        return self.check_results.pop(0) if self.check_results else [("Fake check", True, "fine")]

    def builder(self, **kw):
        return selfbuild.Builder(self.src, self.home, self.cards, kw.get("agent", self.agent), python=sys.executable,
                                 builds_dir=os.path.join(self.tmp, "builds"), run_checks=self.checks,
                                 context=kw.get("context"), take_shots=False)

    def run_build(self, b, request="upgrade yourself: add a button"):
        say = b.start(request)
        self.assertIn("separate copy", say)
        b.thread.join(60)
        return b.status()

    def test_build_apply_commits_only_the_change(self):
        b = self.builder()
        s = self.run_build(b)
        self.assertEqual(s["status"], "ready", s.get("error"))
        self.assertEqual(self.seen_app, "x = 2\n", "uncommitted changes are carried into the build")
        self.assertTrue(self.seen_new, "untracked files are carried too")
        self.assertEqual(s["files"], [["M", "viewer/index.html"]])
        self.assertEqual(self.changed_seen, ["viewer/index.html"])
        self.assertEqual(s["summary"], "Added a button.")
        self.assertFalse(s["source_changed"], "nothing touched the real folder")
        self.assertIn("+<p>a new button</p>", s["diff"])
        self.assertEqual(self.read("viewer/index.html"), "<p>hello</p>\n", "nothing applied before approval")
        self.assertEqual(self.cards.cards[-1]["title"], "Self-build ready")
        self.assertIn("waiting", b.start("upgrade yourself again"), "one build at a time")

        r = b.apply()
        self.assertTrue(r["ok"], r["say"])
        self.assertTrue(r["reload"])
        self.assertFalse(r["restart"])
        self.assertIn("a new button", self.read("viewer/index.html"))
        log = git(self.src, "log", "-1", "--format=%B")
        self.assertIn("Jarvis self-build: upgrade yourself: add a button", log)
        self.assertEqual(git(self.src, "show", "--name-only", "--format=", "HEAD").split(), ["viewer/index.html"])
        status = git(self.src, "status", "--porcelain")
        self.assertIn(" M app.py", status, "your own uncommitted work stays uncommitted")
        self.assertIn("?? new.py", status)
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "builds", s["id"])), "worktree removed")
        self.assertEqual(b.status()["status"], "applied")

    def test_earlier_uncommitted_work_in_a_touched_file_is_named(self):
        self.write("viewer/index.html", "<p>hello</p>\n<!-- my own unsaved edit -->\n")
        agent = lambda prompt, cwd, rules, timeout, **kw: (
            self.write("viewer/index.html", "<p>hello</p>\n<!-- my own unsaved edit -->\n<p>button</p>\n", root=cwd),
            ("SUMMARY: Added a button.", None))[1]
        b = self.builder(agent=agent)
        self.assertEqual(self.run_build(b)["status"], "ready")
        r = b.apply()
        self.assertTrue(r["ok"], r["say"])
        self.assertIn("unsaved changes that were already in viewer/index.html", r["say"])
        self.assertIn("Also includes earlier uncommitted work in: viewer/index.html", git(self.src, "log", "-1", "--format=%B"))

    def test_refuses_to_apply_over_files_changed_since(self):
        b = self.builder()
        self.assertEqual(self.run_build(b)["status"], "ready")
        self.write("viewer/index.html", "<p>edited meanwhile</p>\n")
        r = b.apply()
        self.assertFalse(r["ok"])
        self.assertIn("changed since", r["say"])
        self.assertEqual(self.read("viewer/index.html"), "<p>edited meanwhile</p>\n")
        self.assertIn("Discarded", b.discard())
        self.assertEqual(self.read("viewer/index.html"), "<p>edited meanwhile</p>\n")

    def test_failed_checks_go_back_for_repair(self):
        self.check_results = [[("Page loads", False, "TypeError: boom")], [("Page loads", True, "")]]
        b = self.builder()
        s = self.run_build(b)
        self.assertEqual(s["status"], "ready")
        self.assertEqual(len(self.calls), 2)
        self.assertIn("CHECK FAILED: Page loads", self.calls[1])
        self.assertIn("TypeError: boom", self.calls[1])

    def test_still_failing_after_repairs_cannot_be_applied(self):
        self.check_results = [[("Unit tests", False, "1 failed")]] * (selfbuild.MAX_REPAIRS + 1)
        b = self.builder()
        s = self.run_build(b)
        self.assertEqual(s["status"], "failed")
        self.assertEqual(len(self.calls), selfbuild.MAX_REPAIRS + 1)
        self.assertEqual(self.cards.cards[-1]["title"], "Self-build failed")
        self.assertFalse(b.apply()["ok"])
        self.assertEqual(self.read("viewer/index.html"), "<p>hello</p>\n")

    def test_refused_commit_puts_the_originals_back(self):
        hook = os.path.join(self.src, ".git", "hooks", "pre-commit")
        with open(hook, "w", newline="\n") as f:
            f.write("#!/bin/sh\necho personal data found >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        b = self.builder()
        self.assertEqual(self.run_build(b)["status"], "ready")
        r = b.apply()
        self.assertFalse(r["ok"])
        self.assertIn("personal data found", r["say"])
        self.assertEqual(self.read("viewer/index.html"), "<p>hello</p>\n")
        self.assertEqual(git(self.src, "diff", "--cached", "--name-only"), "")

    def test_agent_that_changes_nothing_fails(self):
        b = self.builder(agent=lambda *a, **k: ("I looked around.", None))
        s = self.run_build(b)
        self.assertEqual(s["status"], "failed")
        self.assertIn("without changing anything", s["error"])

    def test_revision_rebuilds_on_top(self):
        b = self.builder()
        self.run_build(b)
        self.assertIn("Adjusting", b.revise("change the build: make it blue"))
        b.thread.join(60)
        self.assertEqual(b.status()["status"], "ready")
        self.assertIn("make it blue", self.calls[-1])
        self.assertTrue(self.kwargs[-1]["resume"], "a revision continues the build's own session")
        self.assertEqual(self.kwargs[-1]["session"], self.kwargs[0]["session"])

    def test_first_run_gets_context_rules_and_guardrails(self):
        b = self.builder(context=lambda: "RECENT CONVERSATION: we talked about buttons")
        s = self.run_build(b)
        self.assertIn("RECENT CONVERSATION", self.calls[0])
        self.assertIn("THE OWNER ASKS", self.calls[0])
        kw = self.kwargs[0]
        self.assertFalse(kw["resume"])
        self.assertTrue(kw["session"])
        self.assertIn("Bash(git push:*)", kw["deny"])
        self.assertTrue(any(r.startswith("Read(//") and r.endswith("/secrets/**)") for r in kw["deny"]))
        self.assertTrue(os.path.isdir(kw["env"]["JARVIS_TEST_HOME"]), "a scratch data copy for the builder's servers")
        self.assertEqual(s["activity"], [["Bash", "python -m unittest"], ["WebSearch", "css grid"]])
        self.assertNotIn("session", s, "the session id stays server-side")

    def test_questions_first_then_the_answer_builds(self):
        replies = iter(["QUESTIONS:\n1. Blue or green?\n2. Left or right?", None])

        def agent(prompt, cwd, rules, timeout, **kw):
            reply = next(replies)
            if reply:
                self.kwargs.append(kw)
                return reply, None
            return self.agent(prompt, cwd, rules, timeout, **kw)
        b = self.builder(agent=agent)
        s = self.run_build(b)
        self.assertEqual(s["status"], "question")
        self.assertIn("Blue or green?", s["question"])
        self.assertEqual(self.cards.cards[-1]["title"], "Self-build question")
        self.assertTrue(b.awaiting_answer())
        self.assertIn("waiting for your answer", b.start("upgrade yourself again"))
        self.assertIn("Building it now", b.answer("blue, on the left"))
        b.thread.join(60)
        self.assertEqual(b.status()["status"], "ready")
        self.assertIn("THE OWNER ANSWERS", self.calls[-1])
        self.assertIn("blue, on the left", self.calls[-1])
        self.assertTrue(self.kwargs[-1]["resume"])
        self.assertFalse(b.awaiting_answer())

    def test_cancel_stops_a_running_build(self):
        import threading
        go = threading.Event()

        def agent(*a, **k):
            go.wait(10)
            return self.agent(*a, **k)
        b = self.builder(agent=agent)
        b.start("upgrade yourself: something slow")
        self.assertIn("Stopping", b.discard())
        go.set()
        b.thread.join(60)
        self.assertEqual(b.status()["status"], "discarded")
        self.assertEqual(self.read("viewer/index.html"), "<p>hello</p>\n")
        self.assertEqual(b.history()[-1]["status"], "discarded")

    def test_undo_reverts_the_applied_build(self):
        b = self.builder()
        self.run_build(b)
        self.assertTrue(b.apply()["ok"])
        self.assertIn("a new button", self.read("viewer/index.html"))
        r = b.undo()
        self.assertTrue(r["ok"], r["say"])
        self.assertTrue(r["reload"])
        self.assertEqual(self.read("viewer/index.html"), "<p>hello</p>\n")
        self.assertTrue(git(self.src, "log", "-1", "--format=%s").startswith("Revert"))
        self.assertEqual(b.history()[-1]["status"], "undone")
        self.assertFalse(b.undo()["ok"], "nothing left to undo")

    def test_changes_to_the_real_folder_during_a_build_are_flagged(self):
        def agent(prompt, cwd, rules, timeout, **kw):
            self.write("app.py", "x = 'changed behind its back'\n")      # the REAL folder, not the copy
            return self.agent(prompt, cwd, rules, timeout, **kw)
        s = self.run_build(self.builder(agent=agent))
        self.assertTrue(s["source_changed"])

    def test_show_line_keeps_only_preview_steps(self):
        self.assertEqual(selfbuild.Builder._show('SUMMARY: x\nSHOW: --click "#gear" --rm -rf --wait 300 --type #q "hi there"'),
                         ["--click", "#gear", "--wait", "300", "--type", "#q", "hi there"])
        self.assertEqual(selfbuild.Builder._show("SHOW: none"), [])
        self.assertEqual(selfbuild.Builder._show("SHOW: --click"), [])
        self.assertEqual(selfbuild.Builder._questions("Sure.\nSUMMARY: done"), None)
        self.assertEqual(selfbuild.Builder._questions("QUESTIONS:\n1. A or B?\nSHOW: none"), "1. A or B?")

    def test_activity_is_read_from_the_stream(self):
        lines = [json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "python -m unittest"}},
                    {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://example.com/docs"}},
                    {"type": "text", "text": "hi"}]}}),
                 "not json", json.dumps({"type": "result", "result": "done"})]
        self.assertEqual(selfbuild.parse_activity("\n".join(lines)),
                         [["Bash", "python -m unittest"], ["WebFetch", "https://example.com/docs"]])

    def test_rule_paths_use_claude_codes_form(self):
        self.assertEqual(selfbuild.rule_path("C:\\Data\\x"), "//c/Data/x")

    def test_restart_mid_build_abandons_it(self):
        os.makedirs(os.path.join(self.home, "data"))
        with open(os.path.join(self.home, "data", "build.json"), "w") as f:
            json.dump({"id": "x", "status": "running"}, f)
        b = self.builder()
        self.assertEqual(b.status()["status"], "failed")
        self.assertIn("restarted", b.status()["error"])

    def test_a_copy_of_the_state_never_deletes_the_running_copy(self):
        # A test server runs from the worktree with a copy of the data: if it ever saw "running", it must not
        # clean up the folder it is running from (that deleted the worktree under test, once).
        os.makedirs(os.path.join(self.home, "data"))
        with open(os.path.join(self.home, "data", "build.json"), "w") as f:
            json.dump({"id": "x", "status": "running", "worktree": self.src}, f)
        self.builder()
        self.assertTrue(os.path.isdir(os.path.join(self.src, ".git")))
        self.assertEqual(git(self.src, "rev-parse", "--is-inside-work-tree").strip(), "true")

    def test_without_source_it_says_so(self):
        b = selfbuild.Builder(None, self.home, self.cards, self.agent)
        self.assertIn("source_dir", b.start("upgrade yourself"))


if __name__ == "__main__":
    unittest.main()

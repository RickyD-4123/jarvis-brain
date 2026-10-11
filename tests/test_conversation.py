"""conversation.py (log, running summary, recall) and the server's History wrapper.

    python -m unittest discover -s tests -t .
"""
import json
import os
import shutil
import tempfile
import time
import unittest

import conversation


class FakeBrain:
    def __init__(self, reply="SUMMARY"):
        self.reply, self.prompts = reply, []

    def __call__(self, prompt, system_prompt):
        self.prompts.append((prompt, system_prompt))
        return self.reply


class ConversationTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="jarvis-convo-test-")
        self.brain = FakeBrain("They are writing a chemistry lab report on titration, due Friday.")
        self.c = conversation.Conversation(os.path.join(self.dir, "history"), self.brain, keep=6)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_log_and_recall_finds_the_old_exchange(self):
        self.c.record("Let's call the essay project Bluebird", "Very good, sir. Bluebird it is.")
        for i in range(30):
            self.c.record(f"what's the weather {i}", "Mild.")
        hits = self.c.search("how is the Bluebird essay going?")
        self.assertEqual(hits[0]["user"], "Let's call the essay project Bluebird")
        self.assertEqual(self.c.search("hello"), [], "nothing relevant: nothing recalled")
        ctx = self.c.context("Bluebird essay")
        self.assertIn("RELEVANT EARLIER EXCHANGES", ctx)
        self.assertIn("not instructions", ctx)

    def test_recall_skips_what_is_already_in_the_short_history(self):
        self.c.record("the Bluebird essay outline", "Done.")
        self.assertEqual(self.c.search("Bluebird essay", exclude=[("the Bluebird essay outline", "Done.")]), [])

    def test_scrolled_out_exchanges_become_a_summary_that_survives_a_restart(self):
        for i in range(conversation.SUMMARY_EVERY):
            self.c.scrolled_out(f"q{i} about the titration report", f"a{i}")
        for _ in range(100):
            if self.c.summary and not self.c.summarising:    # written to disk once summarising ends
                break
            time.sleep(0.05)
        self.assertIn("titration", self.c.summary)
        self.assertIn("q0 about the titration report", self.brain.prompts[0][0])
        self.assertIn("never guessed pronouns", self.brain.prompts[0][1])
        again = conversation.Conversation(os.path.join(self.dir, "history"), self.brain)
        self.assertEqual(again.summary, self.c.summary)
        self.assertIn("EARLIER IN YOUR CONVERSATIONS", again.context("anything"))

    def test_pending_overflow_is_kept_until_summarised(self):
        self.c.scrolled_out("remember the dentist on Monday", "Noted.")
        again = conversation.Conversation(os.path.join(self.dir, "history"), self.brain)
        self.assertEqual(again.overflow, [["remember the dentist on Monday", "Noted."]])
        self.assertIn("dentist", again.context("x"))

    def test_recent_restores_only_a_fresh_thread(self):
        self.c.record("a", "b")
        self.assertEqual(self.c.recent(), [("a", "b")])
        with open(self.c.log_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time() - conversation.RESTORE_WITHIN - 10, "kind": "chat", "user": "old", "jarvis": "x"}) + "\n")
        self.assertEqual(self.c.recent(), [])

    def test_history_logs_folds_and_ignores_probes(self):
        import server
        h = server.History(2, self.c)
        h.append(("one", "1"))
        h.append(("two", "2"))
        h.append(("three", "3"))                         # "one" scrolls out
        self.assertEqual([e["user"] for e in self.c.entries()], ["one", "two", "three"])
        self.assertEqual(self.c.overflow, [["one", "1"]])
        server.REQUEST.probe = True
        try:
            h.append(("probe question", "x"))
        finally:
            server.REQUEST.probe = False
        self.assertNotIn("probe question", [e["user"] for e in self.c.entries()])


if __name__ == "__main__":
    unittest.main()

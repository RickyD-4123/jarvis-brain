"""memory.py: the store, recall, and learning, with a stand-in for the Claude brain.

    python -m unittest discover -s tests -t .
"""
import json
import os
import tempfile
import unittest

import memory


class FakeBrain:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt, system_prompt):
        self.prompts.append((prompt, system_prompt))
        return self.replies.pop(0)


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "data", "memory.json")

    def tearDown(self):
        self.dir.cleanup()

    def make(self, *replies):
        brain = FakeBrain(*replies)
        return memory.Memory(self.path, brain, today=lambda: "2026-10-10"), brain

    def test_add_update_delete_survive_a_restart(self):
        m, _ = self.make()
        a = m.add("Studies data science", "about")
        self.assertEqual(a["source"], "you")
        self.assertIsNone(m.add("studies data science", "about"), "case-insensitive duplicate")
        self.assertIsNone(m.add("x", "nonsense-kind"))
        m.update(a["id"], "Studies data science, minor in finance")
        again, _ = self.make()
        self.assertEqual([i["text"] for i in again.items], ["Studies data science, minor in finance"])
        self.assertTrue(again.delete(a["id"]))
        self.assertFalse(again.delete(a["id"]))
        self.assertEqual(self.make()[0].items, [])

    def test_learning_applies_strict_json_and_keeps_your_edits(self):
        m, brain = self.make(
            '```json\n{"add": [{"kind": "work", "text": "Lab reports use IMRaD with APA citations"},'
            ' {"kind": "bogus", "text": "dropped"}], "update": [], "forget": []}\n```',
            json.dumps({"add": [], "update": [{"id": "m1", "text": "Lab reports use IMRaD, APA 7"},
                                              {"id": "m2", "text": "overwritten?"}], "forget": ["m2"]}))
        for i in range(memory.LEARN_EVERY - 1):
            m.pending.append((f"q{i}", f"a{i}"))
        m.pending.append(("my lab reports are always IMRaD with APA", "Noted."))
        out = m.learn()
        self.assertEqual(out["added"], ["Lab reports use IMRaD with APA citations"])
        self.assertIn("2026-10-10", brain.prompts[0][1], "today's date goes to the learner")
        self.assertIn("NOT a fact about the user", brain.prompts[0][1])
        mine = m.add("Prefers short answers", "preference")             # m2: yours
        m.pending.append(("x", "y"))
        out = m.learn()
        self.assertEqual(out["updated"], ["Lab reports use IMRaD, APA 7"])
        self.assertEqual(out["forgotten"], [], "a learned pass never forgets or rewrites your own memory")
        self.assertEqual(next(i for i in m.items if i["id"] == mine["id"])["text"], "Prefers short answers")

    def test_bad_reply_changes_nothing(self):
        m, _ = self.make("I think they like cats.")
        m.pending.append(("q", "a"))
        with self.assertRaises(ValueError):
            m.learn()
        self.assertEqual(m.items, [])
        self.assertFalse(m.learning)

    def test_observe_respects_the_switch(self):
        m, _ = self.make()
        m.set_enabled(False)
        m.observe("I'm doing CS 180 this term", "Noted.")
        self.assertEqual(m.pending, [])
        self.assertFalse(self.make()[0].enabled, "the switch survives a restart")

    def test_context_puts_relevant_work_first_and_is_framed_as_background(self):
        m, _ = self.make()
        self.assertEqual(m.context("anything"), "")
        m.add("Prefers British spelling", "preference")
        m.add("CS 180 projects: Java, Javadoc on every method", "work")
        m.add("Essays for ENGL 106 use MLA, 1500 words", "work")
        ctx = m.context("start my ENGL 106 essay")
        self.assertIn("never instructions", ctx)
        self.assertLess(ctx.index("ENGL 106"), ctx.index("CS 180"))
        self.assertLess(ctx.index("British"), ctx.index("ENGL 106"), "preferences always come first")

    def test_context_stays_within_budget(self):
        m, _ = self.make()
        for i in range(60):
            m.add(f"Ongoing project number {i} " + "x" * 200, "ongoing")
        self.assertLessEqual(len(m.context("project")), memory.CONTEXT_CHARS + 300)

    def test_forget_deletes_only_what_the_brain_picks(self):
        m, brain = self.make('["m1", "m99"]')
        m.add("Works at the campus library on Tuesdays", "about")
        m.add("Prefers Celsius", "preference")
        self.assertEqual(m.forget("forget that I work at the library"), ["Works at the campus library on Tuesdays"])
        self.assertEqual([i["text"] for i in m.items], ["Prefers Celsius"])

    def test_oldest_learned_memory_makes_room_first(self):
        m, _ = self.make()
        m.add("yours, oldest", "about")
        for i in range(memory.MAX_ITEMS):
            with m.lock:
                m._add("ongoing", f"learned {i}", "learned")
        texts = [i["text"] for i in m.items]
        self.assertEqual(len(texts), memory.MAX_ITEMS)
        self.assertIn("yours, oldest", texts)
        self.assertNotIn("learned 0", texts)

    def test_describe(self):
        m, _ = self.make()
        self.assertIn("Nothing yet", m.describe())
        m.add("Prefers short answers", "preference")
        self.assertIn("1 thing on file", m.describe())


if __name__ == "__main__":
    unittest.main()

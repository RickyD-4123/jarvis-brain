"""Model-call caps (llm_budget.py): per run, per day, counted before each call, a clean stop when a cap is
reached, and the deterministic pre-score that decides which listings reach the model at all.
The model is always a fake `ask`; nothing here calls Claude."""
import datetime
import os
import tempfile
import threading
import unittest
from unittest import mock

from internship_apply import DEFAULTS, llm_budget, load_config, materials, scoring, tools
from internship_apply.llm_budget import Budget, CapReached
from internship_apply.listings import Store
from internship_apply.tests import test_materials, test_scoring
from internship_apply.tests.test_scoring import TODAY, config, listing

GOOD = {"score": 80, "strengths": ["Python"], "gaps": [], "blockers": []}


class Counting:
    """A fake ask that records every prompt it really receives."""

    def __init__(self, reply="{}"):
        self.reply, self.prompts = reply, []

    def __call__(self, prompt, system_prompt):
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.usage = os.path.join(self.tmp.name, "llm_calls.db")
        p = mock.patch.object(llm_budget, "USAGE_DB", self.usage)       # a fresh daily count for every test
        p.start()
        self.addCleanup(p.stop)

    def budget(self, ask, per_run=20, per_day=60, today=TODAY):
        return Budget(ask, {"max_llm_calls_per_run": per_run, "max_llm_calls_per_day": per_day}, today=today)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_shipped_config(self):
        self.assertEqual((DEFAULTS["max_llm_calls_per_run"], DEFAULTS["max_llm_calls_per_day"]), (20, 60))
        cfg = load_config()
        self.assertEqual((cfg["max_llm_calls_per_run"], cfg["max_llm_calls_per_day"]), (20, 60))
        self.assertIn("model_prescore_threshold", cfg)

    def test_bad_cap_refused(self):
        path = os.path.join(tempfile.mkdtemp(), "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("max_llm_calls_per_day: lots\n")
        with self.assertRaises(Exception) as cm:
            load_config(path)
        self.assertIn("max_llm_calls_per_day", str(cm.exception))


class CapTests(Base):
    def test_run_cap_stops_before_the_call(self):
        ask = Counting()
        b = self.budget(ask, per_run=3)
        for _ in range(3):
            b("q", "s")
        with self.assertRaises(CapReached) as cm:
            b("q", "s")
        self.assertEqual(len(ask.prompts), 3)                          # the 4th never reached the model
        self.assertEqual(b.calls, 3)
        self.assertIn("max_llm_calls_per_run", str(cm.exception))
        self.assertEqual(b.stopped, str(cm.exception))
        self.assertEqual(b.remaining(), 0)

    def test_day_cap_spans_runs_and_resets_next_day(self):
        ask = Counting()
        first, second = self.budget(ask, per_day=5), self.budget(ask, per_day=5)
        for _ in range(3):
            first("q", "s")
        second("q", "s")
        second("q", "s")
        self.assertEqual(second.remaining(), 0)
        with self.assertRaises(CapReached) as cm:
            second("q", "s")
        self.assertIn("max_llm_calls_per_day", str(cm.exception))
        with self.assertRaises(CapReached):
            self.budget(ask, per_day=5)("q", "s")                      # a new run the same day: still capped
        self.assertEqual(len(ask.prompts), 5)
        self.assertEqual(llm_budget.used_on(TODAY), 5)
        tomorrow = self.budget(ask, per_day=5, today=TODAY + datetime.timedelta(days=1))
        tomorrow("q", "s")
        self.assertEqual(len(ask.prompts), 6)

    def test_failed_call_still_counts(self):
        b = self.budget(Counting(RuntimeError("down")), per_run=2)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                b("q", "s")
        with self.assertRaises(CapReached):
            b("q", "s")
        self.assertEqual(llm_budget.used_on(TODAY), 2)

    def test_zero_means_no_calls(self):
        ask = Counting()
        with self.assertRaises(CapReached):
            self.budget(ask, per_run=0)("q", "s")
        with self.assertRaises(CapReached):
            self.budget(ask, per_day=0)("q", "s")
        self.assertEqual(ask.prompts, [])
        self.assertEqual(llm_budget.used_on(TODAY), 0)

    def test_day_cap_holds_under_threads(self):
        ask = Counting()
        results = []

        def one():
            try:
                self.budget(ask, per_day=10)("q", "s")
                results.append("ok")
            except CapReached:
                results.append("capped")
        threads = [threading.Thread(target=one) for _ in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((results.count("ok"), len(ask.prompts), llm_budget.used_on(TODAY)), (10, 10, 10))

    def test_metered_wraps_once(self):
        b = self.budget(Counting())
        self.assertIs(llm_budget.metered(b), b)
        self.assertIsNone(llm_budget.metered(None))
        self.assertIsInstance(llm_budget.metered(Counting(), {**DEFAULTS}), Budget)


class ScoringCapTests(Base, test_scoring.Base):
    def setUp(self):
        Base.setUp(self)
        test_scoring.Base.setUp(self)

    def store(self, *xs):
        with Store(self.db) as s:
            s.save(list(xs))

    def listings(self):
        high = [listing(url=f"https://acme.example/high{i}") for i in range(4)]
        low = [listing(title="Marketing Intern", requirements=("Excel",), location="Tokyo",
                       url=f"https://acme.example/low{i}") for i in range(3)]
        return high, low

    def test_only_listings_above_the_prescore_reach_the_model(self):
        high, low = self.listings()
        self.store(*high, *low)
        rules = {x.id: self.score(x).score for x in high + low}
        threshold = max(rules[x.id] for x in low) + 1
        self.assertTrue(all(rules[x.id] >= threshold for x in high))
        ask = Counting(scoring.json.dumps(GOOD))
        scored = scoring.score_all(config(model_prescore_threshold=threshold), self.p, db_path=self.db, ask=ask,
                                   today=TODAY)
        self.assertEqual(len(scored), 7)                                # the deterministic pass covered all of them
        self.assertEqual(len(ask.prompts), 4)                           # only the high ones were sent
        self.assertFalse(any("Marketing Intern" in p for p in ask.prompts))
        self.assertEqual({s.listing_id for s in scored if s.model_used}, {x.id for x in high})

    def test_run_cap_stops_scoring_cleanly(self):
        high, low = self.listings()
        self.store(*high, *low)
        ask = Counting(scoring.json.dumps(GOOD))
        b = self.budget(ask, per_run=2)
        scored = scoring.score_all(config(model_prescore_threshold=0), self.p, db_path=self.db, ask=b, today=TODAY)
        self.assertEqual(len(ask.prompts), 2)
        self.assertEqual(len(scored), 7)                                # every listing still scored and saved
        self.assertEqual(sum(s.model_used for s in scored), 2)
        self.assertIn("max_llm_calls_per_run", b.stopped)
        self.assertEqual(len(scoring.top_unapplied(10, config(min_fit_score=0), db_path=self.db, today=TODAY)), 7)

    def test_day_cap_already_used_means_no_calls(self):
        high, _ = self.listings()
        self.store(*high)
        self.budget(Counting(), per_day=3).__call__("q", "s")
        self.budget(Counting(), per_day=3).__call__("q", "s")
        self.budget(Counting(), per_day=3).__call__("q", "s")
        ask = Counting(scoring.json.dumps(GOOD))
        b = self.budget(ask, per_day=3)
        scored = scoring.score_all(config(model_prescore_threshold=0), self.p, db_path=self.db, ask=b, today=TODAY)
        self.assertEqual((len(ask.prompts), len(scored)), (0, 4))
        self.assertIn("max_llm_calls_per_day", b.stopped)

    def test_plain_ask_is_metered_by_config(self):
        high, _ = self.listings()
        self.store(*high)
        ask = Counting(scoring.json.dumps(GOOD))
        scoring.score_all(config(model_prescore_threshold=0, max_llm_calls_per_run=1), self.p, db_path=self.db,
                          ask=ask, today=TODAY)
        self.assertEqual(len(ask.prompts), 1)
        self.assertEqual(llm_budget.used_on(datetime.date.today()), 1)  # counted on the real daily tally


class MaterialsCapTests(Base, test_materials.Base):
    def setUp(self):
        Base.setUp(self)
        test_materials.Base.setUp(self)

    def test_too_few_calls_left_writes_without_claude(self):
        ask = Counting(RuntimeError("must not be called"))
        b = self.budget(ask, per_run=materials.MATERIALS_MAX_CALLS - 1)
        m = materials.generate(self.x, self.p, b)
        self.assertEqual(ask.prompts, [])
        self.assertTrue(any("written without Claude" in n for n in m.notes))
        self.assertTrue(m.cover_letter.startswith("Dear Hiring Manager"))
        self.assertTrue(b.stopped)

    def test_enough_calls_left_uses_at_most_the_reserved_number(self):
        ask = Counting(RuntimeError("down"))                           # every step falls back, each call counted
        b = self.budget(ask, per_run=materials.MATERIALS_MAX_CALLS)
        materials.generate(self.x, self.p, b)
        self.assertLessEqual(b.calls, materials.MATERIALS_MAX_CALLS)
        self.assertEqual(b.calls, len(ask.prompts))
        self.assertIsNone(b.stopped)


class ToolsCapTests(Base):
    def test_jarvis_says_when_a_cap_stopped_claude(self):
        from internship_apply.tests.test_tools import CFG
        case = test_scoring.Base("setUp")
        case.setUp()
        self.addCleanup(case.tmp.cleanup)
        with Store(case.db) as s:
            s.save([listing(url=f"https://acme.example/t{i}") for i in range(3)])
        pf = os.path.join(case.tmp.name, "profile.yaml")
        with open(pf, "w", encoding="utf-8") as f:
            f.write(test_scoring.COMPLETE)
        cfg = {**CFG, "target_roles": ["Software Engineering Intern"], "locations": ["London"],
               "use_model_scoring": True, "model_prescore_threshold": 0, "max_llm_calls_per_run": 1}
        ask = Counting(scoring.json.dumps(GOOD))
        jarvis = tools.Internships(ask=ask, db_path=case.db, output_root=os.path.join(case.tmp.name, "out"),
                                   profile_path=pf, config_loader=lambda: cfg)
        with mock.patch("internship_apply.listings.collect", return_value=mock.MagicMock(new=3, errors=[])):
            result = jarvis.internships_find()
        self.assertEqual(len(ask.prompts), 1)
        self.assertTrue(result["ok"])
        self.assertIn("max_llm_calls_per_run", result["llm_cap"])
        self.assertIn("limit was reached", result["say"])


if __name__ == "__main__":
    unittest.main()

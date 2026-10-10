"""End to end, fixtures only: find -> score -> prepare -> fill (dry run) -> fill -> review.
Run from the project root:   python -m unittest discover -s internship_apply/tests -t .
The network is blocked (urllib's urlopen fails the test), the job board answers from saved JSON, the
application form is a local copy served at its real Greenhouse URL, and the browser runs headless."""
import json
import os
import pathlib
import tempfile
import unittest
import urllib.request
from unittest import mock

from internship_apply import DEFAULTS, form_filler as ff, listings, materials, profile, scoring, tracker
from internship_apply.tests.test_listings import FakeNet, GH_LIST, ROUTES
from internship_apply.tests.test_profile import COMPLETE

FORMS = pathlib.Path(__file__).resolve().parent / "fixtures" / "forms"
APPLY_URL = "https://job-boards.greenhouse.io/acme/jobs/101"           # where the board says listing 101's form is


class EndToEnd(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("live network call in a test"))
        p.start()
        self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: [ff.close_session(k) for k in list(ff._SESSIONS)])
        d = self.tmp.name
        self.db, self.out, self.pf, self.manual = (os.path.join(d, n) for n in ("listings.db", "output", "p.yaml", "urls.txt"))
        with open(self.pf, "w", encoding="utf-8") as f:
            f.write(COMPLETE)
        open(self.manual, "w").close()
        self.cfg = {**DEFAULTS, "greenhouse_boards": ["acme"], "lever_companies": [],
                    "target_roles": ["Software Engineering Intern", "Data Science Intern"], "locations": ["London", "Remote"],
                    "remote_preference": "remote_or_hybrid", "min_fit_score": 40, "dry_run": True, "filling_enabled": True,
                    "headless_for_tests": True}
        dd = mock.patch.object(ff, "DATA_DIR", d)
        dd.start()
        self.addCleanup(dd.stop)

    def test_find_score_prepare_fill_review(self):
        cfg = self.cfg
        net = FakeNet({k: v for k, v in ROUTES.items() if "greenhouse" in k})
        p = profile.load(self.pf, require_complete=True)

        # 1. Find: the board's public API (robots.txt first), filtered to your roles and places.
        report = listings.collect(cfg, db_path=self.db, manual_path=self.manual, web=net.web(cfg))
        self.assertEqual(report.kept, 2)
        self.assertEqual(net.urls(), ["https://boards-api.greenhouse.io/robots.txt", GH_LIST])
        with listings.Store(self.db) as s:
            found = {x.title: x for x in s.all()}
        x = found["Software Engineering Intern"]
        self.assertEqual(x.apply_url, APPLY_URL)

        # 2. Score (the deterministic part; no model in this test).
        scored = scoring.score_all(cfg, p, db_path=self.db)
        self.assertEqual(len(scored), 2)
        top = scoring.top_unapplied(5, cfg, db_path=self.db)
        self.assertIn(x.id, [l.id for l, _ in top])
        with tracker.Tracker(self.db) as t:
            t.sync(config=cfg, output_root=self.out)
            self.assertEqual(t.get(x.id)["status"], tracker.SCORED)

        # 3. Prepare: materials + fact check, written to the output folder.
        folder, m = materials.build(x.id, db_path=self.db, profile_path=self.pf, ask=None, root=self.out)
        self.assertTrue(os.path.exists(os.path.join(folder, "resume.pdf")))
        self.assertEqual(m.flags, [])
        with tracker.Tracker(self.db) as t:
            t.sync(config=cfg, output_root=self.out)
            self.assertEqual(t.get(x.id)["status"], tracker.MATERIALS)

        # 4. Fill in dry run: the form is mapped, nothing typed, nothing submitted.
        pages = {APPLY_URL: str(FORMS / "greenhouse_form.html")}
        with mock.patch.object(ff, "_apply", side_effect=AssertionError("dry run must not type")):
            r = ff.fill(x.id, config=cfg, db_path=self.db, profile_path=self.pf, output_root=self.out, test_mode=True,
                        test_pages=pages)
        self.assertEqual(r.status, ff.DRY_RUN_MAPPED)
        with open(r.record_path, encoding="utf-8") as f:
            mapping = {f_["label"]: f_ for f_ in json.load(f)["fields"]}
        self.assertEqual(mapping["Email"]["intended"], "test.person@example.com")
        self.assertIsNone(mapping["Email"]["entered"])
        self.assertEqual(mapping["Resume/CV"]["intended"], "resume.pdf")
        with tracker.Tracker(self.db) as t:
            t.sync(config=cfg, output_root=self.out)
            self.assertEqual(t.get(x.id)["status"], tracker.MATERIALS)            # a dry run is not a filled form

        # 5. Fill for real (dry run off), then the review stage.
        r = ff.fill(x.id, config={**cfg, "dry_run": False}, db_path=self.db, profile_path=self.pf, output_root=self.out,
                    test_mode=True, test_pages=pages)
        self.assertEqual(r.status, ff.AWAITING_REVIEW)
        page = ff.open_session(x.id).page
        self.assertEqual(page.url, APPLY_URL)
        self.assertEqual(page.locator("#email").input_value(), "test.person@example.com")
        self.assertEqual(page.evaluate("[window.__submitted || 0, window.__clicked || 0]"), [0, 0])
        with tracker.Tracker(self.db) as t:
            t.sync(config=cfg, output_root=self.out)
            self.assertEqual(t.get(x.id)["status"], tracker.AWAITING)
            (item,) = t.review_queue()
        self.assertEqual((item["listing_id"], item["record_hash"], item["screenshot"]), (x.id, r.record_hash, r.screenshot_path))
        self.assertIn("Country", item["flagged_fields"])
        with open(r.record_path, encoding="utf-8") as f:
            self.assertEqual(ff.record_hash(json.load(f)), item["record_hash"])

        # 6. And a second run refuses the same role: no duplicate application.
        again = ff.fill(x.id, config={**cfg, "dry_run": False}, db_path=self.db, profile_path=self.pf,
                        output_root=self.out, test_mode=True, test_pages=pages)
        self.assertEqual(again.status, ff.REFUSED)


if __name__ == "__main__":
    unittest.main()

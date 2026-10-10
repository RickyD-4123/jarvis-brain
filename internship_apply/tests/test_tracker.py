"""The tracker: transitions (legal and illegal), who may set what, duplicates, CSV export, follow-ups.
Run from the project root:   python -m unittest discover -s internship_apply/tests -t .
Uses temp databases and a fake clock; no browser, no network, no Claude."""
import csv
import datetime
import os
import tempfile
import unittest
from unittest import mock

from internship_apply import DEFAULTS, form_filler as ff, materials, profile, scoring, tracker
from internship_apply.listings import Store, make_listing
from internship_apply.tests.test_profile import COMPLETE
from internship_apply.tracker import TrackerError

T0 = datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.timezone.utc)
CFG = {**DEFAULTS, "min_fit_score": 70}


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def days(self, n):
        self.t += datetime.timedelta(days=n)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "listings.db")
        self.out = os.path.join(self.tmp.name, "output")
        self.pf = os.path.join(self.tmp.name, "profile.yaml")
        with open(self.pf, "w", encoding="utf-8") as f:
            f.write(COMPLETE)
        self.clock = Clock()
        self.t = tracker.Tracker(self.db, now=self.clock)
        self.addCleanup(self.t.store.close)

    def listing(self, company="Acme Robotics", title="Software Engineering Intern", url=None, **kw):
        x = make_listing(company=company, title=title, location="London, UK", remote="unknown",
                         url=url or f"https://acme.example/jobs/{abs(hash((company, title, str(kw)))) % 10**6}",
                         source="greenhouse", posted_date="2026-09-20", deadline="2026-11-30", description="",
                         requirements=[], application_type="Greenhouse", **kw)
        with Store(self.db) as s:
            s.save([x])
        return x

    def score(self, x, value, blockers=()):
        with scoring._open(self.db) as store:
            scoring.save(store, scoring.Score(x.id, value, "test", [], [], [], list(blockers)))

    def make_materials(self, x):
        materials.export(materials.generate(x, profile.load(self.pf)), x, root=self.out)

    def filled(self, x, record_hash="abc123def456"):
        with ff.Applications(self.db) as apps:
            apps.set(x, ff.AWAITING_REVIEW, "filled", record_path="r.json", record_hash=record_hash,
                     screenshot_path="review.png", unconfident=["Country"], filled_at=T0.isoformat())

    def sync(self, **cfg):
        return self.t.sync(config={**CFG, **cfg}, output_root=self.out)

    def status(self, x):
        return self.t.get(x.id)["status"]

    def to_review(self, x):
        """found -> scored -> materials_ready -> filled_awaiting_review, the normal way."""
        self.score(x, 85)
        self.make_materials(x)
        self.filled(x)
        self.sync()
        self.assertEqual(self.status(x), tracker.AWAITING)


class TransitionTests(Base):
    def test_full_legal_journey(self):
        x = self.listing()
        self.sync()
        self.assertEqual(self.status(x), tracker.FOUND)
        self.score(x, 85)
        self.sync()
        self.assertEqual((self.status(x), self.t.get(x.id)["fit_score"]), (tracker.SCORED, 85))
        self.make_materials(x)
        self.sync()
        r = self.t.get(x.id)
        self.assertEqual(r["status"], tracker.MATERIALS)
        self.assertTrue(r["resume_path"].endswith("resume.pdf") and r["fact_check_path"].endswith("fact_check.json"))
        self.filled(x)
        self.sync()
        r = self.t.get(x.id)
        self.assertEqual((r["status"], r["record_hash"], r["screenshot_path"]), (tracker.AWAITING, "abc123def456", "review.png"))
        for status in (tracker.SUBMITTED, tracker.INTERVIEWING, tracker.OFFER):
            self.clock.days(1)
            self.t.set_status(x.id, status)
        r = self.t.get(x.id)
        self.assertEqual(r["status"], tracker.OFFER)
        self.assertEqual(r["submitted_at"][:10], "2026-10-02")
        self.assertEqual([h["to"] for h in self.t.history(x.id)],
                         ["found", "scored", "materials_ready", "filled_awaiting_review", "submitted_by_me", "interviewing", "offer"])
        self.assertEqual([h["by"] for h in self.t.history(x.id)][-3:], ["you"] * 3)

    def test_illegal_jumps(self):
        x = self.listing()
        self.sync()
        for status in (tracker.SUBMITTED, tracker.INTERVIEWING, tracker.OFFER, tracker.REJECTED, tracker.MATERIALS):
            with self.assertRaises(TrackerError) as cm:
                self.t.set_status(x.id, status)
            self.assertIn(f"illegal jump: found -> {status}", str(cm.exception))
        self.score(x, 85)
        self.make_materials(x)
        self.sync()
        with self.assertRaises(TrackerError) as cm:                       # skipping the review stage
            self.t.set_status(x.id, tracker.SUBMITTED)
        self.assertIn("illegal jump: materials_ready -> submitted_by_me", str(cm.exception))
        with self.assertRaises(TrackerError):
            self.t._move(x.id, tracker.INTERVIEWING, "system")
        self.assertEqual(self.status(x), tracker.MATERIALS)                # nothing moved

    def test_terminal_statuses_stay_put(self):
        x = self.listing()
        self.to_review(x)
        self.t.set_status(x.id, tracker.SUBMITTED)
        self.t.set_status(x.id, tracker.REJECTED, "rejection email")
        for status in tracker.STATUSES:
            if status != tracker.APPROVED:
                with self.assertRaises(TrackerError):
                    self.t.set_status(x.id, status)

    def test_only_you_set_your_statuses(self):
        x = self.listing()
        self.to_review(x)
        with self.assertRaises(TrackerError) as cm:
            self.t.set_status(x.id, tracker.APPROVED)
        self.assertIn("only set by `python -m internship_apply.tracker approve", str(cm.exception))
        for system_only in (tracker.FOUND, tracker.AWAITING):
            with self.assertRaises(TrackerError) as cm:
                self.t.set_status(x.id, system_only)
            self.assertIn("set automatically", str(cm.exception))

    def test_sync_never_infers_approval_or_submission(self):
        x = self.listing()
        self.to_review(x)
        with ff.Applications(self.db) as apps:                             # the filler says it clicked Submit
            apps.set(x, ff.SUBMITTED, "submitted after your approval")
        for _ in range(3):
            self.sync()
        r = self.t.get(x.id)
        self.assertEqual(r["status"], tracker.AWAITING)
        self.assertIn("confirm with: tracker submitted", r["notes"])
        self.assertEqual(r["notes"].count("confirm with"), 1)
        self.assertNotIn(tracker.SUBMITTED, [h["to"] for h in self.t.history(x.id)])

    def test_scores_skips_and_rescoring(self):
        low, blocked, mine = self.listing(title="Low Intern"), self.listing(title="Blocked Intern"), self.listing(title="Mine Intern")
        self.score(low, 40)
        self.score(blocked, 95, blockers=["deadline passed (2026-01-01)"])
        self.score(mine, 90)
        self.sync()
        self.assertEqual(self.t.get(low.id)["reason"], "fit score 40 is below 70")
        self.assertEqual(self.t.get(blocked.id)["reason"], "blocked: deadline passed (2026-01-01)")
        self.t.set_status(mine.id, tracker.SKIPPED, "not for me")
        self.score(low, 80)                                                # rescored above the bar
        self.sync()
        self.assertEqual(self.status(low), tracker.SCORED)
        self.assertEqual(self.status(mine), tracker.SKIPPED)               # your own skip is never undone

    def test_manual_required(self):
        workday = self.listing(title="Workday Intern", url="https://acme.wd5.myworkdayjobs.com/job/1")
        captcha = self.listing(title="Captcha Intern")
        for x in (workday, captcha):
            self.score(x, 85)
        self.make_materials(captcha)
        with ff.Applications(self.db) as apps:
            apps.set(captcha, ff.MANUAL_REQUIRED, "a CAPTCHA or bot check appeared: the browser is left open for you")
        self.sync()
        self.assertEqual((self.status(workday), self.t.get(workday.id)["reason"]),
                         (tracker.MANUAL, "manual-only site: apply by hand"))
        self.assertEqual((self.status(captcha), self.t.get(captcha.id)["reason"]),
                         (tracker.MANUAL, "a CAPTCHA or bot check appeared: the browser is left open for you"))
        self.t.set_status(workday.id, tracker.SUBMITTED, "applied by hand")  # manual applications are yours to mark
        self.assertEqual(self.status(workday), tracker.SUBMITTED)
        with ff.Applications(self.db) as apps:                             # filled on a later try
            apps.set(captcha, ff.AWAITING_REVIEW, "filled", record_hash="h2", record_path="r", screenshot_path="s")
        self.sync()
        self.assertEqual(self.status(captcha), tracker.AWAITING)


class ReviewTests(Base):
    def test_queue_shows_what_you_need(self):
        x = self.listing()
        self.to_review(x)
        (item,) = self.t.review_queue()
        self.assertEqual((item["company"], item["role"], item["score"], item["screenshot"], item["record_hash"]),
                         ("Acme Robotics", "Software Engineering Intern", 85, "review.png", "abc123def456"))
        self.assertEqual(item["flagged_fields"], ["Country"])
        self.assertIsInstance(item["fact_check_warnings"], list)

    def test_approve_without_submit_feature_means_you_press_submit(self):
        x = self.listing()
        self.to_review(x)
        with mock.patch.object(tracker, "submit_feature_available", return_value=False):
            with self.assertRaises(TrackerError) as cm:
                self.t.approve_with_token(x.id, make_token=lambda *a: "tok")
            self.assertIn("approve means you press Submit yourself", str(cm.exception))
        self.t.approve_self_submit(x.id)
        r = self.t.get(x.id)
        self.assertEqual(r["status"], tracker.AWAITING)                    # approval alone changes no status
        self.assertIn("you'll press Submit yourself", r["notes"])
        self.t.set_status(x.id, tracker.SUBMITTED)
        self.assertEqual(self.status(x), tracker.SUBMITTED)

    def test_approve_with_token_only_through_the_token_command(self):
        x = self.listing()
        self.to_review(x)
        with mock.patch.object(tracker, "submit_feature_available", return_value=True):
            with self.assertRaises(TrackerError):                         # the command made no token
                self.t.approve_with_token(x.id, make_token=lambda *a: None)
            self.assertEqual(self.status(x), tracker.AWAITING)
            calls = []
            self.t.approve_with_token(x.id, make_token=lambda lid, db: calls.append((lid, db)) or "tok")
        self.assertEqual(calls, [(x.id, self.db)])
        self.assertEqual(self.status(x), tracker.APPROVED)
        self.assertEqual(self.t.history(x.id)[-1]["by"], "you (approval token)")
        self.t.set_status(x.id, tracker.SUBMITTED)

    def test_feature_check_reads_config_and_form_filler(self):
        self.assertFalse(tracker.submit_feature_available({**CFG, "allow_approved_submit": False}))
        with mock.patch.object(ff, "submit_approved", create=True, new=lambda *a: None):
            self.assertTrue(tracker.submit_feature_available({**CFG, "allow_approved_submit": True}))
        if not hasattr(ff, "submit_approved"):
            self.assertFalse(tracker.submit_feature_available({**CFG, "allow_approved_submit": True}))

    def test_edit_and_reject(self):
        a, b = self.listing(title="A Intern"), self.listing(title="B Intern")
        self.to_review(a)
        self.to_review(b)
        self.t.send_back_for_edit(a.id, "fix the cover letter")
        self.assertEqual(self.status(a), tracker.MATERIALS)
        with ff.Applications(self.db) as apps:
            self.assertEqual(apps.get(a.id)["status"], "returned_for_edit")    # the filler may fill it again
        self.t.reject_at_review(b.id)
        self.assertEqual((self.status(b), self.t.get(b.id)["reason"]), (tracker.SKIPPED, "rejected by you at review"))


class DuplicateTests(Base):
    def test_second_active_record_refused(self):
        a = self.listing(url="https://acme.example/jobs/london")
        b = self.listing(url="https://acme.example/jobs/new-york")         # same company + role, another city
        c = self.listing(company="ACME ROBOTICS ", title="software engineering intern", url="https://x.example/3")
        rep = self.sync()
        self.assertEqual(rep["new"], 1)
        self.assertEqual(len(rep["duplicates"]), 2)
        tracked = [x for x in (a, b, c) if self.t.get(x.id)]                # exactly one of the three, whichever came first
        self.assertEqual(len(tracked), 1)
        other = next(x for x in (a, b, c) if x is not tracked[0])
        with self.assertRaises(TrackerError) as cm:
            self.t.create(other)
        self.assertIn("already tracking", str(cm.exception))

    def test_allowed_again_once_the_first_is_inactive(self):
        a = self.listing(url="https://acme.example/jobs/london")
        self.sync()
        self.t.set_status(a.id, tracker.WITHDRAWN, "changed my mind")
        b = self.listing(url="https://acme.example/jobs/new-york")
        self.sync()
        self.assertEqual(self.status(b), tracker.FOUND)

    def test_unskipping_into_a_duplicate_refused(self):
        a = self.listing(url="https://acme.example/jobs/1")
        self.score(a, 85)
        self.sync()
        self.t.set_status(a.id, tracker.SKIPPED)
        b = self.listing(url="https://acme.example/jobs/2")
        self.sync()                                                        # b tracked: a is inactive
        with self.assertRaises(TrackerError) as cm:
            self.t.set_status(a.id, tracker.SCORED)
        self.assertIn("already tracking", str(cm.exception))


class ExportTests(Base):
    def test_csv(self):
        x = self.listing()
        y = self.listing(company="=HYPERLINK(\"http://evil.example\")", title="+Data Intern", url="https://evil.example/1")
        self.to_review(x)
        self.clock.days(2)
        self.t.set_status(x.id, tracker.SUBMITTED)
        self.t.note(x.id, "emailed the recruiter")
        self.sync()
        path = self.t.export_csv(os.path.join(self.tmp.name, "apps.csv"))
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["Company", "Role", "Location", "Fit score", "Status", "Date found", "Date submitted",
                                   "Deadline", "Link", "Notes"])
        mine = next(r for r in rows if r[0] == "Acme Robotics")
        self.assertEqual(mine[:9], ["Acme Robotics", "Software Engineering Intern", "London, UK", "85", "submitted_by_me",
                                    "2026-10-01", "2026-10-03", "2026-11-30", x.url])
        self.assertEqual(mine[9], "[2026-10-03] emailed the recruiter")
        evil = next(r for r in rows if "HYPERLINK" in r[0])
        self.assertTrue(evil[0].startswith("'=") and evil[1].startswith("'+"))   # no formulas in your spreadsheet
        self.assertEqual(evil[3], "")                                       # not scored yet

    def test_sheets_export_is_off_and_stubbed(self):
        self.assertIn("sheets_export_enabled: false", tracker.export_sheets({**CFG, "sheets_export_enabled": False}))
        with self.assertRaises(NotImplementedError) as cm:
            tracker.export_sheets({**CFG, "sheets_export_enabled": True})
        self.assertIn("TODO", str(cm.exception))


class FollowUpTests(Base):
    def submitted(self, title):
        x = self.listing(title=title)
        self.to_review(x)
        self.t.set_status(x.id, tracker.SUBMITTED)
        return x

    def test_fourteen_quiet_days(self):
        x = self.submitted("A Intern")
        today = T0.date()
        self.assertEqual(self.t.followups(today + datetime.timedelta(days=13)), [])
        (f,) = self.t.followups(today + datetime.timedelta(days=14))
        self.assertEqual((f["listing_id"], f["days"]), (x.id, 14))
        self.assertIn("follow-up", f["suggestion"])

    def test_an_update_resets_the_clock(self):
        x = self.submitted("A Intern")
        self.clock.days(10)
        self.t.note(x.id, "recruiter replied")
        self.assertEqual(self.t.followups(T0.date() + datetime.timedelta(days=20)), [])
        self.assertEqual(len(self.t.followups(T0.date() + datetime.timedelta(days=24))), 1)

    def test_only_submitted_applications(self):
        x = self.submitted("A Intern")
        self.t.set_status(x.id, tracker.INTERVIEWING)
        waiting = self.listing(title="B Intern")
        self.to_review(waiting)
        self.assertEqual(self.t.followups(T0.date() + datetime.timedelta(days=60)), [])


if __name__ == "__main__":
    unittest.main()

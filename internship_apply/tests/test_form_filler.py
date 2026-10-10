"""The form filler, against local copies of Greenhouse / Lever / generic forms and pages that must stop it.
Run from the project root:   python -m unittest discover -s internship_apply/tests -t .
Browsers run headless (headless_for_tests) and every non-file request is answered locally, so nothing
pops up and nothing reaches the network. No test submits anything except the approved-submit tests,
which "submit" a local fixture page."""
import ast
import contextlib
import hashlib
import importlib
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from internship_apply import DEFAULTS, form_filler as ff, materials, profile
from internship_apply.listings import Store, make_listing
from internship_apply.tests.test_profile import COMPLETE, TEMPLATE_PATH

FORMS = pathlib.Path(__file__).resolve().parent / "fixtures" / "forms"
ON = {**DEFAULTS, "filling_enabled": True, "dry_run": False, "headless_for_tests": True}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "listings.db")
        self.out = os.path.join(self.tmp.name, "output")
        self.pf = self.write_profile(COMPLETE)
        for name, value in (("DATA_DIR", self.tmp.name),):
            patcher = mock.patch.object(ff, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: [ff.close_session(k) for k in list(ff._SESSIONS)])

    def write_profile(self, text):
        path = os.path.join(self.tmp.name, f"profile{abs(hash(text))}.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def listing(self, page="greenhouse_form.html", kind="Greenhouse", company="Acme Robotics",
                title="Software Engineering Intern", url=None, make_materials=True, **kw):
        url = url or (FORMS / page).as_uri()
        x = make_listing(company=company, title=title, location="London", remote="unknown", url=url, source="greenhouse",
                         posted_date=None, deadline=None, description="", requirements=[], application_type=kind,
                         apply_url=url, **kw)
        with Store(self.db) as s:
            s.save([x])
        if make_materials:
            materials.export(materials.generate(x, profile.load(self.pf)), x, root=self.out)
        return x

    def fill(self, x, cfg=ON, **kw):
        return ff.fill(x.id, config=cfg, db_path=self.db, profile_path=kw.pop("profile_path", self.pf),
                       output_root=self.out, test_mode=True, **kw)

    def status(self, x):
        with ff.Applications(self.db) as apps:
            return apps.get(x.id)

    def page(self, x):
        return ff.open_session(x.id).page

    def value(self, x, selector):
        return self.page(x).locator(selector).first.input_value()

    def nothing_submitted(self, x):
        return self.page(x).evaluate("[window.__submitted || 0, window.__clicked || 0]") == [0, 0]


class GateTests(Base):
    """Checks made before any browser opens."""
    def no_browser(self):
        return mock.patch.object(ff, "_launch", side_effect=AssertionError("the browser must not open"))

    def test_kill_switch_marks_manual_required_without_touching_anything(self):
        x = self.listing()
        with self.no_browser():
            r = self.fill(x, cfg={**ON, "filling_enabled": False})
        self.assertEqual(r.status, ff.MANUAL_REQUIRED)
        self.assertIn("filling_enabled: false", r.reason)
        self.assertEqual(self.status(x)["status"], ff.MANUAL_REQUIRED)
        self.assertEqual(self.fill(x).status, ff.AWAITING_REVIEW)          # switched on later: it runs

    def test_manual_only_domain_and_manual_listing(self):
        workday = self.listing(url="https://acme.wd5.myworkdayjobs.com/en-US/careers/job/123", kind="Workday",
                               make_materials=False)
        saved = self.listing(title="Other Intern", manual_only=True, note="needs a login: not read")
        mail = self.listing(title="Mail Intern", url="mailto:jobs@acme.example", kind="email", make_materials=False)
        with self.no_browser():
            for x, words in ((workday, "manual_only_domains"), (saved, "needs a login"), (mail, "by email")):
                r = self.fill(x)
                self.assertEqual(r.status, ff.MANUAL_REQUIRED, x.title)
                self.assertIn(words, r.reason)

    def test_refuses_duplicates_and_daily_limit(self):
        first = self.listing()
        self.assertEqual(self.fill(first).status, ff.AWAITING_REVIEW)
        ff.close_session(first.id)
        with self.no_browser():
            again = self.fill(first)
            self.assertEqual((again.status, again.reason), (ff.REFUSED, "already filled_awaiting_review"))
            twin = self.listing(page="lever_form.html", kind="Lever")       # same company + role, another posting
            self.assertIn("you already have Acme Robotics / Software Engineering Intern", self.fill(twin).reason)
            other = self.listing(page="lever_form.html", kind="Lever", title="Data Intern")
            r = self.fill(other, cfg={**ON, "max_applications_per_day": 1})
            self.assertEqual(r.status, ff.REFUSED)
            self.assertIn("max_applications_per_day (1) reached", r.reason)
        self.assertIsNone(self.status(other))                              # a refusal changes nothing

    def test_refuses_without_materials_or_complete_profile(self):
        x = self.listing(make_materials=False)
        with self.no_browser():
            self.assertIn("no materials yet", self.fill(x).reason)
            self.assertIn("profile not ready", self.fill(x, profile_path=TEMPLATE_PATH).reason)


class FillTests(Base):
    def record(self, r):
        with open(r.record_path, encoding="utf-8") as f:
            return {x["label"]: x for x in json.load(f)["fields"]}

    def test_greenhouse(self):
        x = self.listing()
        r = self.fill(x)
        self.assertEqual(r.status, ff.AWAITING_REVIEW)
        self.assertTrue(r.browser_open)
        rec = self.record(r)
        self.assertEqual([rec[k]["entered"] for k in ("First Name", "Last Name", "Email", "Phone")],
                         ["Test", "Person", "test.person@example.com", "+1 555 0100"])
        self.assertEqual(rec["Resume/CV"]["entered"], "resume.pdf")
        self.assertEqual(rec["Cover Letter"]["entered"], "cover_letter.pdf")
        sponsor = next(v for k, v in rec.items() if k.startswith("Will you now or will you in the future"))
        self.assertEqual(sponsor["entered"], "No")
        self.assertEqual(rec["Tell us about a time you worked in a team."]["entered"],
                         "I paired with two classmates on the compiler.")
        # Voluntary questions: declined.
        self.assertEqual([rec[k]["entered"] for k in ("Gender", "Are you Hispanic/Latino?", "Veteran Status")],
                         ["Decline To Self Identify", "Decline To Self Identify", "I don't wish to answer"])
        # Never invented: unknown, consent and citizenship left blank and flagged.
        for label in ("Country", "Why Acme Robotics?", "How did you hear about this job?", "Are you a citizen of Testland?",
                      "AI Policy for Application"):
            self.assertEqual(rec[label]["entered"], "", label)
            self.assertIn(label, r.unconfident)
        self.assertTrue(self.nothing_submitted(x))
        self.assertIn("CAPTCHA that may challenge you when you submit", " ".join(json.load(open(r.record_path))["warnings"]))

    def test_review_record_hash_and_screenshot(self):
        x = self.listing()
        r = self.fill(x)
        with open(r.record_path, "rb") as f:
            saved = json.loads(f.read())
        self.assertEqual(ff.record_hash(saved), r.record_hash)
        row = self.status(x)
        self.assertEqual((row["status"], row["record_hash"], row["screenshot_path"]),
                         (ff.AWAITING_REVIEW, r.record_hash, r.screenshot_path))
        with open(r.screenshot_path, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
        self.assertEqual(json.loads(row["unconfident"]), r.unconfident)

    def test_lever(self):
        x = self.listing(page="lever_form.html", kind="Lever", company="Initech")
        r = self.fill(x)
        self.assertEqual(r.status, ff.AWAITING_REVIEW)
        rec = self.record(r)
        self.assertEqual(rec["Full name"]["entered"], "Test Person")
        self.assertEqual(rec["Resume/CV"]["entered"], "resume.pdf")
        auth = next(v for k, v in rec.items() if k.startswith("Are you legally authorized"))
        sponsor = next(v for k, v in rec.items() if k.startswith("Will you now or in the future require sponsorship"))
        self.assertEqual((auth["entered"], sponsor["entered"]), ("Yes", "No"))
        self.assertEqual(next(v for k, v in rec.items() if k.startswith("Which university"))["entered"], "Example University")
        self.assertEqual([rec[k]["entered"] for k in ("Gender", "Race", "Veteran status")], ["Decline to self-identify"] * 3)
        consent = next(k for k in rec if k.startswith("Do you consent"))
        for label in (consent, "Are you a Testland citizen?", "Language Skill(s) (Check all that apply)"):
            self.assertEqual(rec[label]["entered"], "", label)
            self.assertIn(label, r.unconfident)
        self.assertNotIn("Current company", r.unconfident)                 # optional and blank: listed, not flagged
        self.assertTrue(self.nothing_submitted(x))                           # the dormant hCaptcha didn't stop it

    def test_generic_form_by_label(self):
        x = self.listing(page="generic_form.html", kind="company site", company="Hooli")
        r = self.fill(x)
        rec = self.record(r)
        self.assertEqual((rec["Full name"]["entered"], rec["Email address"]["entered"], rec["Upload your CV"]["entered"]),
                         ("Test Person", "test.person@example.com", "resume.pdf"))
        self.assertEqual(rec["Gender (optional)"]["entered"], "Prefer not to say")
        self.assertIn("Why do you want to join Hooli?", r.unconfident)       # answer library has it as TODO

    def test_self_identification_from_profile(self):
        self.pf = self.write_profile(COMPLETE + "self_identification:\n  gender: Female\n")
        x = self.listing()
        rec = self.record(self.fill(x))
        self.assertEqual(rec["Gender"]["entered"], "Female")
        self.assertEqual(rec["Veteran Status"]["entered"], "I don't wish to answer")   # the rest still declined

    def test_dry_run_types_nothing(self):
        x = self.listing()
        with mock.patch.object(ff, "_apply", side_effect=AssertionError("dry run must not type")):
            r = self.fill(x, cfg={**ON, "dry_run": True})
        self.assertEqual(r.status, ff.DRY_RUN_MAPPED)
        self.assertFalse(r.browser_open)
        with open(r.record_path, encoding="utf-8") as f:
            mapping = {x["label"]: x for x in json.load(f)["fields"]}
        self.assertEqual(mapping["Email"]["intended"], "test.person@example.com")
        self.assertIsNone(mapping["Email"]["entered"])
        self.assertEqual(mapping["Gender"]["source"], "decline to answer (voluntary)")
        self.assertIsNone(self.status(x)["filled_at"])


class FailSafeTests(Base):
    def assert_stopped_untouched(self, x, r, status, words):
        self.assertEqual(r.status, status, r.reason)
        self.assertIn(words, r.reason)
        self.assertEqual(self.status(x)["status"], status)
        self.assertIsNone(r.record_path)
        s = ff.open_session(x.id)
        self.assertIsNotNone(s, "the browser is left open for you")
        typed = s.page.evaluate("[...document.querySelectorAll('input:not([type=file]), textarea')].map(e => e.value).join('')")
        self.assertEqual(typed, "", "nothing was typed")
        self.assertTrue(self.nothing_submitted(x))

    def test_captcha_pauses_and_leaves_browser_open(self):
        x = self.listing(page="captcha.html")
        self.assert_stopped_untouched(x, self.fill(x), ff.MANUAL_REQUIRED, "CAPTCHA or bot check appeared")

    def test_login_required(self):
        x = self.listing(page="login.html", kind="company site")
        r = self.fill(x)
        self.assert_stopped_untouched(x, r, ff.MANUAL_REQUIRED, "log in or create an account")
        self.assertEqual(self.value(x, "input[type=password]"), "")

    def test_changed_layout(self):
        x = self.listing(page="changed_layout.html")
        self.assert_stopped_untouched(x, self.fill(x), ff.MANUAL_REQUIRED,
                                      "unexpected layout for a Greenhouse form: missing first name, last name, email, resume upload")

    def test_unexpected_iframe(self):
        x = self.listing(page="iframe_layout.html")
        self.assert_stopped_untouched(x, self.fill(x), ff.MANUAL_REQUIRED, "embedded frame")

    def test_lever_layout_checked_too(self):
        x = self.listing(page="greenhouse_form.html", kind="Lever")          # a "Lever" listing with a different form
        self.assert_stopped_untouched(x, self.fill(x), ff.MANUAL_REQUIRED, "unexpected layout for a Lever form")


class SubmitTests(Base):
    """submit_approved exists only with allow_approved_submit, and only submits what you approved."""
    def reload(self, cfg):
        with mock.patch("internship_apply.load_config", return_value=cfg):
            importlib.reload(ff)

        def back():                                   # close this test's browser before the module is reset
            for k in list(ff._SESSIONS):
                ff.close_session(k)
            importlib.reload(ff)
        self.addCleanup(back)
        for name, value in (("DATA_DIR", self.tmp.name),):
            setattr(ff, name, value)

    def test_no_submit_function_by_default(self):
        self.reload({**ON, "allow_approved_submit": False})
        self.assertFalse(hasattr(ff, "submit_approved"))

    def test_only_submit_approved_can_click_submit(self):
        src = pathlib.Path(ff.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "submit_approved")
        inside = range(fn.lineno, fn.end_lineno + 1)
        import re
        pattern = re.compile(r"type=submit|btn-submit|\.submit\(\)|requestSubmit|keyboard\.press\(.Enter")
        outside = [i + 1 for i, line in enumerate(src.splitlines()) if pattern.search(line) and i + 1 not in inside]
        self.assertEqual(outside, [], "a submit selector or Enter key outside submit_approved")

    def test_approved_submit(self):
        cfg = {**ON, "allow_approved_submit": True}
        self.reload(cfg)
        x = self.listing()
        r = ff.fill(x.id, config=cfg, db_path=self.db, profile_path=self.pf, output_root=self.out, test_mode=True)
        self.assertEqual(r.status, ff.AWAITING_REVIEW)
        token = ff._create_approval(x.id, self.db)
        self.assertRaises(ff.SubmitRefused, ff.submit_approved, x.id, "not-the-token", self.db)
        # A change after review invalidates the approval...
        self.page(x).locator("#first_name").fill("Someone Else")
        with self.assertRaises(ff.SubmitRefused) as cm:
            ff.submit_approved(x.id, token, self.db)
        self.assertIn("the form changed after your review", str(cm.exception))
        self.page(x).locator("#first_name").fill("Test")                    # ...until it's back exactly as reviewed
        # Disabled while dry_run is on or the kill switch is off (checked at call time).
        for off in ({**cfg, "dry_run": True}, {**cfg, "filling_enabled": False}):
            with mock.patch.object(ff, "load_config", return_value=off):
                self.assertRaises(ff.SubmitRefused, ff.submit_approved, x.id, token, self.db)
        self.assertTrue(self.nothing_submitted(x))
        note = ff.submit_approved(x.id, token, self.db)
        self.assertEqual(note, "submitted after your approval")
        self.assertEqual(self.page(x).evaluate("window.__clicked"), 1)
        self.assertEqual(self.status(x)["status"], ff.SUBMITTED)
        with open(os.path.join(ff.logmod.log_dir(), "applications.jsonl"), encoding="utf-8") as f:
            events = [json.loads(l)["event"] for l in f if json.loads(l)["listing_id"] == x.id]
        self.assertIn("approved_by_you", events)
        self.assertRaises(ff.SubmitRefused, ff.submit_approved, x.id, token, self.db)   # once only

    def test_never_reopens_a_form_to_submit(self):
        cfg = {**ON, "allow_approved_submit": True}
        self.reload(cfg)
        x = self.listing()
        ff.fill(x.id, config=cfg, db_path=self.db, profile_path=self.pf, output_root=self.out, test_mode=True)
        token = ff._create_approval(x.id, self.db)
        ff.close_session(x.id)                                                # the browser was closed
        with mock.patch.object(ff, "_launch", side_effect=AssertionError("must not reopen")):
            with self.assertRaises(ff.SubmitRefused) as cm:
                ff.submit_approved(x.id, token, self.db)
        self.assertIn("no open session", str(cm.exception))

    def test_approval_bound_to_the_saved_record(self):
        cfg = {**ON, "allow_approved_submit": True}
        self.reload(cfg)
        x = self.listing()
        r = ff.fill(x.id, config=cfg, db_path=self.db, profile_path=self.pf, output_root=self.out, test_mode=True)
        with open(r.record_path, "r+", encoding="utf-8") as f:                # someone edits the record on disk
            rec = json.load(f)
            rec["fields"][0]["entered"] = "Edited"
            f.seek(0), f.truncate(), json.dump(rec, f)
        with self.assertRaises(PermissionError):
            ff._create_approval(x.id, self.db)

    def test_approve_command_needs_you_at_a_terminal(self):
        with mock.patch("sys.stdin.isatty", return_value=False), self.assertRaises(SystemExit) as cm:
            ff.approve_cli("anything", self.db)
        self.assertIn("typed by you in a terminal", str(cm.exception))
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), self.assertRaises(SystemExit) as cm:
            ff.approve_cli("anything", self.db)
        self.assertIn("Not from inside Claude Code", str(cm.exception))


class MappingUnitTests(unittest.TestCase):
    def test_choose_option_never_guesses(self):
        self.assertEqual(ff.choose_option(["Yes", "No"], "no"), "No")
        self.assertEqual(ff.choose_option(["Male", "Female", "Decline To Self Identify"], "decline"), "Decline To Self Identify")
        self.assertIsNone(ff.choose_option(["Male", "Female"], "decline"))
        self.assertIsNone(ff.choose_option(["I decline", "Prefer not to say"], "decline"))          # two: ambiguous
        self.assertIsNone(ff.choose_option(["Example University of Technology"], "exact", "Example University"))

    def test_consent_is_never_answered_even_when_it_looks_like_a_profile_question(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "p.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(COMPLETE)
        p = profile.load(path)
        fields = [{"uid": "a", "kind": "radio", "label": "I certify that I am legally authorized to work in this country",
                   "required": True, "options": ["Yes", "No"], "section": ""},
                  {"uid": "b", "kind": "radio", "label": "Are you legally authorized to work in this country?",
                   "required": True, "options": ["Yes", "No"], "section": ""},
                  {"uid": "c", "kind": "select", "label": "Do you agree to our privacy policy?", "required": True,
                   "options": ["Yes", "No"], "section": ""}]
        certify, plain, privacy = ff.map_fields(fields, p, None, "Acme")
        self.assertEqual((certify.action, certify.confident), ("leave", False))
        self.assertIn("consent", certify.note)
        self.assertEqual((plain.action, plain.value), ("choose", "yes"))     # the plain question is answered
        self.assertEqual((privacy.action, privacy.confident), ("leave", False))

    def test_labels_that_are_not_names(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "p.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(COMPLETE)
        p = profile.load(path)
        for label in ("Name Pronunciation | How do you pronounce your name?", "Preferred Name", "Have you graduated?"):
            self.assertEqual(ff._standard(label, p), ("", None), label)


if __name__ == "__main__":
    unittest.main()

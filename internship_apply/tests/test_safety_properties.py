"""The four safety properties, each proved by code inspection AND by running the real thing on fixtures.
Run from the project root:   python -m unittest discover -s internship_apply/tests -t .

  1. Nothing clicks a final submit button, except submit_approved (if it exists). That function needs a token
     bound to the reviewed record's hash, is off under dry_run or the kill switch, and no Jarvis tool reaches it.
     (Its token / hash / dry-run / same-session behaviour is also exercised in test_form_filler.SubmitTests, and
     its unreachability from every tool in test_tools.SubmitUnreachableTests.)
  2. Nothing creates an account or enters a password.
  3. Nothing solves or bypasses a CAPTCHA.
  4. No secrets or personal data in logs or in files that would be committed."""
import ast
import fnmatch
import importlib
import json
import os
import pathlib
import re
import tempfile
import unittest
from unittest import mock

from internship_apply import DEFAULTS, form_filler as ff, listings, materials, profile, safety, tools
from internship_apply.listings import Store, make_listing
from internship_apply.tests.test_profile import COMPLETE

PKG = pathlib.Path(ff.__file__).resolve().parent
ROOT = PKG.parent
FORMS = PKG / "tests" / "fixtures" / "forms"
CODE = [p for p in PKG.glob("*.py")]
ON = {**DEFAULTS, "filling_enabled": True, "dry_run": False, "headless_for_tests": True}


def functions_containing(path, line_no):
    """Names of the functions (innermost last) that contain a line."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.lineno <= line_no <= n.end_lineno]


def calls(path, attr):
    """(line, receiver source, args source) for every call to .<attr>(...) in a file."""
    src = path.read_text(encoding="utf-8")
    out = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr:
            out.append((n.lineno, ast.get_source_segment(src, n.func.value), [ast.get_source_segment(src, a) for a in n.args]))
    return out


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: [ff.close_session(k) for k in list(ff._SESSIONS)])
        d = self.tmp.name
        self.db, self.out, self.pf = (os.path.join(d, n) for n in ("l.db", "out", "p.yaml"))
        with open(self.pf, "w", encoding="utf-8") as f:
            f.write(COMPLETE)
        dd = mock.patch.object(ff, "DATA_DIR", d)
        dd.start()
        self.addCleanup(dd.stop)

    def run_page(self, page, kind="Greenhouse"):
        url = (FORMS / page).as_uri()
        x = make_listing(company="Acme Robotics", title=f"Intern {page}", location="London", remote="unknown", url=url,
                         source="greenhouse", posted_date=None, deadline=None, description="", requirements=[],
                         application_type=kind, apply_url=url)
        with Store(self.db) as s:
            s.save([x])
        materials.export(materials.generate(x, profile.load(self.pf)), x, root=self.out)
        r = ff.fill(x.id, config=ON, db_path=self.db, profile_path=self.pf, output_root=self.out, test_mode=True)
        page_obj = ff.open_session(x.id).page if ff.open_session(x.id) else None
        return r, page_obj


# ---------------------------------------------------------------- 1. submit

class P1_NoSubmitClick(Fixture):
    SUBMITTISH = re.compile(r"type=submit|btn-submit|requestSubmit|\.submit\(\)|keyboard\.press\(\s*['\"]Enter|"
                            r"press\(\s*['\"]Enter|dispatch_event\(\s*['\"]submit", re.I)

    def test_only_submit_approved_mentions_a_submit_control(self):
        for path in CODE:
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if self.SUBMITTISH.search(line):
                    self.assertIn("submit_approved", functions_containing(path, i), f"{path.name}:{i}: {line.strip()}")

    def test_every_click_outside_submit_approved_is_a_dropdown(self):
        clicks = [(path.name, line, recv) for path in CODE for line, recv, _ in calls(path, "click")
                  if "submit_approved" not in functions_containing(path, line)]
        self.assertTrue(clicks)
        for name, line, recv in clicks:
            self.assertEqual(name, "form_filler.py")
            self.assertEqual(functions_containing(PKG / name, line)[-1], "_apply", f"click at {name}:{line}")
            self.assertRegex(recv, r"^(loc\.first|opts\.nth\(.*\))$")      # open a combobox / pick its option
        for path in CODE:                                                 # and no key but Escape is ever pressed
            for line, recv, args in calls(path, "press"):
                self.assertEqual(args, ['"Escape"'], f"{path.name}:{line}")
        for js in (ff.FIELDS_JS, ff.READ_JS, ff.STATE_JS):               # the page scripts only read
            self.assertNotRegex(js, r"\.click\(|\.submit\(|requestSubmit|dispatchEvent")

    def test_filled_fixtures_are_never_submitted(self):
        for page, kind in (("greenhouse_form.html", "Greenhouse"), ("lever_form.html", "Lever"),
                           ("generic_form.html", "company site"), ("injected_labels.html", "Greenhouse")):
            r, pg = self.run_page(page, kind)
            self.assertEqual(r.status, ff.AWAITING_REVIEW, page)
            self.assertEqual(pg.evaluate("[window.__submitted || 0, window.__clicked || 0]"), [0, 0], page)

    def test_submit_approved_only_with_the_flag_and_guards(self):
        self.assertFalse(hasattr(ff, "submit_approved"))                  # your config: no submit function at all
        allow = {**ON, "allow_approved_submit": True}
        with mock.patch("internship_apply.load_config", return_value=allow):
            importlib.reload(ff)
        try:
            src = pathlib.Path(ff.__file__).read_text(encoding="utf-8")
            fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "submit_approved")
            body = ast.get_source_segment(src, fn)
            # The guards, in order, before the one click:
            for guard in ("_submit_allowed(cfg)", "_SESSIONS.get(listing_id)", "gate(cfg, s.page.url)",
                          "_sha(approval_token", "approved_hash != row[\"record_hash\"]", "record_hash(live) != approved_hash"):
                self.assertIn(guard, body)
                self.assertLess(body.index(guard), body.index("buttons[0].click()"))
            self.assertIn('not cfg.get("dry_run", True)', ast.get_source_segment(src, next(
                n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "_submit_allowed")))
        finally:
            for k in list(ff._SESSIONS):
                ff.close_session(k)
            importlib.reload(ff)

    def test_unreachable_from_jarvis(self):
        jarvis_code = (PKG / "tools.py").read_text(encoding="utf-8") + (ROOT / "gtools.py").read_text(encoding="utf-8") + \
            (ROOT / "server.py").read_text(encoding="utf-8")
        names = {n.attr for n in ast.walk(ast.parse(jarvis_code)) if isinstance(n, ast.Attribute)} | \
            {n.id for n in ast.walk(ast.parse(jarvis_code)) if isinstance(n, ast.Name)}
        self.assertFalse(names & {"submit_approved", "approve_cli", "_create_approval", "approve_with_token"})


# ---------------------------------------------------------------- 2. accounts and passwords

class P2_NoAccountsNoPasswords(Fixture):
    def test_no_code_types_into_a_password_field_or_holds_credentials(self):
        for path in CODE:
            src = path.read_text(encoding="utf-8")
            for line, recv, args in calls(path, "fill") + calls(path, "type") + calls(path, "press_sequentially"):
                self.assertNotRegex(" ".join(a or "" for a in args), r"(?i)pass(word)?|secret|credential", f"{path.name}:{line}")
            self.assertNotRegex(src, r"(?i)\b(login|sign_?in|create_account|register)\s*\(")   # no such actions exist
        # The mapper never fills a password field, whatever its label says.
        p = profile.load(self.pf)
        (m,) = ff.map_fields([{"uid": "a", "kind": "password", "label": "Email", "required": True, "options": [],
                               "section": ""}], p, None, "Acme")
        self.assertEqual(m.action, "leave")

    def test_web_requests_carry_no_credentials(self):
        seen = []
        w = listings.Web(DEFAULTS, transport=lambda url, headers: seen.append(headers) or listings.Response(200, url, b"{}"),
                         clock=lambda: 0, sleep=lambda s: None)
        w.robots["https://a.example"] = "all"
        w.get("https://a.example/x")
        self.assertEqual(set(seen[0]), {"User-Agent", "Accept"})

    def test_login_and_signup_pages_stop_untouched(self):
        for page in ("login.html", "signup.html"):
            r, pg = self.run_page(page, "company site")
            self.assertEqual(r.status, ff.MANUAL_REQUIRED, page)
            self.assertIn("log in or create an account", r.reason)
            self.assertEqual(pg.evaluate("[...document.querySelectorAll('input')].map(e => e.value).join('')"), "", page)
            self.assertEqual(pg.evaluate("[window.__submitted || 0, window.__clicked || 0]"), [0, 0], page)


# ---------------------------------------------------------------- 3. CAPTCHAs

class P3_NoCaptchaSolving(Fixture):
    def test_no_code_touches_frames_or_hides_automation(self):
        banned = re.compile(r"frame_locator|content_frame|\.frames\b|\.frame\(|\bsolve\w*\s*\(|solver|2captcha|anticaptcha|capsolver|"
                            r"stealth|AutomationControlled|webdriver|navigator\.webdriver|add_init_script", re.I)
        for path in CODE:
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#")[0]
                if banned.search(code):
                    self.fail(f"{path.name}:{i}: {line.strip()}")
        reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        self.assertNotRegex(reqs, r"captcha|stealth|undetected|solver")
        launch = ast.get_source_segment((PKG / "form_filler.py").read_text(encoding="utf-8"), next(
            n for n in ast.walk(ast.parse((PKG / "form_filler.py").read_text(encoding="utf-8")))
            if isinstance(n, ast.FunctionDef) and n.name == "_launch"))
        self.assertNotIn("args=", launch)                                  # a plain browser, nothing disguised

    def test_captcha_before_or_during_filling_stops_and_leaves_it_to_you(self):
        for page in ("captcha.html", "captcha_late.html"):
            r, pg = self.run_page(page)
            self.assertEqual(r.status, ff.MANUAL_REQUIRED, page)
            self.assertIn("nothing was solved or bypassed", r.reason)
            self.assertTrue(r.browser_open, "left open for you")
            self.assertIsNone(r.record_hash)                               # never reaches review, so never approvable
            self.assertEqual(pg.evaluate("[window.__submitted || 0, window.__clicked || 0]"), [0, 0], page)


# ---------------------------------------------------------------- 4. secrets and personal data

def gitignore_patterns():
    return [l.strip() for l in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]


def ignored(rel):
    """Enough of .gitignore's rules for this repo's patterns."""
    parts = rel.split("/")
    for p in gitignore_patterns():
        if p.endswith("/"):
            d = p.rstrip("/")
            if ("/" in d and (rel + "/").startswith(d + "/")) or ("/" not in d and d in parts[:-1]):
                return True
        elif fnmatch.fnmatch(parts[-1], p) or fnmatch.fnmatch(rel, p):
            return True
    return False


class P4_NoSecretsInLogsOrCommits(unittest.TestCase):
    def committed(self):
        for path in [*PKG.rglob("*"), ROOT / "requirements.txt", ROOT / ".gitignore"]:
            rel = path.relative_to(ROOT).as_posix()
            if path.is_file() and not ignored(rel):
                yield rel, path

    def test_private_folders_are_ignored(self):
        for rel in ("internship_apply/data/profile.yaml", "internship_apply/data/listings.db",
                    "internship_apply/data/manual_urls.txt", "internship_apply/output/x/resume.pdf",
                    "internship_apply/logs/internship_apply.log", "internship_apply/logs/applications.jsonl",
                    "config.json", "secrets/google_token.dpapi", "data/jarvis.log"):
            self.assertTrue(ignored(rel), rel)
        for rel in ("internship_apply/form_filler.py", "internship_apply/README.md", "requirements.txt"):
            self.assertFalse(ignored(rel), rel)

    def test_no_secret_or_real_personal_data_in_committed_files(self):
        secrets_ = set()
        cfg_path = ROOT / "config.json"
        if cfg_path.exists():
            for k, v in json.loads(cfg_path.read_text(encoding="utf-8")).items():
                if isinstance(v, str) and len(v) >= 8 and re.search(r"secret|token|key|password", k, re.I):
                    secrets_.add(v)
        personal = set()
        if os.path.exists(profile.DEFAULT_PATH):                           # your profile, if this copy has one
            real = profile.load(profile.DEFAULT_PATH)                      # (placeholders are None)
            personal = {v for v in (real.personal.email, real.personal.phone) if v}
        files = list(self.committed())
        self.assertTrue(any(rel.endswith("form_filler.py") for rel, _ in files))
        for rel, path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = path.read_bytes().decode("latin-1")
            for s in secrets_ | personal:
                self.assertNotIn(s, text, f"{rel} holds a secret or your personal data")
            self.assertIsNone(safety.KEY_RE.search(text), rel)
            for email in safety.EMAIL_RE.findall(text):                    # only made-up test addresses
                self.assertRegex(email, r"@(?:[\w-]+\.)*example(?:\.com)?$|@x\.com$|@a\.b\.example$|@b\.example$", f"{rel}: {email}")

    def test_logs_are_redacted(self):
        # Exercised for real in test_hardening.LogTests; here the redactor itself, on everything a log could see.
        fake_google = "ya29" + "." + "a0AfH6SMB" + "x" * 24                # built here, so no key-shaped text is committed
        fake_refresh = "1/" + "/0g" + "X" * 20
        for raw in (f"token={fake_google}", "Authorization: Bearer abc.def.ghi", "password = 'hunter2'",
                    "email me@uni.example", "call +44 7700 900123", f"refresh_token: {fake_refresh}", f"key {fake_google}"):
            out = safety.redact(raw)
            self.assertIn("[redacted", out, raw)
            self.assertNotRegex(out, r"ya29\.|hunter2|me@uni|7700 900123|1//0g", raw)
        self.assertEqual(safety.redact("filled at 2026-10-08T20:15:00, score 85"), "filled at 2026-10-08T20:15:00, score 85")


if __name__ == "__main__":
    unittest.main()

"""Tailored materials, above all the fact check. Run from the project root:
    python -m unittest discover -s internship_apply/tests -t .
Claude is always a fake `ask`; files are written to a temp folder, never to output/."""
import json
import os
import tempfile
import unittest
import zipfile

from internship_apply import materials, profile
from internship_apply.listings import make_listing
from internship_apply.materials import FactChecker
from internship_apply.tests.test_profile import COMPLETE, TEMPLATE_PATH


def listing(**kw):
    d = dict(company="Acme Robotics", title="Software Engineering Intern", location="London, UK", remote="unknown",
             url="https://acme.example/jobs/1", source="greenhouse", posted_date="2026-09-01", deadline=None,
             description="Join our 10-week programme building Kafka pipelines.",
             requirements=["Experience with Java or Python", "Familiarity with Kafka"], application_type="Greenhouse")
    d.update(kw)
    return make_listing(**d)


class FakeModel:
    """Replies by system prompt: REPHRASE / LETTER / ANSWERS / CLAIMS."""
    def __init__(self, **replies):
        self.replies, self.calls = replies, []

    def __call__(self, prompt, system_prompt):
        kind = ("REPHRASE" if system_prompt is materials.REPHRASE_SYSTEM else "LETTER" if system_prompt is materials.LETTER_SYSTEM
                else "ANSWERS" if system_prompt is materials.ANSWERS_SYSTEM else "CLAIMS")
        self.calls.append((kind, prompt))
        reply = self.replies.get(kind, RuntimeError(f"no {kind} reply"))
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = self.profile(COMPLETE)
        self.x = listing()
        self.fc = FactChecker(self.p, self.x)

    def profile(self, text):
        path = os.path.join(self.tmp.name, f"p{abs(hash(text))}.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return profile.load(path)

    def flags(self, text, fc=None, **kw):
        return [(f.claim, f.reason) for f in (fc or self.fc).check(text, "test", **kw)]


class FactCheckRuleTests(Base):
    def test_supported_claims_pass(self):
        text = ("I ran weekly labs for 40 students as a Teaching Assistant at Example University. I am studying "
                "for a BSc in Computer Science, expecting to graduate in 2028, with a GPA of 3.8. I built Tiny Compiler "
                "in Rust with two classmates, and I use Python and Git.")
        self.assertEqual(self.flags(text), [])

    def test_invented_numbers_and_metrics(self):
        out = self.flags("I ran labs for 400 students and cut grading time by 35%.")
        self.assertIn(("400", "number or date not in your profile"), out)
        self.assertIn(("35%", "number or date not in your profile"), out)

    def test_invented_dates(self):
        self.assertIn(("2019", "number or date not in your profile"), self.flags("I have been coding since 2019."))

    def test_invented_skills(self):
        out = self.flags("I built backend services in Java and Kafka.")
        self.assertIn(("java", "skill not in your profile"), out)
        self.assertIn(("Kafka", "name not in your profile"), out)          # not a known tech term: caught as a name

    def test_invented_employer(self):
        self.assertIn(("Google", "name not in your profile"), self.flags("Last summer I interned at Google."))

    def test_invented_achievements_and_degrees(self):
        out = self.flags("I won a hackathon and was certified in cloud security while studying for my Master's.")
        reasons = {r for _, r in out}
        self.assertIn(("won", "achievement not in your profile"), out)
        self.assertIn(("hackathon", "achievement not in your profile"), out)
        self.assertIn("degree not in your profile", reasons)
        self.assertTrue(any(c.lower().startswith("certified") for c, _ in out))

    def test_company_facts_allowed_only_in_sentences_about_them(self):
        self.assertEqual(self.flags("I would love to help your team grow its Kafka pipelines at Acme Robotics."), [])
        self.assertEqual(self.flags("Your 10-week programme suits me."), [])
        self.assertIn(("Kafka", "name not in your profile"), self.flags("I have built Kafka pipelines."))
        self.assertIn(("10", "number or date not in your profile"), self.flags("I worked on it for 10 weeks."))
        self.assertIn(("java", "skill not in your profile"), self.flags("I have Java experience."))

    def test_possessives_and_greeting(self):
        self.assertEqual(self.flags("Dear Hiring Manager,\n\nI admire Acme Robotics's work.\n\nSincerely,\nTest Person"), [])

    def test_unknown_profile_facts_support_nothing(self):
        no_gpa = self.profile(COMPLETE.replace("gpa: 3.8", 'gpa: "TODO: your GPA"'))
        out = self.flags("I graduate with a 3.8 GPA.", fc=FactChecker(no_gpa, self.x))
        self.assertIn(("3.8", "number or date not in your profile"), out)
        self.assertNotIn("TODO", materials.profile_corpus(no_gpa) + materials.labelled_corpus(no_gpa))

    def test_rephrased_bullet_keeps_only_its_own_numbers_and_rank(self):
        fc = FactChecker(self.p)
        original = "Ran weekly labs for 40 students"
        self.assertEqual(self.flags("Ran weekly lab sessions for 40 students", fc=fc, numbers_from=original), [])
        # 3.8 is in the profile, but not in this bullet: a rephrasing can't move facts between items.
        self.assertIn(("3.8", "number or date not in your profile"),
                      self.flags("Ran weekly labs for 40 students, rated 3.8 by them", fc=fc, numbers_from=original))
        self.assertIn(("Led", "makes your role sound more senior than your original"),
                      self.flags("Led weekly labs for 40 students", fc=fc, numbers_from=original))
        self.assertIn(("Acme", "name not in your profile"),
                      self.flags("Ran weekly labs for 40 students using Acme tools", fc=FactChecker(self.p, self.x),
                                 numbers_from=original))                 # even the listing's names

    def test_word_limit(self):
        m = materials.generate(self.x, self.p)
        m.cover_letter = "word " * 320
        self.assertTrue(any(f.reason == "over the 300-word limit" for f in materials.check_materials(m, self.p, self.x)))


class ClaimCheckTests(Base):
    def run_check(self, claims):
        return materials.claude_claim_check({"cover letter": "anything"}, self.p, FakeModel(CLAIMS={"claims": claims}))

    def test_real_evidence_passes(self):
        self.assertEqual(self.run_check([
            {"where": "cover letter", "claim": "I ran labs for 40 students", "evidence": "Ran weekly labs for 40 students"},
            {"where": "cover letter", "claim": "a GPA of 3.8", "evidence": "education.gpa: 3.8"}]), [])

    def test_invented_missing_or_weak_evidence_is_flagged(self):
        flags = self.run_check([
            {"where": "cover letter", "claim": "I mentored new TAs", "evidence": "Mentored three new teaching assistants"},
            {"where": "cover letter", "claim": "I debugged each stage", "evidence": None},
            {"where": "cover letter", "claim": "I'm detail-oriented", "evidence": "a"},
            {"where": "cover letter", "claim": "my GPA", "evidence": "(not provided)"},
            {"where": "made-up label", "claim": "something", "evidence": "nope"}])
        self.assertEqual([f.claim for f in flags],
                         ["I mentored new TAs", "I debugged each stage", "I'm detail-oriented", "my GPA", "something"])
        self.assertTrue(all(f.reason == "not supported by your profile (claim check)" for f in flags))
        self.assertEqual(flags[-1].where, "materials")                    # unknown labels aren't trusted either

    def test_prompt_gets_only_known_facts(self):
        model = FakeModel(CLAIMS={"claims": []})
        materials.claude_claim_check({"cover letter": "Hello"}, self.p, model)
        prompt = model.calls[0][1]
        self.assertIn("experience[0].bullets[0]: Ran weekly labs for 40 students", prompt)
        self.assertNotIn("TODO", prompt)


class GenerateTests(Base):
    def test_template_letter_needs_no_model_and_passes_its_own_check(self):
        m = materials.generate(self.x, self.p)
        self.assertEqual(m.flags, [])
        self.assertIn("Software Engineering Intern position at Acme Robotics", m.cover_letter)
        self.assertLess(len(m.cover_letter.split()), 300)
        self.assertIsNone(m.answers["why_this_company"])                  # TODO in the library: not drafted
        self.assertEqual(m.answers["teamwork"], "I paired with two classmates on the compiler.")
        self.assertTrue(any("template" in n for n in m.notes))

    def test_refuses_an_unfilled_profile(self):
        with self.assertRaises(profile.ProfileError):
            materials.generate(self.x, profile.load(TEMPLATE_PATH))

    def test_with_model_bad_rephrasing_reverted_and_claims_flagged(self):
        model = FakeModel(
            REPHRASE={"1": "Led weekly labs for 400 students"},
            LETTER={"body": "I am applying for the Software Engineering Intern role at Acme Robotics. I ran weekly labs "
                            "for 40 students. I also interned at Google."},
            ANSWERS={"teamwork": "At Acme Robotics I'd bring what I learned pairing with two classmates on the compiler."},
            CLAIMS={"claims": [{"where": "cover letter", "claim": "I ran weekly labs for 40 students",
                                "evidence": "Ran weekly labs for 40 students"},
                               {"where": "cover letter", "claim": "I also interned at Google", "evidence": None}]})
        m = materials.generate(self.x, self.p, ask=model)
        self.assertEqual(m.resume.experience[0].bullets[0].text, "Ran weekly labs for 40 students")    # reverted
        reverted = [f for f in m.flags if f.action == "reverted to your original wording"]
        self.assertEqual({f.claim for f in reverted}, {"Led", "400"})
        letter = {(f.claim, f.reason) for f in m.flags if f.where == "cover letter"}
        self.assertIn(("Google", "name not in your profile"), letter)
        self.assertIn(("I also interned at Google", "not supported by your profile (claim check)"), letter)
        self.assertIn("pairing with two classmates", m.answers["teamwork"])
        self.assertEqual([k for k, _ in model.calls], ["REPHRASE", "LETTER", "ANSWERS", "CLAIMS"])

    def test_good_rephrasing_kept(self):
        model = FakeModel(REPHRASE={"1": "Ran weekly lab sessions for 40 students"}, LETTER={"body": "Fine letter."},
                          ANSWERS={}, CLAIMS={"claims": []})
        m = materials.generate(self.x, self.p, ask=model)
        self.assertEqual(m.resume.experience[0].bullets[0].text, "Ran weekly lab sessions for 40 students")
        self.assertEqual(m.flags, [])

    def test_model_failures_fall_back_safely(self):
        m = materials.generate(self.x, self.p, ask=FakeModel())            # every call raises
        self.assertIn("Software Engineering Intern position at Acme Robotics", m.cover_letter)
        self.assertEqual(m.resume.experience[0].bullets[0].text, "Ran weekly labs for 40 students")
        self.assertTrue(any("claim check unavailable" in n for n in m.notes))

    def test_resume_puts_matching_skills_first(self):
        p = self.profile(COMPLETE.replace("languages: [Python, Rust]", "languages: [Rust, Python]"))
        r = materials.build_resume(p, self.x)                             # the listing asks for Python, not Rust
        self.assertEqual(dict(r.skills)["Languages"], ["Python", "Rust"])


class ExportTests(Base):
    def test_files_and_report_side_by_side(self):
        import datetime
        model = FakeModel(REPHRASE={}, LETTER={"body": "I ran weekly labs for 40 students. I interned at Google."},
                          ANSWERS={}, CLAIMS={"claims": []})
        m = materials.generate(self.x, self.p, ask=model)
        day = datetime.date(2026, 10, 8)
        folder = materials.export(m, self.x, root=self.tmp.name, today=day)
        self.assertEqual(os.path.basename(folder), "Acme-Robotics_Software-Engineering-Intern_2026-10-08")
        self.assertEqual(sorted(os.listdir(folder)), ["answers.md", "cover_letter.docx", "cover_letter.pdf",
                                                      "fact_check.json", "fact_check.md", "materials.json", "resume.docx",
                                                      "resume.pdf"])
        for name in ("resume.pdf", "cover_letter.pdf"):
            with open(os.path.join(folder, name), "rb") as f:
                self.assertEqual(f.read(5), b"%PDF-")
        for name in ("resume.docx", "cover_letter.docx"):
            with zipfile.ZipFile(os.path.join(folder, name)) as z:
                self.assertIn("ran weekly labs", z.read("word/document.xml").decode().lower())
        with open(os.path.join(folder, "fact_check.md"), encoding="utf-8") as f:
            report = f.read()
        self.assertIn("2 claim(s) not supported by profile.yaml", report)
        self.assertIn("| cover letter | Google | employer or organisation not in your profile |", report)
        self.assertIn("| cover letter | Google | name not in your profile |", report)
        with open(os.path.join(folder, "fact_check.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["flags"][0]["claim"], "Google")
        again = materials.export(m, self.x, root=self.tmp.name, today=day)  # same day: a new folder, nothing overwritten
        self.assertTrue(again.endswith("_2"))


if __name__ == "__main__":
    unittest.main()

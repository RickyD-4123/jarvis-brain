"""Loading and validating the applicant profile. Run from the project root:
    python -m unittest discover -s internship_apply/tests -t .
Uses made-up fixtures in a temp folder; never reads or changes your real profile.yaml. Also checks
the shipped template (profile.example.yaml) still loads."""
import os
import tempfile
import textwrap
import unittest

from internship_apply import profile

# The blank template as shipped (every value a TODO). data/profile.yaml is your own (git-ignored) copy.
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "profile.example.yaml")

COMPLETE = """
personal:
  name: Test Person
  email: test.person@example.com
  phone: "+1 555 0100"
  city: Testville
  linkedin: https://www.linkedin.com/in/test-person
  github: https://github.com/test-person
  portfolio: "TODO: none yet"
education:
  university: Example University
  degree: BSc
  major: Computer Science
  expected_graduation: 2028-06
  gpa: 3.8
  relevant_coursework: [Algorithms, Databases, "TODO: another"]
experience:
  - role: Teaching Assistant
    organisation: Example University
    dates: "TODO: dates"
    bullets: [Ran weekly labs for 40 students]
  - role: "TODO: job title"
    organisation: "TODO: company"
    dates: "TODO: dates"
    bullets: ["TODO: achievement"]
projects:
  - name: Tiny Compiler
    tech_stack: [Rust]
    description: A compiler for a toy language.
    link: https://github.com/test-person/tiny
skills:
  languages: [Python, Rust]
  frameworks: []
  tools: [Git]
work_authorisation:
  countries: [Testland]
  authorised_to_work: yes
  requires_sponsorship_now: "no"
  requires_sponsorship_future: No
  notes: "TODO: notes"
availability:
  start_date: 2027-06-01
  term_length: 12 weeks
answers:
  why_this_company: "TODO: later"
  teamwork: I paired with two classmates on the compiler.
"""


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def write(self, text):
        path = os.path.join(self.dir.name, "profile.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(text))
        return path

    def problems(self, text, **kw):
        with self.assertRaises(profile.ProfileError) as cm:
            profile.load(self.write(text), **kw)
        return "\n".join(cm.exception.problems)

    # ---- the shipped template

    def test_template_loads_with_everything_unknown(self):
        p = profile.load(TEMPLATE_PATH)
        self.assertEqual(p.missing_required(), profile.REQUIRED)
        self.assertIsNone(p.personal.name)
        self.assertEqual(p.experience, [])             # example items are dropped, not kept as facts
        self.assertEqual(p.projects, [])
        self.assertEqual(p.skills.languages, [])
        self.assertIn("personal.email", p.unfilled)

    def test_template_text_never_contains_placeholders(self):
        text = profile.load(TEMPLATE_PATH).as_text()
        self.assertNotIn("TODO", text)
        self.assertIn(profile.UNKNOWN, text)
        self.assertIn("never guess", text)

    def test_template_refused_when_complete_profile_needed(self):
        with self.assertRaises(profile.ProfileError) as cm:
            profile.load(TEMPLATE_PATH, require_complete=True)
        self.assertIn("personal.name: still a placeholder", str(cm.exception))

    # ---- a filled-in profile

    def test_complete_profile(self):
        p = profile.load(self.write(COMPLETE), require_complete=True)
        self.assertEqual(p.personal.name, "Test Person")
        self.assertIsNone(p.personal.portfolio)        # TODO stays unknown
        self.assertEqual(p.education.gpa, "3.8")       # YAML number -> text
        self.assertEqual(p.education.expected_graduation, "2028-06")
        self.assertEqual(p.availability.start_date, "2027-06-01")   # YAML date -> text
        self.assertEqual(p.education.relevant_coursework, ["Algorithms", "Databases"])
        self.assertEqual(len(p.experience), 1)         # the all-TODO example is dropped
        self.assertIsNone(p.experience[0].dates)       # a partly filled one keeps its unknowns
        self.assertEqual(p.projects[0].tech_stack, ["Rust"])
        self.assertIs(p.work_authorisation.authorised_to_work, True)       # unquoted yes
        self.assertIs(p.work_authorisation.requires_sponsorship_now, False)  # quoted "no"
        self.assertIs(p.work_authorisation.requires_sponsorship_future, False)
        self.assertEqual(p.missing_required(), [])
        self.assertIsNone(p.answers["why_this_company"])

    def test_as_text(self):
        p = profile.load(self.write(COMPLETE))
        text = p.as_text()
        self.assertNotIn("TODO", text)
        self.assertIn("Name: Test Person", text)
        self.assertIn("Teaching Assistant, Example University ((not provided))", text)
        self.assertIn("Needs sponsorship now: no", text)
        self.assertIn("Why this company: (not provided)", text)
        self.assertNotIn("test.person@example.com", p.as_text(include_contact=False))

    # ---- validation

    def test_missing_file(self):
        with self.assertRaises(profile.ProfileError) as cm:
            profile.load(os.path.join(self.dir.name, "nope.yaml"))
        self.assertIn("file not found", str(cm.exception))

    def test_bad_yaml_gives_line(self):
        out = self.problems("personal:\n  name: [unclosed\n")
        self.assertIn("not valid YAML at line", out)

    def test_not_a_mapping(self):
        self.assertIn("set of sections", self.problems("- just\n- a list\n"))

    def test_deleted_required_field(self):
        out = self.problems(COMPLETE.replace("  email: test.person@example.com\n", ""))
        self.assertIn("personal.email: missing (required field)", out)

    def test_empty_required_field(self):
        self.assertIn("education.major: missing", self.problems(COMPLETE.replace("major: Computer Science", "major: ")))

    def test_deleted_section(self):
        start = COMPLETE.index("availability:")
        end = COMPLETE.index("answers:")
        self.assertIn("availability: missing section", self.problems(COMPLETE[:start] + COMPLETE[end:]))

    def test_misspelt_section_and_field(self):
        out = self.problems(COMPLETE.replace("projects:", "projetcs:").replace("  github:", "  githbu:"))
        self.assertIn("projetcs: unknown section (did you mean projects?)", out)
        self.assertIn("personal.githbu: unknown field (did you mean github?)", out)

    def test_bad_email_link_and_yes_no(self):
        out = self.problems(COMPLETE.replace("test.person@example.com", "not-an-email")
                            .replace("https://github.com/test-person\n", "github.com/test-person\n")
                            .replace('requires_sponsorship_now: "no"', "requires_sponsorship_now: maybe"))
        self.assertIn("personal.email: 'not-an-email' doesn't look like an email address", out)
        self.assertIn("personal.github: 'github.com/test-person' should be a full link", out)
        self.assertIn("work_authorisation.requires_sponsorship_now: should be yes or no", out)

    def test_wrong_types(self):
        out = self.problems(COMPLETE.replace("relevant_coursework: [Algorithms, Databases, \"TODO: another\"]",
                                             "relevant_coursework: Algorithms")
                            .replace("  name: Test Person", "  name: [Test, Person]"))
        self.assertIn("education.relevant_coursework: should be a list", out)
        self.assertIn("personal.name: should be text", out)

    def test_experience_item_missing_required_key(self):
        out = self.problems(COMPLETE.replace("    organisation: Example University\n", "", 1))
        self.assertIn("experience[0].organisation: missing (required field)", out)

    def test_all_problems_reported_at_once(self):
        with self.assertRaises(profile.ProfileError) as cm:
            profile.load(self.write(COMPLETE.replace("test.person@example.com", "bad")
                                    .replace("major: Computer Science", "major: ")))
        self.assertEqual(len(cm.exception.problems), 2)
        self.assertIn("education.major", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

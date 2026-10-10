"""Your profile: the facts an application needs (education, skills, experience, links), read
from internship_apply/data/profile.yaml, which you keep by hand (start from profile.example.yaml). Never invented.

Any value starting with "TODO:" is a placeholder and is loaded as unknown (None, or left out of a
list), so nothing downstream can mistake it for a fact. load() rejects a file that is broken
(bad YAML, a section or required field deleted, wrong types, a misspelt key, a malformed email or
link) and lists every problem at once. A required field still on its placeholder is allowed until
something needs it: load(require_complete=True) refuses those too, and missing_required() lists them.

    p = load()
    p.missing_required()        # ["personal.name", ...] while placeholders remain
    p.as_text()                 # plain text for prompts; unknowns say so instead of guessing
"""
import dataclasses
import datetime
import difflib
import os
import re
from dataclasses import dataclass, field

import yaml

DEFAULT_PATH = os.path.join(os.environ["JARVIS_HOME"], "internship_apply", "data", "profile.yaml")     if os.environ.get("JARVIS_HOME") else os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "profile.yaml")
PLACEHOLDER_RE = re.compile(r"^\s*TODO\b", re.I)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
UNKNOWN = "(not provided)"

# Filled in before any material is drafted (load(require_complete=True)).
REQUIRED = ["personal.name", "personal.email",
            "education.university", "education.degree", "education.major", "education.expected_graduation",
            "work_authorisation.authorised_to_work", "work_authorisation.requires_sponsorship_now",
            "work_authorisation.requires_sponsorship_future",
            "availability.start_date", "availability.term_length"]


class ProfileError(Exception):
    """The profile file can't be used. .problems lists every reason, one per line in the message."""
    def __init__(self, path, problems):
        self.path, self.problems = path, list(problems)
        super().__init__(f"Problems in {path}:\n" + "\n".join("  - " + p for p in self.problems))


@dataclass
class Personal:
    name: str | None
    email: str | None
    phone: str | None = None
    city: str | None = None
    linkedin: str | None = None
    github: str | None = None
    portfolio: str | None = None


@dataclass
class Education:
    university: str | None
    degree: str | None
    major: str | None
    expected_graduation: str | None
    gpa: str | None = None
    relevant_coursework: list[str] = field(default_factory=list)


@dataclass
class Experience:
    role: str | None
    organisation: str | None
    dates: str | None = None
    bullets: list[str] = field(default_factory=list)


@dataclass
class Project:
    name: str | None
    description: str | None
    tech_stack: list[str] = field(default_factory=list)
    link: str | None = None


@dataclass
class Skills:
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)


@dataclass
class WorkAuthorisation:
    authorised_to_work: bool | None
    requires_sponsorship_now: bool | None
    requires_sponsorship_future: bool | None
    countries: list[str] = field(default_factory=list)
    notes: str | None = None


@dataclass
class Availability:
    start_date: str | None
    term_length: str | None


@dataclass
class Profile:
    personal: Personal
    education: Education
    experience: list[Experience]
    projects: list[Project]
    skills: Skills
    work_authorisation: WorkAuthorisation
    availability: Availability
    answers: dict[str, str | None]
    # Voluntary self-identification: topic -> the option text to choose, or None for "decline to answer".
    # Only the form filler reads it; it's never put in a prompt (as_text leaves it out).
    self_identification: dict[str, str | None] = field(default_factory=dict)
    unfilled: list[str] = field(default_factory=list)     # every placeholder still in the file, by path
    path: str = ""

    def get(self, dotted):
        """A value by its path, e.g. "personal.email" (None while it's a placeholder)."""
        obj = self
        for part in dotted.split("."):
            obj = obj.get(part) if isinstance(obj, dict) else getattr(obj, part)
        return obj

    def missing_required(self):
        return [p for p in REQUIRED if self.get(p) is None]

    def as_text(self, include_contact=True):
        """The profile as plain text for a prompt. Unknowns are written as UNKNOWN, never filled in."""
        v = lambda x: UNKNOWN if x is None else x
        yn = lambda b: UNKNOWN if b is None else ("yes" if b else "no")
        items = lambda xs: ", ".join(xs) if xs else UNKNOWN
        p, e, w, a = self.personal, self.education, self.work_authorisation, self.availability
        out = [f"APPLICANT PROFILE (from the applicant's own file. \"{UNKNOWN}\" means unknown: "
               "never guess, assume or invent it.)", "", f"Name: {v(p.name)}"]
        if include_contact:
            out += [f"Email: {v(p.email)}", f"Phone: {v(p.phone)}"]
        out += [f"City: {v(p.city)}", f"LinkedIn: {v(p.linkedin)}", f"GitHub: {v(p.github)}",
                f"Portfolio: {v(p.portfolio)}",
                "", "EDUCATION", f"{v(e.degree)} in {v(e.major)}, {v(e.university)}",
                f"Expected graduation: {v(e.expected_graduation)}", f"GPA / grade: {v(e.gpa)}",
                f"Relevant coursework: {items(e.relevant_coursework)}",
                "", "EXPERIENCE"]
        for x in self.experience:
            out.append(f"- {v(x.role)}, {v(x.organisation)} ({v(x.dates)})")
            out += [f"    * {b}" for b in x.bullets]
        if not self.experience:
            out.append(UNKNOWN)
        out += ["", "PROJECTS"]
        for x in self.projects:
            out.append(f"- {v(x.name)} [{items(x.tech_stack)}]: {v(x.description)}" + (f" ({x.link})" if x.link else ""))
        if not self.projects:
            out.append(UNKNOWN)
        s = self.skills
        out += ["", "SKILLS", f"Languages: {items(s.languages)}", f"Frameworks: {items(s.frameworks)}",
                f"Tools: {items(s.tools)}",
                "", "WORK AUTHORISATION", f"Countries: {items(w.countries)}",
                f"Authorised to work there: {yn(w.authorised_to_work)}",
                f"Needs sponsorship now: {yn(w.requires_sponsorship_now)}",
                f"Will need sponsorship in future: {yn(w.requires_sponsorship_future)}",
                f"Notes: {v(w.notes)}",
                "", "AVAILABILITY", f"Start date: {v(a.start_date)}", f"Term length: {v(a.term_length)}",
                "", "SHORT ANSWERS (the applicant's own words)"]
        out += [f"{k.replace('_', ' ').capitalize()}: {v(t)}" for k, t in self.answers.items()] or [UNKNOWN]
        return "\n".join(out)


# ---------------------------------------------------------------- reading the file

class _Reader:
    """Turns the raw YAML into the dataclasses, collecting every problem instead of stopping at one."""
    def __init__(self):
        self.problems, self.unfilled = [], []

    def text(self, raw, path, required=False, kind="text"):
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if required:
                self.problems.append(f"{path}: missing (required field)")
            return None
        if isinstance(raw, bool) or isinstance(raw, (dict, list)):
            self.problems.append(f"{path}: should be {kind}, got {type(raw).__name__} {raw!r}")
            return None
        if isinstance(raw, (datetime.date, datetime.datetime)):
            raw = raw.isoformat()                         # YAML turns 2027-06-01 into a date
        raw = str(raw).strip()
        if PLACEHOLDER_RE.match(raw):
            self.unfilled.append(path)
            return None
        if kind == "email" and not EMAIL_RE.match(raw):
            self.problems.append(f"{path}: {raw!r} doesn't look like an email address")
        if kind == "link" and not re.match(r"^https?://\S+$", raw):
            self.problems.append(f"{path}: {raw!r} should be a full link starting with https://")
        return raw

    def yes_no(self, raw, path):
        if isinstance(raw, bool):                         # YAML reads unquoted yes/no as true/false
            return raw
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            self.problems.append(f"{path}: missing (required field, yes or no)")
            return None
        s = str(raw).strip()
        if PLACEHOLDER_RE.match(s):
            self.unfilled.append(path)
            return None
        if s.lower() in ("yes", "y", "true"):
            return True
        if s.lower() in ("no", "n", "false"):
            return False
        self.problems.append(f"{path}: should be yes or no, got {s!r}")
        return None

    def texts(self, raw, path):
        if raw is None:
            return []
        if not isinstance(raw, list):
            self.problems.append(f"{path}: should be a list (one item per line starting with \"- \", or [] for none)")
            return []
        return [t for i, x in enumerate(raw) if (t := self.text(x, f"{path}[{i}]")) is not None]

    def section(self, data, name, fields, kind=dict, any_keys=False):
        """data[name] after checking it's there, is the right shape, and has no misspelt keys."""
        if name not in data:
            self.problems.append(f"{name}: missing section")
            return kind()
        raw = data[name]
        if raw is None:
            return kind()
        if not isinstance(raw, kind):
            self.problems.append(f"{name}: should be a {'list' if kind is list else 'set of fields'}")
            return kind()
        if kind is dict and not any_keys:
            self.unknown_keys(raw, fields, name)
        return raw

    def entry(self, raw, path, fields):
        if not isinstance(raw, dict):
            self.problems.append(f"{path}: each item should be a set of fields ({', '.join(fields)})")
            return None
        self.unknown_keys(raw, fields, path)
        return raw

    def unknown_keys(self, raw, fields, path):
        for k in raw:
            if k not in fields:
                near = difflib.get_close_matches(str(k), fields, n=1)
                self.problems.append(f"{path}.{k}: unknown field" + (f" (did you mean {near[0]}?)" if near else "")
                                     + f"; expected: {', '.join(fields)}")


def _names(cls):
    return [f.name for f in dataclasses.fields(cls)]


SECTIONS = ["personal", "education", "experience", "projects", "skills", "work_authorisation", "availability", "answers",
            "self_identification"]
OPTIONAL_SECTIONS = {"self_identification"}
SELF_ID_TOPICS = ["gender", "race_ethnicity", "veteran_status", "disability", "sexual_orientation", "transgender", "pronouns"]
DECLINE_WORDS = {"decline", "declined", "prefer not to say", "prefer not to answer", "no answer", ""}


def load(path=DEFAULT_PATH, require_complete=False):
    """Read and validate the profile. Raises ProfileError listing every problem.
    require_complete: also refuse while any REQUIRED field is still a placeholder."""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ProfileError(path, ["file not found: copy internship_apply/profile.example.yaml to internship_apply/data/profile.yaml "
                                  "and fill in your own details"]) from None
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ProfileError(path, [f"not valid YAML{where}: {getattr(e, 'problem', None) or e}"]) from None
    if not isinstance(data, dict):
        raise ProfileError(path, ["the file should be a set of sections (personal:, education:, ...)"])

    r = _Reader()
    for k in data:
        if k not in SECTIONS:
            near = difflib.get_close_matches(str(k), SECTIONS, n=1)
            r.problems.append(f"{k}: unknown section" + (f" (did you mean {near[0]}?)" if near else ""))

    def fields_of(raw, cls, path, required=(), kinds=None, lists=()):
        out = {}
        for k in _names(cls):
            if k in lists:
                out[k] = r.texts(raw.get(k), f"{path}.{k}")
            else:
                out[k] = r.text(raw.get(k), f"{path}.{k}", required=k in required, kind=(kinds or {}).get(k, "text"))
        return cls(**out)

    req = lambda sec: [p.split(".", 1)[1] for p in REQUIRED if p.startswith(sec + ".")]
    personal = fields_of(r.section(data, "personal", _names(Personal)), Personal, "personal", req("personal"),
                         {"email": "email", "linkedin": "link", "github": "link", "portfolio": "link"})
    education = fields_of(r.section(data, "education", _names(Education)), Education, "education",
                          req("education"), lists=("relevant_coursework",))

    experience = []
    for i, raw in enumerate(r.section(data, "experience", None, kind=list)):
        if (raw := r.entry(raw, f"experience[{i}]", _names(Experience))) is not None:
            experience.append(fields_of(raw, Experience, f"experience[{i}]", ("role", "organisation"), lists=("bullets",)))
    projects = []
    for i, raw in enumerate(r.section(data, "projects", None, kind=list)):
        if (raw := r.entry(raw, f"projects[{i}]", _names(Project))) is not None:
            projects.append(fields_of(raw, Project, f"projects[{i}]", ("name", "description"), {"link": "link"},
                                      lists=("tech_stack",)))
    # A template item left entirely on placeholders is an example, not a real entry.
    experience = [x for x in experience if x.role or x.organisation or x.dates or x.bullets]
    projects = [x for x in projects if x.name or x.description or x.tech_stack or x.link]

    skills = fields_of(r.section(data, "skills", _names(Skills)), Skills, "skills",
                       lists=("languages", "frameworks", "tools"))

    w = r.section(data, "work_authorisation", _names(WorkAuthorisation))
    work = WorkAuthorisation(**{k: r.yes_no(w.get(k), f"work_authorisation.{k}") for k in req("work_authorisation")},
                             countries=r.texts(w.get("countries"), "work_authorisation.countries"),
                             notes=r.text(w.get("notes"), "work_authorisation.notes"))
    availability = fields_of(r.section(data, "availability", _names(Availability)), Availability, "availability",
                             req("availability"))
    answers = {str(k): r.text(t, f"answers.{k}") for k, t in r.section(data, "answers", None, any_keys=True).items()}

    # Optional: left out, or "decline", means the form filler picks "decline to answer".
    self_id = {}
    if data.get("self_identification") is not None:
        raw = r.section(data, "self_identification", SELF_ID_TOPICS)
        for k in SELF_ID_TOPICS:
            v = r.text(raw.get(k), f"self_identification.{k}")
            self_id[k] = None if v is None or v.strip().lower() in DECLINE_WORDS else v.strip()

    if r.problems:
        raise ProfileError(path, r.problems)
    profile = Profile(personal, education, experience, projects, skills, work, availability, answers,
                      self_identification=self_id, unfilled=r.unfilled, path=path)
    from internship_apply.safety import profile_secrets
    profile_secrets(profile)                          # your name, email, phone and city are redacted from every log
    if require_complete and (missing := profile.missing_required()):
        raise ProfileError(path, [f"{p}: still a placeholder (required field), fill it in" for p in missing])
    return profile

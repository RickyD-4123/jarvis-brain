"""Form filler: fills one application form in a real browser (Playwright), then stops for your review.

Order of a run (fill(listing_id)):
  1. Refuse, touching nothing, if this listing (or the same company + role) is already
     filled_awaiting_review, submitted or applied, or if max_applications_per_day is reached.
  2. Before touching the listing: a manual_only_domains site, a listing saved as manual-only, a
     mailto: link, or filling_enabled: false (the kill switch) -> status "manual_required" with the
     reason. No browser is opened.
  3. Open the application form in a VISIBLE browser (tests: headless_for_tests), then the hard stops:
       - a login / account / password page            -> "manual_required" (no password is ever typed)
       - a visible CAPTCHA or bot check               -> "stopped"; the browser is left open for you,
                                                         nothing is solved or bypassed
       - a layout that isn't what we expect (missing core fields, an unexpected visible iframe)
                                                      -> "stopped", with what was unexpected; no guessing
  4. Map every field to a value from profile.yaml or the step-5 materials (resume / cover letter PDFs,
     drafted answers). Voluntary self-identification questions get "decline to answer" unless
     profile.yaml's self_identification says otherwise. Consent, citizenship and anything without a
     confident match is left blank and flagged. Nothing is invented.
  5. dry_run: true -> save the mapping (field_mapping.json) and close. Nothing is typed.
     Otherwise fill, then stop on the filled page: full-page screenshot, application_record.json of
     every field and the value now on the page, the fields not filled confidently, and the record's
     sha256, all stored in the applications table. Status "filled_awaiting_review".

There is no code path that clicks a final submit button, with one exception: when allow_approved_submit
is true in config.yaml at import, this module defines submit_approved(listing_id, approval_token). It
needs a token you make by typing `python -m internship_apply.form_filler approve <id>` in a terminal
(refused without a terminal, or inside Claude Code, so it never comes from voice or a model), bound to
the hash of the exact record you reviewed; it re-reads the live form and refuses if anything changed;
it only works on the session still open in this process (never reopens a form); it is disabled while
dry_run is true or filling_enabled is false; and it logs your approval. Otherwise you press Submit
yourself in the open browser.

    python -m internship_apply.form_filler fill <listing id>
    python -m internship_apply.form_filler approve <listing id>     (only with allow_approved_submit)
    python -m internship_apply.form_filler status
"""
import datetime
import glob
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field

from internship_apply import DATA_DIR, load_config, profile as profile_mod
from internship_apply.listings import DB_PATH, Store, domain_matches
from internship_apply.materials import OUTPUT_DIR
from internship_apply import log as logmod

log = logmod.get_logger("form_filler")


APPROVAL_MINUTES = 30

MANUAL_REQUIRED = "manual_required"
DRY_RUN_MAPPED = "dry_run_mapped"
STOPPED = "stopped"
AWAITING_REVIEW = "filled_awaiting_review"
SUBMITTED = "submitted"
APPLIED = "applied"
REFUSED = "refused"                      # returned only: nothing about the listing changes
DONE = (AWAITING_REVIEW, SUBMITTED, APPLIED)


# ---------------------------------------------------------------- the applications table

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    listing_id TEXT PRIMARY KEY, company TEXT, title TEXT,
    status TEXT NOT NULL, reason TEXT, url TEXT,
    record_path TEXT, record_hash TEXT, screenshot_path TEXT,
    unconfident TEXT,                               -- JSON list of field labels not filled confidently
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, filled_at TEXT, submitted_at TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id TEXT NOT NULL,
    record_hash TEXT NOT NULL, token_hash TEXT NOT NULL, created_at TEXT NOT NULL, used_at TEXT
);
"""
COLUMNS = ["listing_id", "company", "title", "status", "reason", "url", "record_path", "record_hash", "screenshot_path",
           "unconfident", "created_at", "updated_at", "filled_at", "submitted_at"]


def _now():
    return datetime.datetime.now().astimezone()


def _norm(s):
    return " ".join(re.sub(r"[^\w]+", " ", (s or "").lower()).split())


class Applications:
    def __init__(self, db_path=DB_PATH):
        self.store = Store(db_path)
        self.db = self.store.db
        self.db.executescript(SCHEMA)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.store.close()

    def get(self, listing_id):
        row = self.db.execute(f"SELECT {', '.join(COLUMNS)} FROM applications WHERE listing_id = ?", (listing_id,)).fetchone()
        return dict(zip(COLUMNS, row)) if row else None

    def all(self):
        return [dict(zip(COLUMNS, r)) for r in self.db.execute(f"SELECT {', '.join(COLUMNS)} FROM applications ORDER BY updated_at")]

    def set(self, listing, status, reason="", **cols):
        now = _now().isoformat(timespec="seconds")
        row = {"listing_id": listing.id, "company": listing.company, "title": listing.title, "status": status,
               "reason": reason, "url": listing.apply_url or listing.url, "updated_at": now, **cols}
        if "unconfident" in row and not isinstance(row["unconfident"], str):
            row["unconfident"] = json.dumps(row["unconfident"])
        keys = list(row)
        with self.db:
            self.db.execute(
                f"INSERT INTO applications ({', '.join(keys)}, created_at) VALUES ({', '.join('?' * len(keys))}, ?) "
                f"ON CONFLICT(listing_id) DO UPDATE SET " + ", ".join(f"{k} = excluded.{k}" for k in keys if k != "listing_id"),
                [row[k] for k in keys] + [now])

    def same_role_done(self, listing):
        """Another listing for the same company and role that's already filled, submitted or applied."""
        for r in self.all():
            if r["status"] in DONE and _norm(r["company"]) == _norm(listing.company) and _norm(r["title"]) == _norm(listing.title):
                return r
        return None

    def filled_on(self, day):
        return sum(1 for r in self.all() if r["status"] in DONE and (r["filled_at"] or "")[:10] == day.isoformat())


def log_event(event, listing_id, **details):
    """The audit trail: one redacted JSON line in internship_apply/logs/applications.jsonl."""
    logmod.event(event, listing_id, **details)
    log.info("%s %s %s", event, listing_id, details.get("reason", ""))


# ---------------------------------------------------------------- reading the page

# Every field gets a data-jarvis-uid; radio/checkbox groups share one, with data-jarvis-opt per option.
FIELDS_JS = r"""
() => {
  const STAR = /[*✱]/g;
  const clean = s => (s || '').replace(STAR, ' ').replace(/\s+/g, ' ').trim();
  const visible = el => { if (!el) return false; const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && +s.opacity !== 0 && r.width > 2 && r.height > 2; };
  const text = el => el ? (el.innerText || el.textContent || '') : '';
  const byIds = ids => (ids || '').split(/\s+/).map(i => document.getElementById(i)).filter(Boolean).map(text).join(' ');
  const questionLabel = el => {
    const q = el.closest('.application-question');
    if (q) { const l = q.querySelector('.application-label'); if (l) return text(l); }
    const g = el.closest('[role=radiogroup][aria-labelledby], [role=group][aria-labelledby]');
    if (g) { const t = byIds(g.getAttribute('aria-labelledby')); if (t.trim()) return t; }
    const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg) return text(lg); }
    return '';
  };
  const labelOf = el => {
    const q = questionLabel(el); if (q.trim() && el.type !== 'radio' && el.type !== 'checkbox') return q;
    const by = byIds(el.getAttribute('aria-labelledby')); if (by.trim()) return by;
    if (el.labels && el.labels.length) { const t = [...el.labels].map(text).join(' '); if (t.trim()) return t; }
    return el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || el.id || '';
  };
  const HEADS = [...document.querySelectorAll('h1, h2, h3, h4, legend')];
  const sectionOf = el => {      // the nearest heading BEFORE the field, in page order
    if (el.closest('.eeoc__container, .eeo-section, [data-qa="eeo-section"]')) return 'Voluntary Self-Identification';
    let best = null;
    for (const h of HEADS) {
      if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) best = h; else break;
    }
    return best ? clean(text(best)) : '';
  };
  const out = [], groups = {};
  let n = 0;
  const els = [...document.querySelectorAll('input, select, textarea')];
  for (const el of els) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) continue;
    if (el.getAttribute('aria-hidden') === 'true') continue;
    const isFile = type === 'file', isChoice = type === 'radio' || type === 'checkbox';
    if (!isFile && !isChoice && !visible(el)) continue;
    if (isChoice && el.name) {
      const key = type + ':' + el.name;
      let g = groups[key];
      if (!g) {
        const raw = questionLabel(el);
        g = groups[key] = { uid: 'f' + (n++), kind: type === 'radio' ? 'radio' : 'checkbox', label: clean(raw), options: [],
          required: false, name: el.name, id: el.id || '', section: sectionOf(el), raw: raw };
        out.push(g);
      }
      el.setAttribute('data-jarvis-uid', g.uid);
      el.setAttribute('data-jarvis-opt', String(g.options.length));
      const own = el.labels && el.labels.length ? text(el.labels[0]) : (el.value || '');
      g.options.push(clean(own) || el.value || '');
      g.required = g.required || el.required || /[*✱]/.test(g.raw) ||
        !!(el.closest('.application-question') && el.closest('.application-question').querySelector('.required'));
      continue;
    }
    const raw = labelOf(el);
    const uid = 'f' + (n++);
    el.setAttribute('data-jarvis-uid', uid);
    let kind = type === 'select' ? 'select' : type === 'textarea' ? 'textarea' : isFile ? 'file' :
      type === 'password' ? 'password' : el.getAttribute('role') === 'combobox' ? 'combobox' :
      ['email', 'tel', 'url', 'number', 'date'].includes(type) ? type : 'text';
    const required = el.required || el.getAttribute('aria-required') === 'true' || /[*✱]/.test(raw) ||
      !!el.closest('[aria-required="true"]') ||
      !!(el.closest('.application-question') && el.closest('.application-question').querySelector('.required'));
    out.push({ uid, kind, label: clean(raw), required, name: el.name || '', id: el.id || '', section: sectionOf(el),
      options: kind === 'select' ? [...el.options].map(o => clean(o.text)) : [],
      autocomplete: el.getAttribute('autocomplete') || '' });
  }
  return out.map(f => { delete f.raw; return f; });
}
"""

READ_JS = r"""
(uids) => uids.map(uid => {
  const els = [...document.querySelectorAll('[data-jarvis-uid="' + uid + '"]')];
  if (!els.length) return null;
  const el = els[0], type = (el.getAttribute('type') || el.tagName).toLowerCase();
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  if (type === 'radio' || type === 'checkbox')
    return els.filter(e => e.checked).map(e => clean(e.labels && e.labels.length ? e.labels[0].innerText : e.value)).join(', ');
  if (type === 'file') return [...el.files].map(f => f.name).join(', ');
  if (type === 'select') return el.selectedIndex > 0 || (el.selectedIndex === 0 && el.value) ? clean(el.options[el.selectedIndex].text) : '';
  if (el.getAttribute('role') === 'combobox') {
    const c = el.closest('.select__control, [class*="control"]') || el.parentElement;
    const v = c && c.querySelector('.select__single-value, [class*="singleValue"], [class*="single-value"]');
    return v ? clean(v.innerText) : '';
  }
  return el.value || '';
})
"""

STATE_JS = r"""
() => {
  const visible = el => { const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && +s.opacity !== 0 && r.width > 2 && r.height > 2; };
  const CAPTCHA = /recaptcha|hcaptcha|challenges\.cloudflare\.com|turnstile|arkoselabs|funcaptcha|geetest|captcha/i;
  const frames = [...document.querySelectorAll('iframe')].map(f => ({ src: f.getAttribute('src') || (f.srcdoc ? 'about:srcdoc' : ''),
    title: f.title || '', visible: visible(f) }));
  const body = (document.body ? document.body.innerText : '').slice(0, 20000);
  return {
    url: location.href,
    captchaVisible: frames.filter(f => f.visible && (CAPTCHA.test(f.src) || CAPTCHA.test(f.title)) && !/size=invisible/.test(f.src)).map(f => f.src || f.title),
    captchaDormant: frames.filter(f => (CAPTCHA.test(f.src) || CAPTCHA.test(f.title)) && (!f.visible || /size=invisible/.test(f.src))).length
      + document.querySelectorAll('.h-captcha, .g-recaptcha, .cf-turnstile').length,
    otherFrames: frames.filter(f => f.visible && !(CAPTCHA.test(f.src) || CAPTCHA.test(f.title))).map(f => f.src || '(no src)'),
    botText: /verify (?:that )?you are (?:a )?human|are you a robot|checking your browser|unusual traffic|press (?:&|and) hold|complete the security check|just a moment\.\.\./i.test(body),
    password: [...document.querySelectorAll('input[type=password]')].some(visible),
    loginText: /sign in to apply|log ?in to apply|create an account|create account to apply|sign up to apply|register to apply|forgot (?:your )?password/i.test(body),
    forms: document.querySelectorAll('form').length,
  };
}
"""

LOGIN_PATH_RE = re.compile(r"/(?:log-?in|sign-?in|signin|sign_in|auth|sso|account|register|signup|sign-?up)\b", re.I)


# ---------------------------------------------------------------- deciding what goes in each field

DEMOGRAPHIC_RE = re.compile(r"gender|\bsex\b|\brace\b|ethnic|hispanic|latin[oax]|veteran|disab|sexual orientation|"
                            r"transgender|pronoun|self[- ]identif|demographic|\beeo|equal employment", re.I)
TOPICS = [("gender", r"gender|\bsex\b"), ("race_ethnicity", r"\brace\b|ethnic|hispanic|latin"), ("veteran_status", r"veteran"),
          ("disability", r"disab"), ("sexual_orientation", r"orientation"), ("transgender", r"transgender"),
          ("pronouns", r"pronoun")]
DECLINE_RE = re.compile(r"decline|prefer not|don.?t wish|do not wish|not to (?:answer|disclose|say|self.?identify|identify)|"
                        r"choose not|rather not|don.?t want to (?:answer|say)|no answer", re.I)
CONSENT_RE = re.compile(r"consent|\bagree|agreement|acknowledg|arbitrat|privacy|terms and|terms of|certif|attest|"
                        r"declaration|\bpolicy\b|true and (?:complete|accurate)", re.I)
CITIZEN_RE = re.compile(r"citizen|nationality|permanent resident|clearance|\bvisa (?:type|status)\b", re.I)


@dataclass
class Mapping:
    uid: str
    label: str
    kind: str
    required: bool
    section: str
    action: str                     # "fill" | "choose" | "upload" | "leave"
    value: str | None = None        # text to type, file to upload, or the option wanted
    rule: str | None = None         # for "choose": "exact" | "yes" | "no" | "decline"
    source: str = ""
    confident: bool = False
    note: str = ""


def _yn(b):
    return None if b is None else ("yes" if b else "no")


def choose_option(options, rule, value=None):
    """The one option that satisfies the rule, or None (never a guess between several)."""
    norm = [(_norm(o), o) for o in options]
    if rule == "decline":
        hits = [o for n, o in norm if DECLINE_RE.search(n)]
    elif rule in ("yes", "no"):
        hits = [o for n, o in norm if n == rule or n.startswith(rule + " ")]
    else:                                         # exact only: "Example University" never picks "... of Technology"
        want = _norm(value)
        hits = [o for n, o in norm if want and n == want]
    return hits[0] if len(hits) == 1 else None


def _standard(label, p):
    """(value, source) for a standard field, ("", None) if this isn't one, or (None, source) if your profile
    doesn't know it. Labels are matched start-anchored or whole-phrase so "Name pronunciation" isn't a name."""
    l = _norm(label)
    per, edu, avail, work = p.personal, p.education, p.availability, p.work_authorisation
    parts = (per.name or "").split()
    two = len(parts) == 2                         # "First Last": anything else isn't split for you
    table = [
        (r"^(?:legal )?first name\b|^given name|^forename", (parts[0] if two else None), "personal.name (first part)"),
        (r"^(?:legal )?(?:last name|surname|family name)\b", (parts[1] if two else None), "personal.name (last part)"),
        (r"^(?:full |legal |your )?name$", per.name, "personal.name"),
        (r"^e ?mail(?: address)?$", per.email, "personal.email"),
        (r"^(?:phone|mobile|telephone|cell)(?: number)?$", per.phone, "personal.phone"),
        (r"^(?:current )?(?:location|city)$|^where are you (?:currently )?(?:based|located)$", per.city, "personal.city"),
        (r"linkedin", per.linkedin, "personal.linkedin"),
        (r"github", per.github, "personal.github"),
        (r"portfolio|personal (?:website|site)|^website(?: url)?$", per.portfolio, "personal.portfolio"),
        (r"^(?:school|university|college|institution)(?: name)?$|which (?:university|school|college)", edu.university,
         "education.university"),
        (r"^degree(?: type| level)?$", edu.degree, "education.degree"),
        (r"^(?:discipline|major|field of study|course of study|subject)$", edu.major, "education.major"),
        (r"^(?:expected |anticipated )?graduation(?: date| year| month)?$|when (?:do|will) you graduate",
         edu.expected_graduation, "education.expected_graduation"),
        (r"\bgpa\b|grade point", edu.gpa, "education.gpa"),
        (r"start date|earliest .*start|available to start|when (?:can|could) you start", avail.start_date,
         "availability.start_date"),
        (r"(?:internship|placement|term) (?:length|duration)|how long (?:can|could|would) you", avail.term_length,
         "availability.term_length"),
        (r"(?:authori[sz]ed|eligible|permitted|right) to work", _yn(work.authorised_to_work),
         "work_authorisation.authorised_to_work"),
    ]
    if "sponsor" in l:
        now, future = work.requires_sponsorship_now, work.requires_sponsorship_future
        if re.search(r"\bnow\b|current", l) and "future" not in l:
            return _yn(now), "work_authorisation.requires_sponsorship_now"
        if True in (now, future):
            return "yes", "work_authorisation.requires_sponsorship_now/future"
        return ("no" if (now, future) == (False, False) else None), "work_authorisation.requires_sponsorship_now/future"
    for pattern, value, source in table:
        if re.search(pattern, l):
            return value, source
    return "", None


def _answer_topic(label, answers, company):
    """The answer-library key a free-text question asks for, if exactly one fits."""
    l = _norm(label)
    c = re.escape(_norm(company)) if company else "(?!)"
    rules = {"why_this_company": rf"\bwhy\b.*(?:compan|\bus\b|join|work (?:here|at|for|with)|interest|apply|this (?:role|position)|{c})"
                                 rf"|what (?:excites|interests|attracts|draws) you",
             "challenge_overcome": r"challeng|obstacle|difficult|setback|overc[oa]me",
             "teamwork": r"team ?work|(?:in|on|with) a team|collaborat",
             "leadership": r"leadership|\blead\b|\bled\b"}
    hits = [k for k in answers if (rules.get(k) and re.search(rules[k], l))
            or (k not in rules and all(w in l for w in k.split("_") if len(w) > 3))]
    return hits[0] if len(hits) == 1 else None


def map_fields(fields, p, mat, company):
    """[Mapping] for every field. mat: the materials dict (answers, cover letter, file paths) or None."""
    out = []
    answers = (mat or {}).get("answers") or {}
    for f in fields:
        m = Mapping(f["uid"], f["label"], f["kind"], bool(f["required"]), f.get("section", ""), "leave")
        label, kind = f["label"], f["kind"]
        context = f"{label} {f.get('section', '')} {f.get('name', '')} {f.get('id', '')}"
        if kind == "password":
            m.note = "password field: never filled"
        elif kind == "file":
            if re.search(r"resume|\bcv\b|curriculum", context, re.I) and not re.search(r"cover", label, re.I):
                m.action, m.value, m.source = "upload", (mat or {}).get("resume_path"), "materials: resume.pdf"
            elif re.search(r"cover", context, re.I):
                m.action, m.value, m.source = "upload", (mat or {}).get("cover_letter_path"), "materials: cover_letter.pdf"
            if m.action == "upload" and not m.value:
                m.action, m.note = "leave", "no materials for this listing yet"
            m.confident = m.action == "upload"
        elif DEMOGRAPHIC_RE.search(context) or f.get("section", "").lower().startswith("voluntary self"):
            topic = next((t for t, rx in TOPICS if re.search(rx, label, re.I)), None)
            wanted = p.self_identification.get(topic) if topic else None
            if kind in ("select", "radio", "combobox"):
                m.action = "choose"
                m.rule, m.value = ("exact", wanted) if wanted else ("decline", None)
                m.source = f"profile: self_identification.{topic}" if wanted else "decline to answer (voluntary)"
                m.confident = True
                if kind != "combobox" and not choose_option(f["options"], m.rule, m.value):
                    m.action, m.confident = "leave", False
                    m.note = "no option matches your answer" if wanted else "no 'decline to answer' option: left blank"
            else:
                m.note = "voluntary question: left blank (declined)"
                m.confident = True
        elif CONSENT_RE.search(label):
            m.note = "consent or agreement: your own decision, never made for you"
        elif kind in ("checkbox",):
            m.note = "tick-boxes are left for you"
        else:
            value, source = _standard(label, p)
            if source is None and kind in ("text", "textarea"):
                if re.search(r"cover letter", label, re.I) and (mat or {}).get("cover_letter"):
                    value, source = mat["cover_letter"], "materials: cover letter"
                elif (topic := _answer_topic(label, answers, company)) is not None:
                    value, source = answers.get(topic), f"materials: answer '{topic}'"
            if source is None and CITIZEN_RE.search(label):
                m.note = "citizenship / clearance isn't in your profile"
            elif source is None:
                m.note = "no confident match in your profile"
            elif value is None:
                m.source = source if source.startswith("materials") else f"profile: {source}"
                m.note = "your profile doesn't say (still TODO)"
            elif kind in ("select", "radio", "combobox"):
                rule = value if value in ("yes", "no") else "exact"
                m.action, m.rule, m.value, m.source, m.confident = "choose", rule, value, f"profile: {source}", True
                if kind != "combobox" and not choose_option(f["options"], rule, value):
                    m.action, m.confident, m.note = "leave", False, f"no option matches '{value}'"
            elif value in ("yes", "no") and source.startswith("work_"):
                m.action, m.value, m.source, m.confident = "fill", value.capitalize(), f"profile: {source}", True
            else:
                m.action, m.value, m.confident = "fill", value, True
                m.source = source if source.startswith("materials") else f"profile: {source}"
        if m.action == "leave":
            # Optional and blank is fine (listed in the record, not flagged); required and blank is flagged.
            m.confident = not m.required and (m.confident or "consent" not in m.note)
            if not m.required and m.note:
                m.note = "optional, left blank: " + m.note
        out.append(m)
    return out


# ---------------------------------------------------------------- the browser

_PW = threading.local()            # one Playwright per thread: a second form can open while the first awaits review


class _SharedPlaywright:
    """The thread's Playwright, stopped when its last browser closes."""
    def __init__(self, pw):
        self.pw, self.browsers = pw, 0

    @property
    def chromium(self):
        return self.pw.chromium

    def stop(self):
        self.browsers -= 1
        if self.browsers <= 0 and getattr(_PW, "shared", None) is self:
            _PW.shared = None
            self.pw.stop()


def _launch(headless):
    """Playwright with your installed Chrome (then Edge, then Playwright's own Chromium). A plain browser:
    no flags, no scripts, nothing that hides that it's automated."""
    shared = getattr(_PW, "shared", None)
    if shared is None:
        from playwright.sync_api import sync_playwright
        shared = _PW.shared = _SharedPlaywright(sync_playwright().start())
    last = None
    for channel in ("chrome", "msedge", None):
        try:
            kw = {"channel": channel} if channel else {}
            browser = shared.chromium.launch(headless=headless, **kw)
            shared.browsers += 1
            return shared, browser
        except Exception as e:                    # that browser isn't installed: try the next
            last = e
    shared.browsers += 1
    shared.stop()
    raise RuntimeError(f"couldn't start a browser: {last}")


@dataclass
class Session:
    listing_id: str
    pw: object
    browser: object
    page: object
    url: str
    mappings: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    record_hash: str | None = None

    def close(self):
        for step in (self.browser.close, self.pw.stop):
            try:
                step()
            except Exception:
                pass


_SESSIONS = {}                    # listing id -> the Session left open for you


def open_session(listing_id):
    return _SESSIONS.get(listing_id)


def close_session(listing_id):
    s = _SESSIONS.pop(listing_id, None)
    if s:
        s.close()


@dataclass
class FillResult:
    listing_id: str
    status: str
    reason: str = ""
    record_path: str | None = None
    screenshot_path: str | None = None
    record_hash: str | None = None
    unconfident: list = field(default_factory=list)
    browser_open: bool = False


def record_hash(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def build_record(session, entered):
    fields = []
    for m, value in zip(session.mappings, entered):
        intended = None if m.action == "leave" else (os.path.basename(m.value) if m.action == "upload" and m.value else m.value)
        fields.append({"label": m.label, "kind": m.kind, "required": m.required, "section": m.section, "action": m.action,
                       "source": m.source, "intended": intended, "entered": value, "confident": m.confident, "note": m.note})
    return {**session.meta, "fields": fields}


def find_materials(listing_id, root=OUTPUT_DIR):
    """The newest materials folder made for this listing, as a dict with absolute file paths, or None."""
    found = []
    for path in glob.glob(os.path.join(root, "*", "materials.json")):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if d.get("listing_id") == listing_id:
            found.append((os.path.getmtime(path), os.path.dirname(path), d))
    if not found:
        return None
    _, folder, d = max(found, key=lambda t: t[0])
    d.update(folder=folder, resume_path=os.path.join(folder, d.get("resume_pdf", "resume.pdf")),
             cover_letter_path=os.path.join(folder, d.get("cover_letter_pdf", "cover_letter.pdf")))
    for k in ("resume_path", "cover_letter_path"):
        if not os.path.exists(d[k]):
            d[k] = None
    return d


EXPECTED = {   # the core of each known form; if any is missing, the layout changed: stop
    "Greenhouse": {"first name": "#first_name", "last name": "#last_name", "email": "#email", "resume upload": "input[type=file]#resume"},
    "Lever": {"application form": "form#application-form", "full name": "input[name=name]", "email": "input[name=email]",
              "resume upload": "input[type=file][name=resume]"},
}


def provider_of(listing, url):
    host = urllib.parse.urlsplit(url).netloc.lower()
    if listing.application_type in EXPECTED:
        return listing.application_type
    if domain_matches(host, ["greenhouse.io"]):
        return "Greenhouse"
    if domain_matches(host, ["lever.co"]):
        return "Lever"
    return "generic"


def gate(cfg, url, listing=None):
    """The kill switch and manual-only sites: None if this URL may be filled now, else the reason.
    Checked at EVERY entry point: start of a run, after the page loads (where it actually landed), before
    typing, before each field, before saving the review, and in the approval and submit paths."""
    host = urllib.parse.urlsplit(url or "").netloc
    if not cfg.get("filling_enabled"):
        return "form filling is switched off (filling_enabled: false in config.yaml)"
    if (url or "").lower().startswith("mailto:"):
        return "this application is by email"
    if domain_matches(host, cfg.get("manual_only_domains") or []):
        return f"{host} is in manual_only_domains: apply by hand"
    if listing is not None and listing.manual_only:
        return f"saved as manual-only: {listing.note or 'apply by hand'}"
    return None


def already_applied(db_path, listing):
    """A reason if you already have this role (same company + role, any posting, any earlier run) at or past
    the review stage, in the form filler's records or the tracker's."""
    from internship_apply.tracker import role_key, APPLIED_STATUSES
    key = role_key(listing.company, listing.title)
    with Applications(db_path) as apps:
        for r in apps.all():
            if r["status"] in DONE and role_key(r["company"], r["title"]) == key:
                return f"already {r['status']}" if r["listing_id"] == listing.id else \
                    f"you already have {listing.company} / {listing.title} as {r['status']}"
        if apps.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tracker'").fetchone():
            for lid, company, role, status in apps.db.execute("SELECT listing_id, company, role, status FROM tracker"):
                if status in APPLIED_STATUSES and role_key(company, role) == key:
                    return f"you already have {listing.company} / {listing.title} as {status} in your tracker"
    return None


GOTO_ATTEMPTS = 3


def _goto(page, url, sleep=time.sleep):
    """Open the form, retrying network failures with exponential backoff (2 s, 4 s). A page that loads but
    looks wrong is NOT retried: that's a layout problem, handled by check_page."""
    delay = 2.0
    for attempt in range(1, GOTO_ATTEMPTS + 1):
        try:
            page.goto(url, wait_until="load", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            return
        except Exception as e:
            if attempt == GOTO_ATTEMPTS:
                raise
            log.info("retrying the form page after %s (attempt %d)", type(e).__name__, attempt)
            sleep(delay)
            delay *= 2


def fill(listing_id, config=None, db_path=DB_PATH, profile_path=profile_mod.DEFAULT_PATH, output_root=OUTPUT_DIR,
         test_mode=False, today=None, test_pages=None):
    """One application run. Returns a FillResult; when result.browser_open, the session is in _SESSIONS
    until close_session(listing_id).
    config: a dict, or a function returning the CURRENT config (default: load_config, re-read at every check,
    so flipping filling_enabled off stops a run in progress). test_pages: {url prefix: local HTML file} served
    instead of the network (tests only)."""
    live = config if callable(config) else (lambda: config) if config is not None else load_config
    cfg = live()
    today = today or datetime.date.today()
    with Store(db_path) as store:
        listing = next((x for x in store.all() if x.id == listing_id), None)
    if listing is None:
        return FillResult(listing_id, REFUSED, f"no listing with id {listing_id}")
    dry = bool(cfg["dry_run"])
    url = listing.apply_url or listing.url

    # 1. Already applied for this role (any posting, any earlier run), or the day's limit reached: refuse,
    #    changing nothing.
    if why := already_applied(db_path, listing):
        log.info("refused %s: %s", listing.id, why)
        return FillResult(listing.id, REFUSED, why)
    with Applications(db_path) as apps:
        if not dry and apps.filled_on(today) >= int(cfg["max_applications_per_day"]):
            return FillResult(listing.id, REFUSED, f"max_applications_per_day ({cfg['max_applications_per_day']}) reached today")

        # 2. Before touching the listing: manual-only sites and the kill switch.
        reason = gate(cfg, url, listing)
        if reason:
            apps.set(listing, MANUAL_REQUIRED, reason)
            log_event(MANUAL_REQUIRED, listing.id, reason=reason)
            return FillResult(listing.id, MANUAL_REQUIRED, reason)

        try:
            p = profile_mod.load(profile_path, require_complete=True)
        except profile_mod.ProfileError as e:
            return FillResult(listing.id, REFUSED, f"profile not ready: {'; '.join(e.problems[:3])}")
        mat = find_materials(listing.id, output_root)
        if not mat and not dry:
            return FillResult(listing.id, REFUSED, f"no materials yet: run python -m internship_apply.materials {listing.id}")

        # 3. Open the form.
        pw, browser = _launch(headless=bool(cfg["headless_for_tests"]) if test_mode else False)
        page = browser.new_page()
        if test_mode:                                 # tests never reach the network
            pages = dict(test_pages or {})

            def offline(route):
                u = route.request.url
                hit = next((path for prefix, path in pages.items() if u.startswith(prefix)), None)
                if hit and hit.startswith("redirect:"):          # a test page that sends the browser elsewhere
                    target = json.dumps(hit[len("redirect:"):])
                    return route.fulfill(status=200, content_type="text/html",
                                         body=f"<script>location.replace({target});</script>")
                if hit:
                    with open(hit, encoding="utf-8") as f:
                        return route.fulfill(status=200, body=f.read(), content_type="text/html; charset=utf-8")
                if u.startswith(("file:", "data:")):
                    return route.continue_()
                return route.fulfill(status=200, body="")
            page.route("**/*", offline)
        s = Session(listing.id, pw, browser, page, url)
        folder = mat["folder"] if mat else os.path.join(DATA_DIR, "applications", listing.id)
        os.makedirs(folder, exist_ok=True)

        def stop(status, why, keep_open):
            apps.set(listing, status, why)
            log_event(status, listing.id, reason=why)
            if keep_open:
                _SESSIONS[listing.id] = s             # left open for you (tests close it themselves)
            else:
                s.close()
            return FillResult(listing.id, status, why, browser_open=keep_open)

        try:
            _goto(page, url, sleep=(lambda s: None) if test_mode else time.sleep)
        except Exception as e:
            return stop(STOPPED, f"couldn't open the form: {str(e).splitlines()[0][:160]}", False)

        # Where the page really landed counts too: a redirect to a manual-only site stops here.
        if why := gate(live(), page.url, listing):
            return stop(MANUAL_REQUIRED, why, True)
        problem = check_page(page, provider_of(listing, url))
        if problem:
            return stop(*problem)

        # 4. Map.
        fields = page.evaluate(FIELDS_JS)
        if any(f["kind"] == "password" for f in fields):
            return stop(MANUAL_REQUIRED, "the form asks for a password (an account): apply by hand", True)
        s.mappings = map_fields(fields, p, mat, listing.company)
        state = page.evaluate(STATE_JS)
        warnings = []
        if state["captchaDormant"]:
            warnings.append("the form has a CAPTCHA that may challenge you when you submit: complete it yourself")
        if mat and mat.get("flags"):
            warnings.append(f"the materials have {mat['flags']} fact-check flag(s): see fact_check.md")
        s.meta = {"listing_id": listing.id, "company": listing.company, "title": listing.title, "url": url,
                  "provider": provider_of(listing, url), "dry_run": dry, "materials": mat["folder"] if mat else None,
                  "warnings": warnings}
        unconfident = [m.label or m.uid for m in s.mappings if not m.confident]

        # 5a. Dry run (now or switched on since the run began): the mapping only.
        if dry or live()["dry_run"]:
            dry = s.meta["dry_run"] = True
            s.meta["filled_at"] = None
            record = build_record(s, [None] * len(s.mappings))
            path = os.path.join(folder, "field_mapping.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2, ensure_ascii=False)
            s.close()
            apps.set(listing, DRY_RUN_MAPPED, "dry run: mapping only, nothing typed", record_path=path,
                     record_hash=record_hash(record), unconfident=unconfident)
            return FillResult(listing.id, DRY_RUN_MAPPED, "dry run: mapping only, nothing typed", path,
                              record_hash=record_hash(record), unconfident=unconfident)

        # 5b. Fill, re-checking the kill switch and manual-only list before every field.
        for m in s.mappings:
            if m.action == "leave":
                continue
            if why := gate(live(), page.url, listing):
                return stop(MANUAL_REQUIRED, f"stopped part-way: {why}", True)
            try:
                _apply(page, m)
            except Exception as e:
                m.confident, m.note = False, f"couldn't fill it: {str(e).splitlines()[0][:120]}"
        problem = check_page(page, provider_of(listing, url), after_fill=True, url=url)
        if problem:
            return stop(*problem)
        if why := gate(live(), page.url, listing):
            return stop(MANUAL_REQUIRED, f"stopped before review: {why}", True)

        # 6. Review stage.
        entered = page.evaluate(READ_JS, [m.uid for m in s.mappings])
        for m, value in zip(s.mappings, entered):
            if m.action == "fill" and (value or "") != (m.value or ""):
                m.confident, m.note = False, "the page holds a different value than was typed"
            elif m.action in ("choose", "upload") and m.confident and not value:
                m.confident, m.note = False, m.note or "nothing was selected"
        s.meta["filled_at"] = _now().isoformat(timespec="seconds")
        record = build_record(s, entered)
        s.record_hash = record_hash(record)
        record_path = os.path.join(folder, "application_record.json")
        with open(record_path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
        shot = os.path.join(folder, "review.png")
        page.screenshot(path=shot, full_page=True)
        unconfident = [m.label or m.uid for m in s.mappings if not m.confident]
        apps.set(listing, AWAITING_REVIEW, "filled: waiting for your review", record_path=record_path,
                 record_hash=s.record_hash, screenshot_path=shot, unconfident=unconfident,
                 filled_at=s.meta["filled_at"])
        log_event(AWAITING_REVIEW, listing.id, record_hash=s.record_hash, screenshot=shot)
        _SESSIONS[listing.id] = s
        return FillResult(listing.id, AWAITING_REVIEW, "filled: waiting for your review", record_path, shot,
                          s.record_hash, unconfident, browser_open=True)


def check_page(page, provider, after_fill=False, url=None):
    """None if the page is a form we can work on, else (status, reason, keep the browser open)."""
    state = page.evaluate(STATE_JS)
    if state["captchaVisible"] or state["botText"]:
        return (MANUAL_REQUIRED, "a CAPTCHA or bot check appeared: the browser is left open for you; nothing was solved or "
                         "bypassed", True)
    if after_fill and url and state["url"].split("#")[0] != url.split("#")[0]:
        return MANUAL_REQUIRED, f"the page moved to {state['url']} while filling: stopped", True
    if state["password"] or LOGIN_PATH_RE.search(urllib.parse.urlsplit(state["url"]).path):
        return MANUAL_REQUIRED, "the site wants you to log in or create an account: apply by hand", True
    if state["otherFrames"]:
        return (MANUAL_REQUIRED, "unexpected layout: the page shows an embedded frame (" + ", ".join(state["otherFrames"][:3]) +
                ") instead of a plain form", True)
    if provider in EXPECTED:
        missing = [name for name, sel in EXPECTED[provider].items() if page.locator(sel).count() == 0]
        if missing:
            if state["loginText"]:
                return MANUAL_REQUIRED, "the site wants you to log in or create an account: apply by hand", True
            return MANUAL_REQUIRED, f"unexpected layout for a {provider} form: missing {', '.join(missing)}", True
    else:
        fields = page.evaluate(FIELDS_JS)
        labels = [_norm(f["label"]) for f in fields]
        has_email = any(re.search(r"^e ?mail", l) for l in labels)
        has_name = any(re.search(r"^(?:full |legal |your )?name$|^first name", l) for l in labels)
        if not (has_email and has_name):
            if state["loginText"]:
                return MANUAL_REQUIRED, "the site wants you to log in or create an account: apply by hand", True
            return MANUAL_REQUIRED, "unexpected layout: couldn't find an application form (name and email fields)", True
    return None


def _apply(page, m):
    loc = page.locator(f'[data-jarvis-uid="{m.uid}"]')
    if m.action == "fill":
        loc.first.fill(m.value)
    elif m.action == "upload":
        loc.first.set_input_files(m.value)
    elif m.action == "choose":
        if m.kind == "select":
            options = loc.first.evaluate("e => [...e.options].map(o => o.text.replace(/\\s+/g, ' ').trim())")
            pick = choose_option(options, m.rule, m.value)
            if pick is None:
                raise ValueError("no single matching option")
            loc.first.select_option(index=options.index(pick))
        elif m.kind == "radio":
            options = [o.strip() for o in loc.evaluate_all("els => els.map(e => (e.labels && e.labels.length ? e.labels[0].innerText : e.value))")]
            pick = choose_option(options, m.rule, m.value)
            if pick is None:
                raise ValueError("no single matching option")
            page.locator(f'[data-jarvis-uid="{m.uid}"][data-jarvis-opt="{options.index(pick)}"]').check()
        elif m.kind == "combobox":
            loc.first.click()
            opts = page.locator("[role=option]")
            try:
                opts.first.wait_for(state="visible", timeout=3000)
            except Exception:
                page.keyboard.press("Escape")
                raise ValueError("its options didn't open")
            texts = [t.strip() for t in opts.all_inner_texts()]
            pick = choose_option(texts, m.rule, m.value)
            if pick is None:
                page.keyboard.press("Escape")
                m.value = None
                raise ValueError("no single matching option (" + ", ".join(texts[:6]) + ")")
            opts.nth(texts.index(pick)).click()
            m.value = pick


# ---------------------------------------------------------------- approval and the one submit path

def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _create_approval(listing_id, db_path=DB_PATH):
    """Store an approval for the record as reviewed now; returns the token (shown once, stored only hashed).
    Called only by the `approve` command, which checks you're typing it in a terminal."""
    cfg = load_config()
    if not _submit_allowed(cfg):
        raise PermissionError("approved submit is off (needs allow_approved_submit, dry_run off, filling_enabled on)")
    with Applications(db_path) as apps:
        row = apps.get(listing_id)
        if not row or row["status"] != AWAITING_REVIEW:
            raise PermissionError(f"listing {listing_id} isn't waiting for review")
        if why := gate(cfg, row["url"]):
            raise PermissionError(why)
        with open(row["record_path"], encoding="utf-8") as f:
            record = json.load(f)
        if record_hash(record) != row["record_hash"]:
            raise PermissionError("the saved record no longer matches its hash: it changed after filling")
        token = secrets.token_urlsafe(12)
        with apps.db:
            apps.db.execute("INSERT INTO approvals (listing_id, record_hash, token_hash, created_at) VALUES (?, ?, ?, ?)",
                            (listing_id, row["record_hash"], _sha(token), _now().isoformat(timespec="seconds")))
    log_event("approval_created", listing_id, record_hash=row["record_hash"])
    return token


def _submit_allowed(cfg):
    return bool(cfg.get("allow_approved_submit")) and not cfg.get("dry_run", True) and bool(cfg.get("filling_enabled"))


class SubmitRefused(Exception):
    pass


try:
    _ALLOW_SUBMIT_AT_IMPORT = bool(load_config().get("allow_approved_submit"))
except Exception:
    _ALLOW_SUBMIT_AT_IMPORT = False

globals().pop("submit_approved", None)            # never left over from an earlier load of this module
if _ALLOW_SUBMIT_AT_IMPORT:
    def submit_approved(listing_id, approval_token, db_path=DB_PATH):
        """Click the final submit button, once, on the form you reviewed and approved. See the module notes."""
        cfg = load_config()
        if not _submit_allowed(cfg):
            raise SubmitRefused("submitting is disabled (needs allow_approved_submit: true, dry_run: false, "
                                "filling_enabled: true)")
        s = _SESSIONS.get(listing_id)
        if s is None or s.page.is_closed():
            raise SubmitRefused("no open session for this listing: approvals only work on the browser left open "
                                "after filling; a form is never reopened to submit")
        if why := gate(cfg, s.page.url) or gate(cfg, s.url):
            raise SubmitRefused(why)
        with Applications(db_path) as apps:
            row = apps.get(listing_id)
            if not row or row["status"] != AWAITING_REVIEW or row["record_hash"] != s.record_hash:
                raise SubmitRefused("this listing isn't waiting for review in this session")
            approval = apps.db.execute(
                "SELECT id, record_hash, created_at, used_at FROM approvals WHERE listing_id = ? AND token_hash = ? "
                "ORDER BY id DESC", (listing_id, _sha(approval_token or ""))).fetchone()
            if approval is None:
                raise SubmitRefused("that approval token doesn't match")
            aid, approved_hash, created, used = approval
            if used:
                raise SubmitRefused("that approval token was already used")
            if approved_hash != row["record_hash"]:
                raise SubmitRefused("that approval was for a different version of the form")
            if _now() - datetime.datetime.fromisoformat(created) > datetime.timedelta(minutes=APPROVAL_MINUTES):
                raise SubmitRefused(f"that approval is older than {APPROVAL_MINUTES} minutes: approve again")
            page = s.page
            if page.url.split("#")[0] != s.url.split("#")[0]:
                raise SubmitRefused("the browser is no longer on the reviewed form")
            state = page.evaluate(STATE_JS)
            if state["captchaVisible"] or state["botText"]:
                raise SubmitRefused("a CAPTCHA is showing: complete and submit it yourself in the browser")
            live = build_record(s, page.evaluate(READ_JS, [m.uid for m in s.mappings]))
            if record_hash(live) != approved_hash:
                raise SubmitRefused("the form changed after your review, so the approval no longer matches: review "
                                    "and approve again")
            submit_selector = ("form button[type=submit], form input[type=submit], button#btn-submit, "
                               "button[data-qa=btn-submit]")
            buttons = [b for b in page.locator(submit_selector).all() if b.is_visible()]
            if len(buttons) != 1:
                raise SubmitRefused(f"expected exactly one submit button, found {len(buttons)}: submit it yourself")
            now = _now().isoformat(timespec="seconds")
            with apps.db:
                apps.db.execute("UPDATE approvals SET used_at = ? WHERE id = ?", (now, aid))
            log_event("approved_by_you", listing_id, record_hash=approved_hash, approved_at=created)
            buttons[0].click()
            try:
                page.wait_for_load_state("load", timeout=15000)
            except Exception:
                pass
            after = page.evaluate(STATE_JS)
            note = ("the site showed a CAPTCHA after submit: finish it in the browser"
                    if after["captchaVisible"] or after["botText"] else "submitted after your approval")
            apps.db.execute("UPDATE applications SET status = ?, reason = ?, submitted_at = ?, updated_at = ? "
                            "WHERE listing_id = ?", (SUBMITTED, note, now, now, listing_id))
            apps.db.commit()
            log_event(SUBMITTED, listing_id, record_hash=approved_hash, note=note)
            return note


# ---------------------------------------------------------------- the command line

def approve_cli(listing_id, db_path=DB_PATH):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("Approvals must be typed by you in a terminal.")
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        sys.exit("Not from inside Claude Code: the token would end up in a model's context. Use your own terminal.")
    cfg = load_config()
    if not _submit_allowed(cfg):
        sys.exit("Approved submit is off (needs allow_approved_submit: true, dry_run: false, filling_enabled: true).")
    with Applications(db_path) as apps:
        row = apps.get(listing_id)
    if not row or row["status"] != AWAITING_REVIEW:
        sys.exit(f"{listing_id} isn't waiting for review.")
    if why := gate(cfg, row["url"]):
        sys.exit(f"Not approved: {why}.")
    with open(row["record_path"], encoding="utf-8") as f:
        record = json.load(f)
    print(f"\n{row['company']} / {row['title']}\n{row['url']}\nScreenshot: {row['screenshot_path']}\n")
    for x in record["fields"]:
        mark = " " if x["confident"] else "!"
        print(f" {mark} {x['label'][:60]:60} {str(x['entered'] or '')[:60]}")
    print(f"\nRecord hash: {row['record_hash']}")
    typed = input(f"Type the company name ({row['company']}) to approve submitting exactly this: ").strip()
    if _norm(typed) != _norm(row["company"]):
        sys.exit("Not approved.")
    token = _create_approval(listing_id, db_path)
    print(f"\nApproval token (valid {APPROVAL_MINUTES} min, once): {token}\n"
          "Paste it into the terminal where the form is waiting.")
    return token


def main(argv):
    if len(argv) >= 2 and argv[0] == "fill":
        r = fill(argv[1])
        print(f"{r.status}: {r.reason}")
        if r.record_path:
            print(f"record: {r.record_path}\nscreenshot: {r.screenshot_path}\nhash: {r.record_hash}")
        for label in r.unconfident:
            print(f"  not filled confidently: {label}")
        if r.browser_open:
            try:
                while True:
                    if "submit_approved" in globals() and r.status == AWAITING_REVIEW:
                        line = input("\nPaste an approval token to submit, or press Enter to close the browser: ").strip()
                        if line:
                            try:
                                print(globals()["submit_approved"](r.listing_id, line))
                            except SubmitRefused as e:
                                print(f"Refused: {e}")
                            continue
                    else:
                        input("\nThe browser is open for you (review and press Submit there yourself). "
                              "Press Enter here when you're done to close it: ")
                    break
            except (EOFError, KeyboardInterrupt):
                pass
            close_session(r.listing_id)
    elif len(argv) >= 2 and argv[0] == "approve":
        approve_cli(argv[1])
    elif argv[:1] == ["status"]:
        with Applications() as apps:
            for r in apps.all():
                print(f"{r['status']:24} {r['company']} / {r['title']}  {r['reason'] or ''}")
    else:
        sys.exit("usage: python -m internship_apply.form_filler fill|approve <listing id> | status")


if __name__ == "__main__":
    main(sys.argv[1:])

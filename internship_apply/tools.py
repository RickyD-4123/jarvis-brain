"""Tools: the internship_apply functions Jarvis can call, registered like gtools.TOOLS
(name -> (kind, one-line doc)) and run by gtools.ToolChat through Internships.call().

    internships_find                       fetch and score new listings; the top matches
    internships_prepare(listing_id)        materials + fact-check report for one listing
    internships_fill(listing_id)           fill its form in the browser, stopping before submit
    internships_queue                      what's waiting for your review
    internships_mark(listing_id, status, note="")   change a status (only ones you may set)
    internships_summary                    counts by status and the next deadlines

Rules enforced here, in code:
  * Confirmation: a tool that changes anything (find, prepare, fill, mark) never runs on the first call.
    It returns what it WOULD do; it runs only when Claude repeats the identical call in the very next
    turn and your message there is a yes.
  * internships_mark refuses approved_for_submit always (that's a terminal command, never a tool), and
    submitted_by_me unless BOTH your request and your yes say "submitted by me" in so many words.
  * Nothing here can reach form_filler.submit_approved (or the approval commands): submitting stays in
    your terminal and your browser. guard_answer() strikes any claim that an application was submitted.
  * Results are short, speakable and in the butler's voice: no ids or links in "say", listing text never
    included, company and role names cleaned and summarised (scraped text is untrusted).
"""
import datetime
import json
import re

from internship_apply import llm_budget, load_config
from internship_apply.log import get_logger

log = get_logger("tools")

# kind as in gtools.TOOLS: "read" pulls content in; "create" makes new things; "change" alters existing
# state; "act" does something on this computer.
TOOL_SPECS = {
    "internships_find": ("create", "internships_find(): fetch new internship listings from the boards in your settings, "
                                   "score them, and give the best matches. Changes state: needs his yes first."),
    "internships_prepare": ("create", "internships_prepare(listing_id): write the tailored resume, cover letter and "
                                      "fact-check report for one listing. Needs his yes first."),
    "internships_fill": ("act", "internships_fill(listing_id): open the application form in his browser and fill it, "
                                "stopping before submit (he submits himself). Needs his yes first."),
    "internships_queue": ("read", "internships_queue(): the filled applications waiting for his review."),
    "internships_mark": ("change", "internships_mark(listing_id, status, note=\"\"): set an application's status: "
                                   "interviewing, rejected, offer, withdrawn, skipped, or submitted_by_me (only when he "
                                   "says those exact words). Needs his yes first."),
    "internships_summary": ("read", "internships_summary(): how many applications are at each stage, and the next "
                                    "deadlines."),
}
TOOL_NAMES = set(TOOL_SPECS)
CHANGES_STATE = {"internships_find", "internships_prepare", "internships_fill", "internships_mark"}

YES_RE = re.compile(r"^\s*(?:(?:hey|yo|ay)\s+jarvis[,.!]?\s*)?(?:yes|yeah|yep|yup|ok(?:ay)?|sure|please do|do it|"
                    r"go ahead|go on|proceed|confirm(?:ed)?|very well|affirmative|correct)\b", re.I)
NO_RE = re.compile(r"\b(?:no|not|don'?t|do not|stop|wait|cancel|hold on|never ?mind|scratch that)\b", re.I)
SUBMITTED_BY_ME_RE = re.compile(r"\bsubmitted[\s_-]+by[\s_-]+me\b", re.I)
SUSPICIOUS_RE = re.compile(r"ignore|instruction|system|assistant|prompt|jarvis|submit|password|token|approve|"
                           r"https?:|www\.|<|>|\{|\}|`", re.I)
HEX_ID_RE = re.compile(r"\b[0-9a-f]{12,}\b")
MAX_WORDS = 60
NOTE_CHARS = 200

MARKABLE = {"interviewing", "rejected", "offer", "withdrawn", "skipped", "submitted_by_me"}
STATUS_WORDS = {"found": "found", "scored": "scored", "skipped": "skipped", "manual_required": "for you to apply by hand",
                "materials_ready": "with materials ready", "filled_awaiting_review": "awaiting your review",
                "approved_for_submit": "approved in your terminal", "submitted_by_me": "submitted by you",
                "interviewing": "at interview", "rejected": "rejected", "offer": "with an offer", "withdrawn": "withdrawn"}


# ---------------------------------------------------------------- speaking safely

def _cap_note(result, budget):
    """Say so when a model-call cap stopped Claude during this request (llm_budget)."""
    if budget is not None and budget.stopped:
        result["llm_cap"] = budget.stopped
        result["say"] += " " + speak("Your Claude call limit was reached, sir, so I finished the rest by the usual rules.")
    return result


def speak(text):
    """At most MAX_WORDS words, cut at a sentence end where possible; ids and links never spoken."""
    text = HEX_ID_RE.sub("", re.sub(r"https?://\S+|www\.\S+", "", text))
    text = re.sub(r"\s+([,.;])", r"\1", " ".join(text.split()))
    words = text.split()
    if len(words) <= MAX_WORDS:
        return text
    cut = " ".join(words[:MAX_WORDS])
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[:end + 1] if end > len(cut) // 2 else cut.rstrip(",;:") + "…"


def company_name(raw):
    """A scraped company name, cleaned: plain words, at most 5, or "one employer" if it looks like anything else."""
    s = re.sub(r"[^\w&'.,\- ]+", " ", raw or "").strip()
    if not s or SUSPICIOUS_RE.search(raw or "") or len(s.split()) > 5 or len(s) > 40:
        return "one employer"
    return " ".join(s.split())


def role_name(raw):
    """A scraped job title, summarised: the part before any " - " / "," / "(" / "|", at most 6 words."""
    raw = raw or ""
    if SUSPICIOUS_RE.search(raw):
        return "an internship"
    head = re.split(r"\s[-–—|]\s|,|\(|\|", raw, maxsplit=1)[0]
    head = re.sub(r"[^\w&'.\- ]+", " ", head)
    words = head.split()
    if not words:
        return "an internship"
    short = " ".join(words[:6])
    if not re.search(r"intern|co-?op|placement", short, re.I) and re.search(r"intern|co-?op|placement", raw, re.I):
        short += " internship"
    return short


def _when(day):
    d = datetime.date.fromisoformat(day[:10])
    suffix = "th" if 11 <= d.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d.day % 10, "th")
    return f"{d:%A} the {d.day}{suffix} of {d:%B}"


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


SUBMIT_CLAIM_RE = re.compile(
    r"\b(?:I|we)(?:'ve| have| just| now| also)*\s+(?:submitted|sent off|sent in|filed|lodged)\b[^.]*|"
    r"\b(?:I|we)(?:'ve| have| just| now| also)*\s+applied\b[^.]*|"
    r"\b(?:application|form)s?\b[^.]{0,60}?\b(?:has|have|was|were|is|are|got)\s+(?:now\s+|been\s+|just\s+)*"
    r"(?:submitted|sent|filed)\b(?![^.]{0,20}\bby you\b)[^.]*", re.I)
SUBMIT_CORRECTION = "To be clear, sir, I have not submitted anything; only you submit applications."


def guard_answer(answer):
    """(answer, corrected): any sentence saying an application was submitted (by Jarvis, or just "was submitted")
    is removed and a correction added. "Recorded as submitted by you" is fine: that's your own word."""
    if not SUBMIT_CLAIM_RE.search(answer or ""):
        return answer, False
    kept = [s for s in re.split(r"(?<=[.!?])\s+", answer) if not SUBMIT_CLAIM_RE.search(s)]
    return " ".join(kept + [SUBMIT_CORRECTION]).strip(), True


# ---------------------------------------------------------------- the tools

class Internships:
    """The target object ToolChat calls. ask: the server's Claude call (for scoring and materials), or None."""

    def __init__(self, ask=None, db_path=None, output_root=None, profile_path=None, config_loader=load_config):
        from internship_apply import listings, materials, profile
        self.ask = ask
        self.db_path = db_path or listings.DB_PATH
        self.output_root = output_root or materials.OUTPUT_DIR
        self.profile_path = profile_path or profile.DEFAULT_PATH
        self.config = config_loader
        self.pending = None                       # {"key", "turn_no", "plan"} awaiting your yes

    # ------------------------------------------------------------ the confirmation gate

    @staticmethod
    def _key(name, args):
        return name, json.dumps(args, sort_keys=True, default=str)

    def call(self, name, args, turn):
        """ToolChat's entry point. turn: {"question": your message, "turn_no": int, ...}."""
        if name not in TOOL_NAMES:
            return {"ok": False, "say": "There's no such internship tool, sir."}
        if not isinstance(args, dict):
            return {"ok": False, "say": "I couldn't read that request, sir."}
        question, turn_no = turn.get("question", ""), turn.get("turn_no", 0)
        log.info("tool %s called (turn %s)", name, turn_no)
        try:
            if name not in CHANGES_STATE:
                return getattr(self, name)(**args)
            p, key = self.pending, self._key(name, args)
            if p and p["key"] == key and p["turn_no"] == turn_no - 1 and self._confirmed(name, args, question):
                self.pending = None
                return getattr(self, name)(**args)
            if p and p["turn_no"] == turn_no - 1 and p["key"] == key and YES_RE.search(question) \
                    and name == "internships_mark" and str(args.get("status", "")).lower() == "submitted_by_me":
                p["turn_no"] = turn_no                      # keep waiting: the yes didn't say the status
                return {"ok": False, "needs_confirmation": True,
                        "say": speak("I need the exact words, sir. Please say: yes, submitted by me.")}
            plan = getattr(self, "plan_" + name)(question=question, **args)
            if not plan.get("ok", True):
                self.pending = None
                return plan
            self.pending = {"key": key, "turn_no": turn_no, "plan": plan["say"]}
            return {"ok": True, "needs_confirmation": True, "done": False,
                    "say": speak(plan["say"] + " " + plan.get("ask", "Shall I proceed, sir?"))}
        except TypeError as e:
            return {"ok": False, "say": "That request was missing something, sir.", "error": str(e)[:120]}

    def _confirmed(self, name, args, question):
        if not YES_RE.search(question) or (NO_RE.search(question) and not SUBMITTED_BY_ME_RE.search(question)):
            return False
        if name == "internships_mark" and str(args.get("status", "")).lower() == "submitted_by_me":
            return bool(SUBMITTED_BY_ME_RE.search(question))
        return True

    # ------------------------------------------------------------ helpers

    def _tracker(self):
        from internship_apply.tracker import Tracker
        return Tracker(self.db_path)

    def _record(self, listing_id):
        with self._tracker() as t:
            return t.get(listing_id)

    def _label(self, r):
        return f"the {role_name(r['role'])} role at {company_name(r['company'])}"

    def _missing(self, listing_id):
        return {"ok": False, "say": "I can't find that listing in your tracker, sir. Ask me for the summary first."}

    # ------------------------------------------------------------ plans (first call: say what would happen)

    def plan_internships_find(self, question=""):
        cfg = self.config()
        boards = len(cfg["greenhouse_boards"]) + len(cfg["lever_companies"])
        how = "with Claude's judgement as well" if cfg.get("use_model_scoring") and self.ask else "by the usual rules"
        return {"say": f"I'll check {_plural(min(boards, int(cfg['max_companies_per_run'])), 'job board')} and your pasted "
                       f"links for new internships, then score them {how}."}

    def plan_internships_prepare(self, listing_id, question=""):
        r = self._record(listing_id)
        if not r:
            return self._missing(listing_id)
        return {"say": f"I'll write a tailored resume, cover letter and answers for {self._label(r)}, then fact-check "
                       "every claim against your profile."}

    def plan_internships_fill(self, listing_id, question=""):
        r = self._record(listing_id)
        if not r:
            return self._missing(listing_id)
        from internship_apply import form_filler as ff
        from internship_apply.listings import Store
        cfg = self.config()
        if not cfg["filling_enabled"]:
            return {"ok": False, "say": speak("Form filling is switched off in your settings, sir, so I won't touch that "
                                              "form. Turn on filling enabled if you'd like me to.")}
        with Store(self.db_path) as store:
            listing = next((x for x in store.all() if x.id == listing_id), None)
        if listing is not None and ff.gate(cfg, listing.apply_url or listing.url, listing):
            return {"ok": False, "say": speak("That one is for you to apply by hand, sir: it's a manual-only site or "
                                              "needs a sign-in, so I won't fill it.")}
        if cfg["dry_run"]:
            return {"say": f"Dry run is on, sir, so for {self._label(r)} I'll only map the form's fields. Nothing will "
                           "be typed."}
        return {"say": f"I'll open the form for {self._label(r)} in your browser and fill it from your profile and "
                       "materials, stopping before the submit button."}

    def plan_internships_mark(self, listing_id, status, note="", question=""):
        from internship_apply import tracker as tr
        r = self._record(listing_id)
        if not r:
            return self._missing(listing_id)
        status = re.sub(r"[\s-]+", "_", str(status).strip().lower())
        if status == tr.APPROVED:
            return {"ok": False, "say": speak("Approving a form for submission is done in your own terminal, sir, never "
                                              "by me. I've left it as it is.")}
        if status not in MARKABLE:
            return {"ok": False, "say": speak("I can mark an application as interviewing, rejected, offer, withdrawn, "
                                              "skipped or submitted by you, sir. Which did you mean?")}
        if status == tr.SUBMITTED and not SUBMITTED_BY_ME_RE.search(question):
            return {"ok": False, "say": speak("I won't mark anything as submitted unless you say so in those words, sir. "
                                              "If you've submitted it yourself, please say: mark it submitted by me.")}
        if status not in tr.TRANSITIONS[r["status"]]:
            return {"ok": False, "say": speak(f"{self._label(r).capitalize()} is {STATUS_WORDS[r['status']]}, sir, so it "
                                              f"can't go straight to {STATUS_WORDS[status]}.")}
        plan = {"say": f"I'll mark {self._label(r)} as {STATUS_WORDS[status]}."}
        if status == tr.SUBMITTED:
            plan["ask"] = "To confirm, please say: yes, submitted by me."
        return plan

    # ------------------------------------------------------------ running (after your yes)

    def internships_find(self):
        from internship_apply import listings, profile, scoring
        from internship_apply.tracker import INACTIVE
        cfg = self.config()
        report = listings.collect(cfg, db_path=self.db_path)
        p = profile.load(self.profile_path)
        budget = llm_budget.metered(self.ask, cfg) if cfg.get("use_model_scoring") else None    # one run per request
        scoring.score_all(cfg, p, db_path=self.db_path, ask=budget)
        with self._tracker() as t:
            t.sync(config=cfg, output_root=self.output_root)
            inactive = {r["listing_id"] for r in t.all() if r["status"] in INACTIVE}
        top = [(x, s) for x, s in scoring.top_unapplied(10, cfg, db_path=self.db_path) if x.id not in inactive][:3]
        items = [{"id": x.id, "company": company_name(x.company), "role": role_name(x.title), "score": s.score}
                 for x, s in top]
        head = f"I found {_plural(report.new, 'new listing')}, sir."
        if report.errors:
            head += f" {_plural(len(report.errors), 'source')} gave trouble."
        if items:
            best = "; ".join(f"{i['role']} at {i['company']}, {i['score']}" for i in items)
            say = f"{head} The best matches: {best}."
        else:
            say = f"{head} Nothing scores above your minimum just now."
        out = {"ok": True, "done": True, "say": speak(say), "matches": items}
        return _cap_note(out, budget)

    def internships_prepare(self, listing_id):
        from internship_apply import materials, profile
        r = self._record(listing_id)
        if not r:
            return self._missing(listing_id)
        cfg = self.config()
        budget = llm_budget.metered(self.ask, cfg)                                             # one run per request
        try:
            folder, m = materials.build(listing_id, db_path=self.db_path, profile_path=self.profile_path, ask=budget,
                                        root=self.output_root, config=cfg)
        except profile.ProfileError as e:
            return {"ok": False, "say": speak(f"Your profile isn't finished, sir: {_plural(len(e.problems), 'required field')} "
                                              "still need filling in before I can write anything.")}
        except LookupError:
            return self._missing(listing_id)
        with self._tracker() as t:
            t.sync(config=cfg, output_root=self.output_root)
        flags = len(m.flags)
        check = ("The fact check found nothing unsupported." if not flags else
                 f"The fact check flagged {_plural(flags, 'claim')} for you to look at before anything is used.")
        return _cap_note({"ok": True, "done": True, "id": listing_id,
                          "say": speak(f"The materials for {self._label(r)} are ready, sir. {check}")}, budget)

    def internships_fill(self, listing_id):
        from internship_apply import form_filler as ff
        r = self._record(listing_id)
        if not r:
            return self._missing(listing_id)
        res = ff.fill(listing_id, config=self.config(), db_path=self.db_path, profile_path=self.profile_path,
                      output_root=self.output_root)
        with self._tracker() as t:
            t.sync(config=self.config(), output_root=self.output_root)
        label = self._label(r)
        reason = res.reason or ""
        if res.status == ff.AWAITING_REVIEW:
            n = len(res.unconfident)
            need = f" {_plural(n, 'field')} need you." if n else ""
            say = (f"The form for {label} is filled and waiting in your browser for your review, sir.{need} "
                   "Pressing submit is yours alone.")
            return {"ok": True, "done": True, "id": listing_id, "say": speak(say)}
        if res.status == ff.DRY_RUN_MAPPED:
            return {"ok": True, "done": True, "id": listing_id,
                    "say": speak(f"Dry run is on, sir, so I only mapped the form for {label}. Nothing was typed.")}
        plain = [("filling_enabled", "Form filling is switched off in your settings, sir, so I didn't touch it."),
                 ("manual_only_domains", "That site is on your manual-only list, sir. It's one to apply for by hand."),
                 ("max_applications_per_day", "You've reached today's limit on applications, sir. I'll leave it for tomorrow."),
                 ("by email", "That one is applied for by email, sir, so it's yours to send."),
                 ("CAPTCHA", "A security check appeared, sir. I've left the browser open for you and haven't touched it."),
                 ("log in", "The site wants you to sign in, sir, so it's one to do by hand."),
                 ("unexpected layout", "The form isn't laid out as I expected, sir, so I stopped rather than guess."),
                 ("already", "You already have that role in hand, sir, so I won't fill it twice."),
                 ("no materials yet", "There are no materials for it yet, sir. Shall I prepare them first?"),
                 ("profile not ready", "Your profile isn't finished yet, sir, so I can't fill anything."),
                 ("manual-only", "That listing is marked to apply by hand, sir.")]
        say = next((s for k, s in plain if k.lower() in reason.lower()),
                   "I couldn't fill that form, sir. The browser is left as it was.")
        return {"ok": False, "done": False, "say": speak(say)}

    def internships_mark(self, listing_id, status, note=""):
        from internship_apply import tracker as tr
        status = re.sub(r"[\s-]+", "_", str(status).strip().lower())
        if status == tr.APPROVED:                       # belt and braces: never, whatever the gate said
            return {"ok": False, "say": "Approving for submission is done in your own terminal, sir."}
        with self._tracker() as t:
            r = t.get(listing_id)
            if not r:
                return self._missing(listing_id)
            try:
                t.set_status(listing_id, status, "set by you through Jarvis")
                if note:
                    t.note(listing_id, str(note)[:NOTE_CHARS])
            except tr.TrackerError:
                return {"ok": False, "say": speak("The tracker wouldn't allow that change, sir; it would skip a step.")}
        return {"ok": True, "done": True, "id": listing_id,
                "say": speak(f"Very good, sir. {self._label(r).capitalize()} is now recorded as {STATUS_WORDS[status]}.")}

    def internships_queue(self):
        with self._tracker() as t:
            q = t.review_queue()
        if not q:
            return {"ok": True, "say": "Nothing is waiting for your review, sir.", "waiting": []}
        parts = []
        for x in q[:3]:
            bits = [f"{role_name(x['role'])} at {company_name(x['company'])}"]
            if x["score"] is not None:
                bits.append(f"score {x['score']}")
            if x["flagged_fields"]:
                bits.append(f"{_plural(len(x['flagged_fields']), 'field')} for you")
            if x["fact_check_warnings"]:
                bits.append(f"{_plural(len(x['fact_check_warnings']), 'fact-check flag')}")
            parts.append(", ".join(bits))
        more = f" And {len(q) - 3} more." if len(q) > 3 else ""
        say = f"{_plural(len(q), 'application')} await your review, sir: " + "; ".join(parts) + "." + more
        return {"ok": True, "say": speak(say), "waiting": [{"id": x["listing_id"], "company": company_name(x["company"]),
                                                            "role": role_name(x["role"])} for x in q]}

    def internships_summary(self, today=None):
        from internship_apply.tracker import INACTIVE, SUBMITTED, INTERVIEWING, OFFER
        today = today or datetime.date.today()
        with self._tracker() as t:
            rows = t.all()
            follow = t.followups(today)
        if not rows:
            return {"ok": True, "say": "Your tracker is empty, sir. Ask me to find some internships."}
        counts = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        order = ["filled_awaiting_review", "materials_ready", "scored", "manual_required", "submitted_by_me",
                 "interviewing", "offer"]
        parts = [f"{counts[s]} {STATUS_WORDS[s]}" for s in order if counts.get(s)]
        say = f"{_plural(sum(c for s, c in counts.items() if s not in INACTIVE), 'application')} in play, sir"
        say += (": " + ", ".join(parts) + ".") if parts else "."
        soon = sorted((r for r in rows if r["deadline"] and r["status"] not in INACTIVE | {SUBMITTED, INTERVIEWING, OFFER}
                       and 0 <= (datetime.date.fromisoformat(r["deadline"][:10]) - today).days <= 14),
                      key=lambda r: r["deadline"])
        if soon:
            say += " Next deadline: " + "; ".join(f"{role_name(r['role'])} at {company_name(r['company'])}, "
                                                  f"{_when(r['deadline'])}" for r in soon[:2]) + "."
        if follow:
            say += f" {_plural(len(follow), 'application')} could do with a follow-up."
        return {"ok": True, "say": speak(say), "counts": counts}

"""Tracker: every listing's journey, from found to offer, in the `tracker` table of listings.db.

Statuses and who may set them:
  found, scored, skipped, manual_required, materials_ready, filled_awaiting_review
        set by sync() from what the other steps recorded (listings, scores, materials, the form
        filler); skipped and manual_required can also be set by you.
  approved_for_submit
        ONLY by `python -m internship_apply.tracker approve <id> --token` typed in your terminal, and only
        when allow_approved_submit is true and form_filler defines submit_approved. Never from set_status(),
        voice, tool calls or model output.
  submitted_by_me, interviewing, rejected, offer, withdrawn
        ONLY by you, through an explicit command naming the status (CLI `submitted` / `set`, or
        set_status(id, "submitted_by_me")). Nothing here infers them: not even the form filler's own
        "submitted" after an approved submit, which only adds a note asking you to confirm.

Every change is checked against TRANSITIONS (so found -> submitted_by_me, or materials_ready ->
submitted_by_me skipping the review stage, is refused) and written to `tracker_history`. Only one active
record (anything but skipped, rejected, withdrawn) per company + role.

    python -m internship_apply.tracker sync | list [status] | queue | show <id>
    python -m internship_apply.tracker approve <id> [--token] | edit <id> [note] | reject <id> [reason]
    python -m internship_apply.tracker submitted <id> | set <id> <status> [note] | note <id> <text>
    python -m internship_apply.tracker export [path.csv] | sheets | followups
"""
import csv
import datetime
import json
import os
import re
import sys
import urllib.parse

from internship_apply import PACKAGE_DIR, load_config
from internship_apply.listings import DB_PATH, Store, domain_matches
from internship_apply.log import get_logger

log = get_logger("tracker")

FOUND, SCORED, SKIPPED, MANUAL = "found", "scored", "skipped", "manual_required"
MATERIALS, AWAITING, APPROVED = "materials_ready", "filled_awaiting_review", "approved_for_submit"
SUBMITTED, INTERVIEWING, REJECTED, OFFER, WITHDRAWN = "submitted_by_me", "interviewing", "rejected", "offer", "withdrawn"
STATUSES = [FOUND, SCORED, SKIPPED, MANUAL, MATERIALS, AWAITING, APPROVED, SUBMITTED, INTERVIEWING, REJECTED, OFFER, WITHDRAWN]
INACTIVE = {SKIPPED, REJECTED, WITHDRAWN}
# Statuses that mean "you've applied for this role (or it's ready to send)": the form filler won't fill the same
# company + role again in any later run.
APPLIED_STATUSES = {AWAITING, APPROVED, SUBMITTED, INTERVIEWING, OFFER, REJECTED}
FOLLOW_UP_DAYS = 14
EXPORT_PATH = os.path.join(PACKAGE_DIR, "output", "applications.csv")
CSV_HEADERS = ["Company", "Role", "Location", "Fit score", "Status", "Date found", "Date submitted", "Deadline", "Link", "Notes"]

# status -> the statuses it may move to. Anything else is an illegal jump.
TRANSITIONS = {
    FOUND: {SCORED, SKIPPED, MANUAL, WITHDRAWN},
    SCORED: {MATERIALS, SKIPPED, MANUAL, WITHDRAWN},
    SKIPPED: {SCORED},                               # rescored above the bar (by sync), or you un-skip it
    MANUAL: {MATERIALS, SUBMITTED, SKIPPED, WITHDRAWN},   # you applied by hand, or it can be retried
    MATERIALS: {AWAITING, MANUAL, SKIPPED, WITHDRAWN},
    AWAITING: {APPROVED, SUBMITTED, MATERIALS, MANUAL, SKIPPED, WITHDRAWN},
    APPROVED: {SUBMITTED, AWAITING, WITHDRAWN},      # back to review if the approval lapses or the form changed
    SUBMITTED: {INTERVIEWING, REJECTED, OFFER, WITHDRAWN},
    INTERVIEWING: {OFFER, REJECTED, WITHDRAWN},
    OFFER: {WITHDRAWN},                               # declining an offer
    REJECTED: set(),
    WITHDRAWN: set(),
}
SYSTEM_SETS = {FOUND, SCORED, SKIPPED, MANUAL, MATERIALS, AWAITING}
USER_SETS = {SCORED, SKIPPED, MANUAL, MATERIALS, SUBMITTED, INTERVIEWING, REJECTED, OFFER, WITHDRAWN}   # not APPROVED

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracker (
    listing_id TEXT PRIMARY KEY,
    company TEXT NOT NULL, role TEXT NOT NULL, location TEXT, link TEXT, deadline TEXT,
    status TEXT NOT NULL, reason TEXT, notes TEXT NOT NULL DEFAULT '',
    fit_score INTEGER,
    materials_folder TEXT, resume_path TEXT, cover_letter_path TEXT, fact_check_path TEXT,
    record_path TEXT, record_hash TEXT, screenshot_path TEXT,
    found_at TEXT NOT NULL, scored_at TEXT, materials_at TEXT, filled_at TEXT, approved_at TEXT, submitted_at TEXT,
    updated_at TEXT NOT NULL,
    last_update_at TEXT NOT NULL              -- last status change or note: what the follow-up rule looks at
);
CREATE TABLE IF NOT EXISTS tracker_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id TEXT NOT NULL,
    from_status TEXT, to_status TEXT NOT NULL, at TEXT NOT NULL, by TEXT NOT NULL, reason TEXT
);
"""
COLUMNS = ["listing_id", "company", "role", "location", "link", "deadline", "status", "reason", "notes", "fit_score",
           "materials_folder", "resume_path", "cover_letter_path", "fact_check_path", "record_path", "record_hash",
           "screenshot_path", "found_at", "scored_at", "materials_at", "filled_at", "approved_at", "submitted_at",
           "updated_at", "last_update_at"]
STAMP = {SCORED: "scored_at", MATERIALS: "materials_at", AWAITING: "filled_at", APPROVED: "approved_at",
         SUBMITTED: "submitted_at"}


class TrackerError(Exception):
    """An illegal transition, a duplicate, or a status only you can set."""


def _now():
    return datetime.datetime.now().astimezone()


def _norm(s):
    return " ".join(re.sub(r"[^\w]+", " ", (s or "").lower()).split())


def role_key(company, title):
    """One role, however it's written: case, punctuation, "(Summer 2027)"-style asides and internship /
    intern / co-op spellings don't make a different role. A different team after " - " still does."""
    t = re.sub(r"\([^)]*\)", " ", title or "")
    t = re.sub(r"\b(?:internships?|interns)\b", "intern", t, flags=re.I)
    t = re.sub(r"\bco[\s-]?ops?\b", "coop", t, flags=re.I)
    return f"{_norm(company)}|{_norm(t)}"


def submit_feature_available(config=None):
    """True only when allow_approved_submit is on in config.yaml AND form_filler defines submit_approved."""
    from internship_apply import form_filler
    cfg = config or load_config()
    return bool(cfg.get("allow_approved_submit")) and hasattr(form_filler, "submit_approved")


class Tracker:
    def __init__(self, db_path=DB_PATH, now=_now):
        self.db_path = db_path
        self.store = Store(db_path)
        self.db = self.store.db
        self.db.executescript(SCHEMA)
        self.now = now

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.store.close()

    # ------------------------------------------------------------ reading

    def get(self, listing_id):
        row = self.db.execute(f"SELECT {', '.join(COLUMNS)} FROM tracker WHERE listing_id = ?", (listing_id,)).fetchone()
        return dict(zip(COLUMNS, row)) if row else None

    def all(self, status=None):
        sql = f"SELECT {', '.join(COLUMNS)} FROM tracker" + (" WHERE status = ?" if status else "") + " ORDER BY found_at, company"
        return [dict(zip(COLUMNS, r)) for r in self.db.execute(sql, (status,) if status else ())]

    def history(self, listing_id):
        return [dict(zip(("from", "to", "at", "by", "reason"), r)) for r in self.db.execute(
            "SELECT from_status, to_status, at, by, reason FROM tracker_history WHERE listing_id = ? ORDER BY id",
            (listing_id,))]

    def active_duplicate(self, company, role, listing_id=None):
        for r in self.all():
            if r["status"] not in INACTIVE and r["listing_id"] != listing_id \
                    and role_key(r["company"], r["role"]) == role_key(company, role):
                return r
        return None

    # ------------------------------------------------------------ writing

    def create(self, listing):
        """A new record at "found". Refused if the same company + role already has an active record."""
        if self.get(listing.id):
            raise TrackerError(f"{listing.id} is already tracked")
        if dup := self.active_duplicate(listing.company, listing.title):
            raise TrackerError(f"already tracking {dup['company']} / {dup['role']} ({dup['listing_id']}, {dup['status']})")
        now = self.now().isoformat(timespec="seconds")
        with self.db:
            self.db.execute("INSERT INTO tracker (listing_id, company, role, location, link, deadline, status, found_at, "
                            "updated_at, last_update_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (listing.id, listing.company, listing.title, listing.location, listing.url, listing.deadline,
                             FOUND, now, now, now))
            self._history(listing.id, None, FOUND, now, "system", "")
        return self.get(listing.id)

    def _history(self, listing_id, frm, to, at, by, reason):
        self.db.execute("INSERT INTO tracker_history (listing_id, from_status, to_status, at, by, reason) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (listing_id, frm, to, at, by, reason))

    def _move(self, listing_id, to, by, reason="", **fields):
        """The one place a status changes. by: "system" | "you" | "you (approval token)"."""
        r = self.get(listing_id)
        if r is None:
            raise TrackerError(f"{listing_id} isn't tracked")
        if to not in STATUSES:
            raise TrackerError(f"unknown status {to!r}")
        if to not in TRANSITIONS[r["status"]]:
            raise TrackerError(f"illegal jump: {r['status']} -> {to}")
        if r["status"] in INACTIVE and to not in INACTIVE and \
                (dup := self.active_duplicate(r["company"], r["role"], listing_id)):
            raise TrackerError(f"already tracking {dup['company']} / {dup['role']} ({dup['listing_id']})")
        now = self.now().isoformat(timespec="seconds")
        cols = {"status": to, "reason": reason, "updated_at": now, "last_update_at": now, **fields}
        if to in STAMP:
            cols[STAMP[to]] = now
        with self.db:
            self.db.execute(f"UPDATE tracker SET {', '.join(k + ' = ?' for k in cols)} WHERE listing_id = ?",
                            [*cols.values(), listing_id])
            self._history(listing_id, r["status"], to, now, by, reason)
        log.info("%s: %s -> %s by %s (%s)", listing_id, r["status"], to, by, reason)
        return self.get(listing_id)

    def _update(self, listing_id, **fields):
        if fields:
            fields["updated_at"] = self.now().isoformat(timespec="seconds")
            with self.db:
                self.db.execute(f"UPDATE tracker SET {', '.join(k + ' = ?' for k in fields)} WHERE listing_id = ?",
                                [*fields.values(), listing_id])

    def note(self, listing_id, text):
        r = self.get(listing_id)
        if r is None:
            raise TrackerError(f"{listing_id} isn't tracked")
        now = self.now()
        line = f"[{now:%Y-%m-%d}] {text.strip()}"
        notes = (r["notes"] + "\n" + line).strip()
        with self.db:
            self.db.execute("UPDATE tracker SET notes = ?, updated_at = ?, last_update_at = ? WHERE listing_id = ?",
                            (notes, now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), listing_id))
        return self.get(listing_id)

    def set_status(self, listing_id, status, reason=""):
        """You changing a status, explicitly by name. approved_for_submit is never accepted here."""
        if status == APPROVED:
            raise TrackerError("approved_for_submit is only set by `python -m internship_apply.tracker approve <id> --token` "
                               "typed in your terminal")
        if status not in USER_SETS:
            raise TrackerError(f"{status} is set automatically from the other steps, not by hand")
        return self._move(listing_id, status, "you", reason)

    # ------------------------------------------------------------ the review stage

    def approve_self_submit(self, listing_id):
        """Approval without a token: you'll press Submit yourself in the open browser, then run `submitted`."""
        r = self.get(listing_id)
        if not r or r["status"] != AWAITING:
            raise TrackerError("only a filled application waiting for review can be approved")
        return self.note(listing_id, f"approved by you: you'll press Submit yourself (record {r['record_hash'][:12]}). "
                                     "Then run: tracker submitted " + listing_id)

    def approve_with_token(self, listing_id, make_token=None):
        """The CLI path to approved_for_submit: form_filler's terminal-only approval makes a token bound to the
        exact reviewed record; only once it succeeds does the status move."""
        if not submit_feature_available():
            raise TrackerError("approved submit isn't available (allow_approved_submit is off, or form_filler has no "
                               "submit_approved): approve means you press Submit yourself")
        r = self.get(listing_id)
        if not r or r["status"] != AWAITING:
            raise TrackerError("only a filled application waiting for review can be approved")
        from internship_apply import form_filler
        token = (make_token or form_filler.approve_cli)(listing_id, self.db_path)   # exits unless you're at a terminal
        if not token:
            raise TrackerError("no approval token was made")
        return self._move(listing_id, APPROVED, "you (approval token)", f"approved record {r['record_hash'][:12]}")

    def send_back_for_edit(self, listing_id, note=""):
        """Edit: back to materials_ready; the filled form must be redone after you change the materials."""
        r = self._move(listing_id, MATERIALS, "you", note or "sent back for edits at review")
        with self.db:                                  # let the form filler fill it again
            self.db.execute("UPDATE applications SET status = 'returned_for_edit', updated_at = ? WHERE listing_id = ?",
                            (self.now().isoformat(timespec="seconds"), listing_id))
        return r

    def reject_at_review(self, listing_id, reason=""):
        return self._move(listing_id, SKIPPED, "you", reason or "rejected by you at review")

    def review_queue(self):
        """Filled applications waiting for you: company, role, score, flagged fields, fact-check warnings,
        screenshot and the field-record hash."""
        out = []
        apps = {}
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'applications'").fetchone():
            apps = {r[0]: r[1] for r in self.db.execute("SELECT listing_id, unconfident FROM applications")}
        for r in self.all():
            if r["status"] not in (AWAITING, APPROVED):
                continue
            warnings = []
            if r["fact_check_path"] and os.path.exists(r["fact_check_path"]):
                with open(r["fact_check_path"], encoding="utf-8") as f:
                    warnings = [f"{x['where']}: {x['claim']} ({x['reason']})" for x in json.load(f).get("flags", [])]
            out.append({"listing_id": r["listing_id"], "company": r["company"], "role": r["role"], "score": r["fit_score"],
                        "status": r["status"], "flagged_fields": json.loads(apps.get(r["listing_id"]) or "[]"),
                        "fact_check_warnings": warnings, "screenshot": r["screenshot_path"], "record_hash": r["record_hash"]})
        return out

    # ------------------------------------------------------------ catching up with the other steps

    def sync(self, config=None, output_root=None):
        """Move records forward from what listings / scoring / materials / the form filler recorded.
        Only system statuses; never approved_for_submit or anything after it. Returns a report."""
        from internship_apply import form_filler, materials
        cfg = config or load_config()
        root = output_root or materials.OUTPUT_DIR
        report = {"new": 0, "moved": 0, "duplicates": [], "notes": []}
        listings = {x.id: x for x in self.store.all()}
        scores = {}
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'scores'").fetchone():
            scores = {r[0]: (r[1], r[2], json.loads(r[3])) for r in
                      self.db.execute("SELECT listing_id, score, skipped, blockers FROM scores")}
        apps = {}
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'applications'").fetchone():
            cols = ["listing_id", "status", "reason", "record_path", "record_hash", "screenshot_path"]
            apps = {r[0]: dict(zip(cols, r)) for r in self.db.execute(f"SELECT {', '.join(cols)} FROM applications")}

        for x in listings.values():
            r = self.get(x.id)
            if r is None:
                try:
                    r = self.create(x)
                    report["new"] += 1
                except TrackerError as e:
                    report["duplicates"].append(f"{x.company} / {x.title} ({x.id}): {e}")
                    continue
            before = r["status"]
            r = self._sync_one(r, x, scores.get(x.id), apps.get(x.id), cfg, root, report, form_filler, materials)
            if r["status"] != before:
                report["moved"] += 1
        return report

    def _sync_one(self, r, x, score, app, cfg, root, report, form_filler, materials):
        step = lambda to, reason="", **f: self._move(x.id, to, "system", reason, **f)
        if score is not None:
            value, blocked, blockers = score
            if r["fit_score"] != value:
                self._update(x.id, fit_score=value)
            low = blocked or value < int(cfg["min_fit_score"])
            why = f"blocked: {blockers[0]}" if blocked and blockers else f"fit score {value} is below {cfg['min_fit_score']}"
            last = (self.history(x.id) or [{}])[-1]
            if r["status"] == FOUND:
                r = step(SKIPPED, why) if low else step(SCORED, f"fit score {value}")
            elif r["status"] == SKIPPED and not low and last.get("by") == "system":
                r = step(SCORED, f"rescored: fit score {value}")       # never undoes a skip you made
        if r["status"] == SCORED:
            host = urllib.parse.urlsplit(x.apply_url or x.url).netloc
            if x.manual_only or domain_matches(host, cfg["manual_only_domains"]) or x.url.startswith("mailto:"):
                r = step(MANUAL, x.note or "manual-only site: apply by hand")
        mat = form_filler.find_materials(x.id, root)
        paths = {} if not mat else dict(materials_folder=mat["folder"], resume_path=mat["resume_path"],
                                         cover_letter_path=mat["cover_letter_path"],
                                         fact_check_path=os.path.join(mat["folder"], "fact_check.json"))
        if mat and r["status"] == SCORED:
            r = step(MATERIALS, "materials generated", **paths)
        if mat and app and r["status"] == MANUAL and app["status"] == form_filler.AWAITING_REVIEW:
            r = step(MATERIALS, "filled on a later try", **paths)          # e.g. after you switched filling on
        if app:
            if app["status"] == form_filler.MANUAL_REQUIRED and r["status"] in (FOUND, SCORED, MATERIALS):
                r = step(MANUAL, app["reason"] or "the form filler needs you to apply by hand")
            elif app["status"] == form_filler.AWAITING_REVIEW and r["status"] == MATERIALS:
                r = step(AWAITING, "filled: waiting for your review", record_path=app["record_path"],
                         record_hash=app["record_hash"], screenshot_path=app["screenshot_path"])
            elif app["status"] == form_filler.AWAITING_REVIEW and r["status"] == AWAITING and app["record_hash"] != r["record_hash"]:
                self._update(x.id, record_path=app["record_path"], record_hash=app["record_hash"],
                             screenshot_path=app["screenshot_path"])
            elif app["status"] == form_filler.SUBMITTED and r["status"] in (AWAITING, APPROVED) \
                    and "confirm with: tracker submitted" not in r["notes"]:
                r = self.note(x.id, "the form filler clicked Submit after your approval: confirm with: tracker submitted "
                              + x.id)
                report["notes"].append(f"{x.company} / {x.title}: confirm the submission yourself")
        return r

    # ------------------------------------------------------------ export and follow-ups

    def export_csv(self, path=EXPORT_PATH):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        safe = lambda v: ("'" + v) if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(CSV_HEADERS)
            for r in self.all():
                w.writerow([safe(v) for v in (r["company"], r["role"], r["location"] or "",
                                              "" if r["fit_score"] is None else r["fit_score"], r["status"],
                                              (r["found_at"] or "")[:10], (r["submitted_at"] or "")[:10],
                                              r["deadline"] or "", r["link"] or "", r["notes"].replace("\n", " | "))])
        return path

    def followups(self, today=None):
        """submitted_by_me with no status change or note for FOLLOW_UP_DAYS days: a suggestion each."""
        today = today or self.now().date()
        out = []
        for r in self.all(SUBMITTED):
            quiet = (today - datetime.date.fromisoformat(r["last_update_at"][:10])).days
            if quiet >= FOLLOW_UP_DAYS:
                out.append({"listing_id": r["listing_id"], "company": r["company"], "role": r["role"], "days": quiet,
                            "suggestion": f"No update from {r['company']} on {r['role']} for {quiet} days: consider a short, "
                                          "polite follow-up email to the recruiter, then add a note here."})
        return out


def export_sheets(config=None):
    """Google Sheets export, behind sheets_export_enabled (default false).

    TODO(OAuth): set up a separate OAuth client/scope for Sheets before this can work. Jarvis's gtools.py
    already holds a Google token with the Sheets scope, but this module must not borrow it without its
    own reviewed setup. Until then this does nothing."""
    cfg = config or load_config()
    if not cfg.get("sheets_export_enabled"):
        return "Google Sheets export is off (sheets_export_enabled: false). Use the CSV export instead."
    raise NotImplementedError("TODO: Google Sheets export needs its OAuth setup first; use the CSV export for now")


# ---------------------------------------------------------------- the command line

def _print_record(r, t):
    print(f"{r['listing_id']}  {r['company']} / {r['role']}  [{r['status']}]  score {r['fit_score']}")
    for k in ("reason", "link", "deadline", "materials_folder", "screenshot_path", "record_hash", "notes"):
        if r.get(k):
            print(f"  {k}: {r[k]}")
    for h in t.history(r["listing_id"]):
        print(f"  {h['at'][:16]}  {h['from'] or '-'} -> {h['to']}  by {h['by']}  {h['reason'] or ''}")


def main(argv):
    cmd, args = (argv[0], argv[1:]) if argv else ("list", [])
    try:
        with Tracker() as t:
            if cmd == "sync":
                rep = t.sync()
                print(f"{rep['new']} new, {rep['moved']} moved, {len(rep['duplicates'])} duplicates not tracked")
                for line in rep["duplicates"] + rep["notes"]:
                    print("  " + line)
            elif cmd == "list":
                for r in t.all(args[0] if args else None):
                    print(f"{r['status']:24} {str(r['fit_score'] if r['fit_score'] is not None else '-'):>4}  "
                          f"{r['company']} / {r['role']}  ({r['listing_id']})  {r['reason'] or ''}")
            elif cmd == "show" and args:
                _print_record(t.get(args[0]) or sys.exit("not tracked"), t)
            elif cmd == "queue":
                q = t.review_queue()
                if not q:
                    print("Nothing waiting for review.")
                for x in q:
                    print(f"\n{x['company']} / {x['role']}  score {x['score']}  [{x['status']}]  ({x['listing_id']})")
                    print(f"  screenshot: {x['screenshot']}\n  record hash: {x['record_hash']}")
                    for f in x["flagged_fields"]:
                        print(f"  flagged field: {f}")
                    for w in x["fact_check_warnings"]:
                        print(f"  fact-check: {w}")
                print("\napprove <id> | edit <id> [note] | reject <id> [reason]")
            elif cmd == "approve" and args:
                if "--token" in args:
                    r = t.approve_with_token(args[0])
                    print(f"{r['company']} / {r['role']}: approved_for_submit. Paste the token into the waiting form.")
                else:
                    t.approve_self_submit(args[0])
                    print("Approved: press Submit yourself in the open browser, then run: tracker submitted " + args[0])
                    if submit_feature_available():
                        print("(Or approve with a token: tracker approve " + args[0] + " --token)")
            elif cmd == "edit" and args:
                t.send_back_for_edit(args[0], " ".join(args[1:]))
                print("Sent back to materials_ready: change the materials, then fill the form again.")
            elif cmd == "reject" and args:
                t.reject_at_review(args[0], " ".join(args[1:]))
                print("Rejected at review: skipped.")
            elif cmd == "submitted" and args:
                t.set_status(args[0], SUBMITTED, "you submitted it")
                print("Marked submitted_by_me.")
            elif cmd == "set" and len(args) >= 2:
                t.set_status(args[0], args[1], " ".join(args[2:]))
                print(f"Marked {args[1]}.")
            elif cmd == "note" and len(args) >= 2:
                t.note(args[0], " ".join(args[1:]))
                print("Noted.")
            elif cmd == "export":
                print(t.export_csv(args[0] if args else EXPORT_PATH))
            elif cmd == "sheets":
                print(export_sheets())
            elif cmd == "followups":
                for f in t.followups() or [{"suggestion": "No follow-ups due."}]:
                    print(f["suggestion"])
            else:
                print(__doc__)
    except TrackerError as e:
        sys.exit(f"Refused: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])

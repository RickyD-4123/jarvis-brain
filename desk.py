"""Desk: cards and reminders, kept on disk in data/ (never served to the browser).

A card is anything that lands on the desk for you: a reminder coming due, a briefing, the outcome
of an instruction. Each has an id and a priority. The open desk page collects new cards by polling;
cards that came while it was closed are caught up the next time it opens, each exactly once.
"""
import datetime
import json
import os
import re
import threading
import time

import secure

KEEP_CARDS = 300


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    """Atomic: write a temp file, then swap it in. State files are owner-only after every write."""
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder)
        secure.lock(folder)                               # files created inside inherit owner-only
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)
    secure.lock(path)


# ---------------------------------------------------------------- cards

class Cards:
    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        saved = read_json(path, {"next": 1, "cards": []})
        self.next, self.cards = saved["next"], saved["cards"]

    def _save(self):
        self.cards = self.cards[-KEEP_CARDS:]
        write_json(self.path, {"next": self.next, "cards": self.cards})

    def add(self, kind, title, text, priority="normal", ref=None):
        with self.lock:
            card = {"id": f"card-{self.next}", "n": self.next, "kind": kind, "title": title, "text": text,
                    "priority": priority, "ref": ref, "created": time.time(), "at_desk": None}
            self.next += 1
            self.cards.append(card)
            self._save()
            return dict(card)

    def latest(self):
        with self.lock:
            return self.next - 1

    def for_desk(self, after):
        """Cards newer than `after` (a card number), marked as delivered to the open desk."""
        with self.lock:
            new = [c for c in self.cards if c["n"] > after]
            now = time.time()
            for c in new:
                c["at_desk"] = c["at_desk"] or now
            if new:
                self._save()
            return [dict(c) for c in new]

    def catch_up(self, seconds, kinds=("reminder", "briefing", "outcome", "reply")):
        """Cards of these kinds from the last `seconds` that no desk page has shown yet, marked shown."""
        with self.lock:
            cutoff, now = time.time() - seconds, time.time()
            missed = [c for c in self.cards if c["created"] >= cutoff and not c["at_desk"] and c["kind"] in kinds]
            for c in missed:
                c["at_desk"] = now
            if missed:
                self._save()
            return [dict(c) for c in missed]

    def recent(self, seconds):
        with self.lock:
            cutoff = time.time() - seconds
            return [dict(c) for c in self.cards if c["created"] >= cutoff]


# ---------------------------------------------------------------- reminders

WORD_NUMS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
             "thirty": 30, "forty": 40, "forty-five": 45, "a couple of": 2, "a few": 3}
PREFIX = r"^\s*(?:(?:hey\s+)?jarvis[,.]?\s*)?(?:please\s+|can you\s+|could you\s+)?"
# "remind me at 5 to…", and "in twenty minutes give me a reminder about the invoice": a time phrase may
# come first, but nothing else may (so "did you remind me…?" is not a new reminder).
REMIND_RE = re.compile(PREFIX + r"(?P<pre>(?:(?:in|at|tomorrow|tonight|today|this|on)\b[^,?]*?,?\s+)?)"
                       r"(?:please\s+)?(?:remind me|set (?:me )?(?:a |an )?reminder|give me a reminder|send me a reminder)\b"
                       r"(?:\s*(?P<conn>to|that|about|of|for)\b)?\s*(?P<rest>.*)$", re.I | re.S)
LIST_RE = re.compile(r"\b(?:what|which|list|show|any|my)\b.*\breminders?\b|^\s*reminders?\s*\??\s*$", re.I)
CANCEL_RE = re.compile(r"\b(?:cancel|delete|remove|clear|drop)\b.*\breminder\s*(?:number\s*|no\.?\s*|#\s*)?(\d+)", re.I)
PRIORITY_RE = re.compile(r"[,;]?\s*\b(?:(?:it'?s\s+)?urgent(?:ly)?|important|high[- ]priority|asap|critical)\b", re.I)
NUM = r"(\d+|a couple of|a few|forty-five|" + "|".join(k for k in WORD_NUMS if " " not in k and k != "forty-five") + r")"
REL_RE = re.compile(r"\bin\s+(?:" + NUM + r"\s+(minutes?|mins?|hours?|hrs?|days?)|(half an hour|an hour and a half))\b", re.I)
AT_RE = re.compile(r"\b(?:(today|tonight|tomorrow)\s+)?at\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?"
                   r"(?:\s+(today|tonight|tomorrow))?(?![\w:])", re.I)
NOON_RE = re.compile(r"\b(?:(today|tomorrow)\s+)?at\s+(noon|midday|midnight)(?:\s+(today|tomorrow))?\b", re.I)
PART_RE = re.compile(r"\b(tomorrow|this)\s+(morning|afternoon|evening)\b|\b(tonight|tomorrow)\b", re.I)
PARTS = {"morning": 9, "afternoon": 14, "evening": 18, "tonight": 20, "tomorrow": 9}


def _clock(h, m, ampm, day, now):
    ampm = (ampm or "").replace(".", "").lower()
    if h > 23 or m > 59 or (ampm and not 1 <= h <= 12):
        return None
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if day == "tomorrow":
        base += datetime.timedelta(days=1)
    if not ampm and h <= 12:
        if day == "tonight" and h < 12:
            h += 12
        elif day == "tomorrow" and 1 <= h <= 6:
            h += 12                                       # "tomorrow at 3" means the afternoon
        elif not day:
            # The next time the clock shows h:m: this morning, this afternoon, or tomorrow morning.
            for cand in ([h, h + 12] if h < 12 else [12]):
                t = base.replace(hour=cand % 24, minute=m)
                if t > now:
                    return t
            return (base + datetime.timedelta(days=1)).replace(hour=h % 24, minute=m)
    return base.replace(hour=h, minute=m)


def parse_reminder(text, now=None):
    """("add", task, due, priority) | ("list",) | ("cancel", n) | ("error", message) | None."""
    now = now or datetime.datetime.now()
    m = CANCEL_RE.search(text)
    if m and re.search(r"\breminder", text, re.I):
        return ("cancel", int(m.group(1)))
    m = REMIND_RE.match(text)
    if not m:
        return ("list",) if LIST_RE.search(text) else None
    rest = (m.group("pre") + " " + m.group("rest")).strip()
    priority ="high" if PRIORITY_RE.search(rest) else "normal"
    # Remove the priority word only where it's an instruction about the reminder ("urgently" up front,
    # ", it's urgent" at the end), never from the reminder's own words ("the urgent invoice").
    rest = re.sub(r"^(?:urgently|asap|it'?s urgent[,:]?)\s+", "", rest, flags=re.I)
    rest = re.sub(r"[,;]?\s*(?:(?:it'?s|this is)\s+)?(?:urgent|important|high[- ]priority|asap|critical)[.!]?\s*$", "", rest, flags=re.I)
    due, span = None, None
    if (r := REL_RE.search(rest)):
        if r.group(3):
            minutes = 30 if r.group(3).lower() == "half an hour" else 90
        else:
            n = r.group(1).lower()
            n = int(n) if n.isdigit() else WORD_NUMS[n]
            unit = r.group(2).lower()
            minutes = n * (1 if unit.startswith("m") else 60 if unit.startswith("h") else 1440)
        due, span = now + datetime.timedelta(minutes=minutes), r.span()
    elif (r := NOON_RE.search(rest)):
        day = (r.group(1) or r.group(3) or "").lower()
        hour = 0 if r.group(2).lower() == "midnight" else 12
        due = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if day == "tomorrow" or due <= now:
            due += datetime.timedelta(days=1)
        span = r.span()
    elif (r := AT_RE.search(rest)):
        due = _clock(int(r.group(2)), int(r.group(3) or 0), r.group(4), (r.group(1) or r.group(5) or "").lower(), now)
        span = r.span()
        if not due:
            return ("error", "That isn't a time I recognise. Try 'at 5pm' or 'in 20 minutes'.")
    elif (r := PART_RE.search(rest)):
        word = (r.group(2) or r.group(3)).lower()
        due = now.replace(hour=PARTS[word], minute=0, second=0, microsecond=0)
        if (r.group(1) or r.group(3) or "").lower() == "tomorrow" or due <= now:
            due += datetime.timedelta(days=1)
        span = r.span()
    if not due:
        return ("error", "When should I remind you? Say something like 'at 5pm', 'tomorrow at 9' or 'in 20 minutes'.")
    if due <= now:
        return ("error", "That time has already passed.")
    task = re.sub(r"\s{2,}", " ", (rest[:span[0]] + " " + rest[span[1]:]).strip())
    conn = (m.group("conn") or "").lower()
    lead = re.match(r"(to|that|about|of)\s+", task, re.I)    # "remind me in 20 minutes to stretch"
    if lead:
        conn, task = conn or lead.group(1).lower(), task[lead.end():]
    task = task.strip(" ,.;:!?")
    if not task:
        return ("error", "Remind you of what, exactly?")
    return ("add", task[0].upper() + task[1:], due, priority, conn or "to")


def say_time(due, now=None):
    now = now or datetime.datetime.now()
    t = due.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
    if due.date() == now.date():
        return f"at {t}"
    if due.date() == (now + datetime.timedelta(days=1)).date():
        return f"tomorrow at {t}"
    return f"on {due:%A} {due.day} {due:%B} at {t}"


class Reminders:
    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        saved = read_json(path, {"next": 1, "items": []})
        self.next, self.items = saved["next"], saved["items"]

    def _save(self):
        keep = [r for r in self.items if not r["fired"] and not r["cancelled"]] + \
               [r for r in self.items if r["fired"] or r["cancelled"]][-100:]
        self.items = sorted(keep, key=lambda r: r["n"])
        write_json(self.path, {"next": self.next, "items": self.items})

    def add(self, task, due, priority="normal", source="desk"):
        with self.lock:
            r = {"n": self.next, "task": task, "due": due.timestamp(), "priority": priority, "source": source,
                 "created": time.time(), "fired": None, "cancelled": False}
            self.next += 1
            self.items.append(r)
            self._save()
            return dict(r)

    def pending(self):
        with self.lock:
            return [dict(r) for r in sorted(self.items, key=lambda r: r["due"]) if not r["fired"] and not r["cancelled"]]

    def cancel(self, n):
        with self.lock:
            for r in self.items:
                if r["n"] == n and not r["fired"] and not r["cancelled"]:
                    r["cancelled"] = True
                    self._save()
                    return dict(r)
        return None

    def take_due(self, now=None):
        """Reminders that are due, marked fired (on disk) before they are returned: each fires once."""
        now = now or time.time()
        with self.lock:
            due = [r for r in self.items if not r["fired"] and not r["cancelled"] and r["due"] <= now]
            for r in due:
                r["fired"] = now
            if due:
                self._save()
            return [dict(r) for r in due]

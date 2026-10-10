"""Standing orders: "I'm leaving, only alert me if anything urgent comes up".

classify(text) sorts every sentence, from the desk or the phone, into one of:
  "standing"  a standing order: arm the URGENT FILTER (matched anywhere in the sentence)
  "disarm"    "I'm back": lift it
  "ignore"    no request at all ("um", "never mind")
  "process"   everything else: handle it now, as usual

While the filter is armed, ordinary desk cards arrive quietly (shown, not chimed or spoken) and
an urgent one is announced at most ONCE per ten minutes: a burst of backend events can't flood
you. Arming it is a status change, never a card or a notification. Run this file to print the
routing table.
"""
import os
import re
import threading
import time

import desk

URGENT_COOLDOWN = 600
VERBS = r"(?:alert|notify|message|ping|text|tell|bother|disturb|call|contact|interrupt|wake|buzz|reach)"
URGENT = r"(?:urgent|an emergency|emergencies|emergency|important|can'?t wait|cannot wait|critical)"
STANDING_RE = re.compile(
    rf"\b(?:only|just)\s+{VERBS}\s+me\s+(?:if|when|for|about|with)\b[^.?!;]*?\b{URGENT}"
    rf"|\b{VERBS}\s+me\s+only\s+(?:if|when|for|about|with)\b[^.?!;]*?\b{URGENT}"
    rf"|\b(?:do\s+not|don'?t|dont|never)\s+{VERBS}\s+me\s+(?:unless|except)\b[^.?!;]*?\b{URGENT}"
    rf"|\bunless\s+(?:it'?s|it\s+is|something\s+is|something'?s)\s+{URGENT}[^.?!;]*?\b(?:leave|let)\s+me\b"
    rf"|\b{URGENT}\s+(?:things?\s+|stuff\s+|matters\s+|messages\s+|alerts\s+)?only\b"
    rf"|\bonly\s+(?:for\s+)?(?:urgent|emergencies)\b(?:\s+(?:things|stuff|matters|messages|alerts))?\s*(?:please)?\s*[.!]?\s*$"
    rf"|\bdo\s+not\s+disturb\b|\bdnd\b",
    re.I)
# A question ABOUT the filter ("will you only alert me if...?") is not an order.
QUESTION_RE = re.compile(r"^\s*(?:jarvis[,.]?\s*)?(?:will|would|do|does|can|could|should|are|is|did|have|how|what|why)\b[^.!]*\?\s*$", re.I)
DISARM_RE = re.compile(
    r"\b(?:i'?m|i\s+am)\s+(?:back|home|here|free|available)\b(?!\s+(?:at|by|in|around|after)\b)"
    r"|\bback\s+at\s+(?:my|the)\s+desk\b"
    r"|\b(?:lift|cancel|clear|end|drop|remove|stop|turn\s+off|disarm)\s+(?:the\s+|that\s+|my\s+)?"
    r"(?:urgent\s+filter|standing\s+order|filter|do\s+not\s+disturb|dnd)\b"
    r"|\byou\s+can\s+(?:message|notify|text|alert|bother|ping|reach)\s+me\s+(?:again|now|normally)\b"
    r"|\b(?:notify|tell|alert)\s+me\s+(?:about|of)\s+everything\b|\bback\s+to\s+normal\b", re.I)
# "If I'm free on Tuesday, book..." is a condition, not "I'm back".
CONDITIONAL_RE = re.compile(r"\b(?:if|when|whether|unless|once|as\s+long\s+as)\s+(?:i'?m|i\s+am)\s+"
                            r"(?:back|home|here|free|available)\b", re.I)
FILLER_WORDS =r"(?:u+m+|u+h+|e+r+m*|h+m+|m+|mhm|mm-?hmm|uh-?huh|a+h+|o+h+|huh|well|so|like|okay\s+so)"
IGNORE_RE = re.compile(rf"^[\s\W]*(?:{FILLER_WORDS}[\s\W]*)*(?:(?:never\s*mind|forget\s+it|scratch\s+that|"
                       rf"cancel\s+that|nothing|no\s+wait|wait)[\s\W]*)?$", re.I)
CONTEXTS = [  # first match wins: "I'm off to bed" is bedtime, not leaving
    ("bedtime", re.compile(r"\b(?:bed|bedtime|sleep|sleeping|asleep|turning\s+in|nap|good\s*night|night\s+night|"
                           r"call(?:ing)?\s+it\s+a\s+night)\b", re.I)),
    ("meeting", re.compile(r"\b(?:meeting|meetings|in\s+a\s+call|on\s+a\s+call|call\s+with|board\s+call|conference|"
                           r"presentation|interview|appointment|standup|stand-up|webinar|zoom|teams\s+call|"
                           r"(?:the|a|my|this)\s+\w*\s*call\s+(?:starts|is\s+starting|now))\b", re.I)),
    ("leaving", re.compile(r"\b(?:leaving|leave|heading\s+out|head\s+out|off\s+out|going\s+out|stepping\s+out|popping\s+out|"
                           r"out\s+for|i'?m\s+off|off\s+to|away|going\s+away|out\s+of\s+(?:the\s+)?office|commut\w*|"
                           r"driving|lunch|errands?|gym|walk)\b", re.I)),
]


def classify(text):
    """(route, context): route is "standing", "disarm", "ignore" or "process"."""
    t = (text or "").strip()
    if not t or IGNORE_RE.match(t):
        return ("ignore", None)
    if STANDING_RE.search(t) and not QUESTION_RE.match(t):
        return ("standing", next((name for name, rx in CONTEXTS if rx.search(t)), "general"))
    if DISARM_RE.search(t) and not QUESTION_RE.match(t) and not CONDITIONAL_RE.search(t):
        return ("disarm", None)
    return ("process", None)


class Standing:
    """The armed/disarmed state, on disk so a restart keeps your standing order."""

    def __init__(self, path, clock=time.time):
        self.path, self.clock, self.lock = path, clock, threading.Lock()
        self.state = desk.read_json(path, {"armed": False, "since": None, "context": None, "order": None,
                                           "last_urgent_push": 0, "held": 0})

    def _save(self):
        desk.write_json(self.path, self.state)

    def armed(self):
        return bool(self.state["armed"])

    def arm(self, order, context):
        with self.lock:
            if not self.state["armed"]:                   # a fresh order: a fresh count and a fresh cooldown
                self.state.update(since=self.clock(), held=0, last_urgent_push=0)
            self.state.update(armed=True, context=context, order=order)
            self._save()

    def disarm(self):
        """Lift the filter. Returns (was it armed, how many announcements it kept quiet)."""
        with self.lock:
            held, was = self.state.get("held", 0), self.state["armed"]
            self.state.update(armed=False, since=None, context=None, order=None, held=0)
            self._save()
            return was, held

    def urgent_slot(self):
        """True if an urgent card may be announced now (the ten-minute cooldown has passed)."""
        return self.clock() - self.state.get("last_urgent_push", 0) >= URGENT_COOLDOWN

    def note_urgent_push(self):
        with self.lock:
            self.state["last_urgent_push"] = self.clock()
            self._save()

    def note_held(self):
        with self.lock:
            self.state["held"] = self.state.get("held", 0) + 1
            self._save()

    def status(self):
        s = self.state
        return {"armed": s["armed"], "since": s["since"], "context": s["context"], "order": s["order"],
                "held": s.get("held", 0), "cooldown_left": max(0, int(URGENT_COOLDOWN - (self.clock() - s.get("last_urgent_push", 0))))}


# ---------------------------------------------------------------- the routing table

TABLE = [
    ("I'm leaving, only alert me if anything urgent comes up", "standing order", "leaving"),
    ("Don't message me unless it's urgent", "standing order", "general"),
    ("Heading into a meeting with the bank, so only notify me if something urgent comes in.", "standing order", "meeting"),
    ("Right, I'm off to bed. Don't wake me unless it's an emergency.", "standing order", "bedtime"),
    ("Thanks Jarvis, I'm stepping out for lunch; only ping me if it's urgent", "standing order", "leaving"),
    ("The board call starts now, do not disturb", "standing order", "meeting"),
    ("ok I'm going to sleep now, urgent stuff only", "standing order", "bedtime"),
    ("What's on my calendar tomorrow?", "process immediately", None),
    ("Is the Lantern Books invoice urgent?", "process immediately", None),
    ("Will you only alert me if something urgent comes up?", "process immediately", None),
    ("Don't call Maya unless it's urgent", "process immediately", None),
    ("Remind me at 5pm to call the bank, it's urgent", "process immediately", None),
    ("Off to the dentist, ping me only if it's urgent", "standing order", "leaving"),
    ("I'm back", "process immediately", None),
    ("I'll be back at 3, can you check my email?", "process immediately", None),
    ("If I'm free Tuesday at 3pm, put a call with the roaster in the diary", "process immediately", None),
    ("Call me", "process immediately", None),
    ("um", "ignore", None),
    ("hmm...", "ignore", None),
    ("uh, never mind", "ignore", None),
    ("", "ignore", None),
    ("...", "ignore", None),
]
LABEL = {"standing": "standing order", "disarm": "process immediately", "process": "process immediately", "ignore": "ignore"}


def run_table(ack=None):
    """Print the routing table; returns (passed, total). ack(context) -> the line it would say."""
    rows, passed = [], 0
    for sentence, want, want_ctx in TABLE:
        route, ctx = classify(sentence)
        got = LABEL[route]
        ok = got == want and (want_ctx is None or ctx == want_ctx)
        passed += ok
        note = (f"arms filter ({ctx})" + (f": \"{ack(ctx)}\"" if ack else "")) if route == "standing" else \
            "lifts filter" if route == "disarm" else ""
        rows.append((("PASS" if ok else "FAIL"), sentence or "(empty)", want, got, note))
    w = max(len(r[1]) for r in rows)
    print(f"{'':4}  {'sentence':{w}}  {'expected':19}  {'got':19}  note")
    for r in rows:
        print(f"{r[0]:4}  {r[1]:{w}}  {r[2]:19}  {r[3]:19}  {r[4]}")
    print(f"\n{passed}/{len(rows)} rows pass")
    return passed, len(rows)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import server
        ack = lambda ctx: server.STANDING_ACK[ctx]
    except Exception:
        ack = None
    p, n = run_table(ack)
    sys.exit(0 if p == n else 1)

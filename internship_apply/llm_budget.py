"""Model-call caps. Every Claude call internship_apply makes goes through the existing Claude Code brain
(your subscription, no API key, nothing pay-per-use) and is counted here first.

  max_llm_calls_per_run  calls one run may make (one scoring pass, one set of materials, one tool call)
  max_llm_calls_per_day  calls all runs together may make on one calendar day (local time)

A call is counted BEFORE it is made, so a call that fails still counts (it may have used your quota).
When a cap is reached the call is not made: CapReached is raised with a plain message, the callers stop
asking Claude and finish the run by the usual rules, and Budget.stopped keeps the message to show you.

The daily count lives in data/llm_calls.db (one row per call: day, time, purpose; no prompts).
"""
import contextlib
import datetime
import os
import sqlite3
import threading

from internship_apply import DATA_DIR, load_config

USAGE_DB = os.path.join(DATA_DIR, "llm_calls.db")      # tests point this at a throwaway file
_lock = threading.Lock()


class CapReached(Exception):
    """A model-call cap was reached; the call was not made."""


def _connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.execute("CREATE TABLE IF NOT EXISTS llm_calls (day TEXT NOT NULL, at TEXT NOT NULL, purpose TEXT NOT NULL)")
    return db


def used_on(day, db_path=None):
    """How many model calls were counted on day (a date)."""
    with contextlib.closing(_connect(db_path or USAGE_DB)) as db:
        return db.execute("SELECT COUNT(*) FROM llm_calls WHERE day = ?", (day.isoformat(),)).fetchone()[0]


class Budget:
    """A drop-in ask(prompt, system_prompt) that counts each call against both caps. One Budget = one run."""

    def __init__(self, ask, config=None, db_path=None, today=None):
        config = config or load_config()
        self.ask = ask
        self.per_run = int(config["max_llm_calls_per_run"])
        self.per_day = int(config["max_llm_calls_per_day"])
        self.db_path = db_path or USAGE_DB
        self.today = today                          # a fixed date for tests; None = the real day
        self.calls = 0
        self.stopped = None                         # the cap message, once a cap stopped a call

    def _day(self):
        return self.today or datetime.date.today()

    def used_today(self):
        return used_on(self._day(), self.db_path)

    def remaining(self):
        """Calls this run may still make: the smaller of what's left of the run and of the day."""
        return max(0, min(self.per_run - self.calls, self.per_day - self.used_today()))

    def _refuse(self, message):
        self.stopped = message
        raise CapReached(message)

    def take(self, purpose=""):
        """Count one call, or raise CapReached (nothing counted) if either cap is reached."""
        if self.calls >= self.per_run:
            self._refuse(f"Stopped asking Claude: this run has used its {self.per_run} model calls "
                         "(max_llm_calls_per_run in internship_apply/config.yaml).")
        with _lock, contextlib.closing(_connect(self.db_path)) as db:
            db.execute("BEGIN IMMEDIATE")           # check and count together, even across processes
            day = self._day().isoformat()
            used = db.execute("SELECT COUNT(*) FROM llm_calls WHERE day = ?", (day,)).fetchone()[0]
            if used >= self.per_day:
                db.execute("ROLLBACK")
                self._refuse(f"Stopped asking Claude: today's {self.per_day} model calls are used up "
                             "(max_llm_calls_per_day in internship_apply/config.yaml). The count starts again tomorrow.")
            now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            db.execute("INSERT INTO llm_calls (day, at, purpose) VALUES (?, ?, ?)", (day, now, str(purpose)[:40]))
            db.execute("COMMIT")
        self.calls += 1

    def __call__(self, prompt, system_prompt, purpose=""):
        self.take(purpose)
        return self.ask(prompt, system_prompt)


def metered(ask, config=None, **kw):
    """ask wrapped in a Budget (a new run), or the same Budget if it already is one, or None for None."""
    if ask is None or isinstance(ask, Budget):
        return ask
    return Budget(ask, config, **kw)

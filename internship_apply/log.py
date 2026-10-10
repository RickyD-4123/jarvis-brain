"""Logging for internship_apply: one rotating file in internship_apply/logs/, everything redacted.

    from internship_apply.log import get_logger
    log = get_logger("listings")
    log.info("fetched %s listings from %s", n, board)

Every record passes through safety.redact() before it is written (message AND arguments), so emails,
phone numbers, tokens, passwords, keys and your own name/email/phone/city never reach the file.
events(): structured JSON lines (the form filler's application events), redacted the same way.
logs/ is git-ignored.
"""
import json
import logging
import logging.handlers
import os
import threading

from internship_apply import USER_DIR
from internship_apply.safety import redact

LOG_DIR = os.path.join(USER_DIR, "logs")
LOG_FILE = "internship_apply.log"
EVENTS_FILE = "applications.jsonl"
_lock = threading.Lock()
_configured = {}


class Redact(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg, record.args = redact(msg), None
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def _handler(log_dir):
    os.makedirs(log_dir, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(os.path.join(log_dir, LOG_FILE), maxBytes=1_000_000, backupCount=3,
                                             encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    h.addFilter(Redact())
    return h


def get_logger(name, log_dir=None):
    """A logger under "internship_apply" writing (redacted) to log_dir (default internship_apply/logs/)."""
    root = logging.getLogger("internship_apply")
    with _lock:
        if log_dir is None:                           # keep whatever folder is already set (tests set a temp one)
            log_dir = _configured.get("dir") or LOG_DIR
        if _configured.get("dir") != log_dir:
            for h in list(root.handlers):
                root.removeHandler(h)
                h.close()
            root.addHandler(_handler(log_dir))
            root.setLevel(logging.INFO)
            root.propagate = False
            _configured["dir"] = log_dir
    return logging.getLogger(f"internship_apply.{name}")


def set_log_dir(log_dir):
    """Point every internship_apply logger at another folder (tests use a temp one)."""
    get_logger("log", log_dir)


def log_dir():
    return _configured.get("dir") or LOG_DIR


def event(kind, listing_id, **details):
    """One JSON line in logs/applications.jsonl (the form filler's audit trail), redacted."""
    import datetime
    line = json.dumps({"time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "event": kind,
                       "listing_id": listing_id, **details}, ensure_ascii=False, default=str)
    d = log_dir()
    os.makedirs(d, exist_ok=True)
    with _lock, open(os.path.join(d, EVENTS_FILE), "a", encoding="utf-8") as f:
        f.write(redact(line) + "\n")

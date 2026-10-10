"""Tests for internship_apply. Every test writes its logs to a throwaway folder, never internship_apply/logs/,
and counts model calls in a throwaway file, never data/llm_calls.db (the real daily count)."""
import atexit
import shutil
import tempfile

import os

from internship_apply import llm_budget, log

TEST_LOG_DIR = tempfile.mkdtemp(prefix="internship-logs-")
log.set_log_dir(TEST_LOG_DIR)
llm_budget.USAGE_DB = os.path.join(TEST_LOG_DIR, "llm_calls.db")


def _cleanup():
    import logging
    for h in list(logging.getLogger("internship_apply").handlers):
        h.close()
    shutil.rmtree(TEST_LOG_DIR, ignore_errors=True)


atexit.register(_cleanup)

"""Internship apply: Jarvis finds internship listings, scores them against your profile, drafts
materials, and (only when you allow it) fills in application forms, logging everything it does.

Safe by default (see config.yaml): dry_run is on, form filling is switched off, nothing is ever
submitted without your approval, and sites in manual_only_domains are never automated.

load_config() reads config.yaml (committed: neutral defaults, TODO for anything personal), then
config.local.yaml (git-ignored: your own values) on top of it, fills in defaults for anything left
out, and refuses wrong types. A value still set to TODO counts as not set, with a "fill in" note.
"""
import os
import sys

import yaml

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PACKAGE_DIR, "data")
CONFIG_PATH = os.path.join(PACKAGE_DIR, "config.yaml")
LOCAL_CONFIG_PATH = os.path.join(PACKAGE_DIR, "config.local.yaml")

DEFAULTS = {
    "target_roles": [], "locations": [], "remote_preference": "any",
    "greenhouse_boards": [], "lever_companies": [],
    "min_seconds_between_requests": 2, "user_agent_contact": "",
    "min_fit_score": 70, "max_applications_per_day": 5,
    "score_weights": {"skills": 40, "role": 10, "location": 15, "graduation": 15, "model": 30},
    "use_model_scoring": True, "model_prescore_threshold": 50,
    "max_llm_calls_per_run": 20, "max_llm_calls_per_day": 60,
    "max_companies_per_run": 25, "max_requests_per_run": 100,
    "dry_run": True, "filling_enabled": False, "allow_approved_submit": False, "headless_for_tests": True,
    "sheets_export_enabled": False,
    "manual_only_domains": ["myworkdayjobs.com"],
}
REMOTE_PREFERENCES = ("remote_only", "remote_or_hybrid", "any")


class ConfigError(Exception):
    pass


def is_todo(v):
    """A template placeholder ("TODO", "TODO: ...") counts as not filled in."""
    return isinstance(v, str) and v.strip().upper().startswith("TODO")


def _read(path, required=True):
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        if not required:
            return {}
        raise ConfigError(f"{path}: file not found") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: not valid YAML: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: should be a set of settings")
    return raw


_warned = set()


def load_config(path=CONFIG_PATH, local_path=None):
    """local_path: the override file; defaults to config.local.yaml when path is the shipped config.yaml.
    The result's "todo" lists the settings still set to TODO (left out, so their defaults apply)."""
    if local_path is None and path == CONFIG_PATH:
        local_path = LOCAL_CONFIG_PATH
    raw = _read(path)
    if local_path:
        for k, v in _read(local_path, required=False).items():
            raw[k] = {**raw[k], **v} if isinstance(raw.get(k), dict) and isinstance(v, dict) else v
    todo = []
    for k, v in list(raw.items()):
        if is_todo(v) or (isinstance(v, list) and any(is_todo(x) for x in v)):
            todo.append(k)
            raw[k] = [x for x in v if not is_todo(x)] if isinstance(v, list) else None
    if todo and tuple(todo) not in _warned:
        _warned.add(tuple(todo))
        print(f"internship_apply: fill in {', '.join(todo)} in {local_path or path} "
              "(still TODO, treated as not set)", file=sys.stderr)
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if v is not None}}
    problems = []
    for k, default in DEFAULTS.items():
        v = cfg[k]
        if isinstance(default, dict):
            if not isinstance(v, dict):
                problems.append(f"{k}: should be a set of named values")
                continue
            for name, x in v.items():
                if name not in default:
                    problems.append(f"{k}.{name}: unknown (expected: {', '.join(default)})")
                elif isinstance(x, bool) or not isinstance(x, (int, float)) or x < 0:
                    problems.append(f"{k}.{name}: should be a number, 0 or more")
            cfg[k] = {**default, **v}
        elif isinstance(default, list):
            if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
                problems.append(f"{k}: should be a list of text items")
            else:
                cfg[k] = [x.strip() for x in v]
        elif isinstance(default, bool):
            if not isinstance(v, bool):
                problems.append(f"{k}: should be true or false")
        elif isinstance(default, (int, float)):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                problems.append(f"{k}: should be a number, 0 or more")
        elif not isinstance(v, str):
            problems.append(f"{k}: should be text")
    if cfg["remote_preference"] not in REMOTE_PREFERENCES:
        problems.append(f"remote_preference: should be one of {', '.join(REMOTE_PREFERENCES)}")
    if problems:
        where = f"{path} + {local_path}" if local_path and os.path.exists(local_path) else path
        raise ConfigError(f"Problems in {where}:\n" + "\n".join("  - " + p for p in problems))
    cfg["manual_only_domains"] = [d.lower().lstrip(".") for d in cfg["manual_only_domains"]]
    cfg["todo"] = todo
    return cfg

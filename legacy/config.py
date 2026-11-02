"""
config.py — defaults + load/save for the thesis planner.

Config lives in planner/data/config.json (gitignored). Everything is optional;
missing keys fall back to DEFAULT_CONFIG (deep-merged on load).
"""
import json
import os
from copy import deepcopy

DEFAULT_CONFIG = {
    "work_days": [0, 1, 2, 3, 4],          # Mon=0 .. Sun=6
    "day_start": "09:00",
    "day_end": "19:00",
    "lunch": ["13:00", "14:00"],
    "buffer_min": 10,                       # gap between blocks
    "max_block_min": 90,                    # split longer tasks
    "deep_min_block": 45,                   # deep work refuses shorter fragments
    "max_day_min": 300,                     # planned work cap per day
    "calendar_name": "Thesis",              # Apple Calendar to read/write
    "habits_calendar_name": "Habits",       # mirror calendar for habit events
    "habits": {
        "enabled": True,                    # habits materialize + mirror
        "weeks_ahead": 1,                   # rolling window: current + N weeks
    },
    "university_root": "",                  # iCloud Drive folder with one
                                            # subfolder per subject; enables
                                            # the per-subject categories and
                                            # folder-grounded estimates
    "thesis_root": "",                      # the thesis repo; "" = this repo
    "pi_meeting": None,                     # e.g. "2026-11-03"
    "complexity_minutes": {"S": 45, "M": 90, "L": 180},
    "complexity_rank": {"S": 1, "M": 3, "L": 5},
    "deadline_soon_days": 3,                # "urgent" threshold for rationale
    "now_margin_min": 5,                    # earliest slot today is now + this
    "horizon_weeks": 1,                     # weeks the auto cycle may fill ahead
    # ---- background cycle (web cycle watcher + launchd tick) ----
    "auto": {
        "watch": True,                      # keep the heartbeat running
        "watch_seconds": 20,                # poll interval for the watcher
        "notify": False,                    # macOS notification on change
        "llm_estimates": False,             # background estimation via LLM
                                            # (the OpenRouter quota otherwise
                                            # serves real intakes; the
                                            # heuristic fills idle ticks)
        # periodic plan review: real triggers only (calibration shift, busy
        # deltas, deadline pressure, task churn, staleness), at most once per
        # every_hours, ONE LLM call, verdicts recorded with their why
        "review": {
            "enabled": True,
            "every_hours": 24,              # wall-clock gate
            "stale_hours": 48,              # plan older than this is stale
            "calibration_shift": 0.15,      # ratio moved >=15% since review
        },
    },
    # ---- estimating ----
    # backend: llm      -> any OpenAI-compatible endpoint (OpenRouter in
    #                      practice); the instant estimate, synchronous
    #          heuristic -> offline rules + calibration
    "brain": {
        "backend": "llm",
        "llm": {
            "base_url": "",                 # e.g. https://openrouter.ai/api/v1
            "model": "",                    # e.g. openrouter/free
            "api_key_env": "PLANNER_LLM_KEY",
            "key_file": "",                 # default planner/data/llm_key;
                                            # the file wins when the env var
                                            # (above) is unset. "" api_key_env
                                            # = declared keyless endpoint.
            "timeout": 90,
        },
    },
}


def _merge(base, over):
    out = deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path):
    cfg = deepcopy(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = _merge(cfg, json.load(f))
    return cfg


def save(path, cfg):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

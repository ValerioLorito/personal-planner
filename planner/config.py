"""
config.py — defaults + load/save for Planner.

Config lives in data/config.json (created on first run). Missing keys fall back
to DEFAULTS (deep-merged), so the file only needs what you change.
"""
from __future__ import annotations

import json
import os
from copy import deepcopy

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("PLANNER_DATA", os.path.join(APP_ROOT, "data"))
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "planner.db")
UI_DIR = os.path.join(APP_ROOT, "ui")

DEFAULTS = {
    "port": 8765,
    # ---- knowledge sources (read-only) ----
    "thesis": {
        "label": "Thesis",
        "root": "~/Desktop/commonsense-atlas",
        "skip_dirs": ["data", "external", ".git", "node_modules", "__pycache__",
                      ".venv", "venv", "build", "dist", ".ipynb_checkpoints",
                      "wandb", "outputs", "checkpoints", "logs"],
    },
    "university": {
        "root": "~/Desktop/iCloud/SAPIENZA/ERASMUS",
        "exclude": ["OeAD"],                 # subfolders that are not courses
        # extra words that identify a course in calendar event titles,
        # e.g. {"HYBRID QUANTUM": ["191.023", "quantum"]}
        "aliases": {},
    },
    # ---- calendars ----
    "calendars": {
        "thesis": "Thesis",                  # primary project / deep-work calendar
        "university": "University",          # courses / secondary calendar
        "habits": "Habits",                  # recurring habits calendar
        "read_also": [],                     # empty = auto-inherit all non-ignored calendars
        "ignore": ["Birthdays", "Siri Suggestions", "Scheduled Reminders",
                   "Festività in Italia", "Festività italiane", "Holidays"],
    },
    # ---- working pattern for placement ----
    "work": {
        "days": [0, 1, 2, 3, 4],             # Mon=0 .. Sun=6
        "weekend_if_needed": True,           # use Sat/Sun only when a deadline needs it
        "day_start": "09:00",
        "day_end": "19:00",
        "max_block_min": 90,
        "min_block_min": 25,
        "buffer_min": 10,
        "max_day_min": 360,                  # planned work per day (all tasks)
        "max_task_day_min": 180,             # one task per day — spreads work out
        "deadline_margin_hours": 12,         # finish this long before a deadline
        "now_margin_min": 10,
        "horizon_days": 28,
    },
    # ---- LLM (OpenAI-compatible; Gemini 3.5 Flash recommended or OpenRouter) ----
    "llm": {
        "provider": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-3.5-flash",
        "api_key_env": "GEMINI_API_KEY",
        "key_file": "",                      # default data/llm_key
        "timeout": 120,
        "temperature": 0.2,
        "daily_budget": 1500,                # Gemini free tier: 1500 req/day
        "watch_budget": 300,                 # of which the calendar watcher may use
        "max_steps": 4,                      # agent turns per task
    },
    # ---- live calendar watcher ----
    "watch": {
        "enabled": True,
        "debounce_sec": 2,
        "poll_sec": 120,                     # fallback poll if the live feed dies
        "agent_cooldown_min": 15,            # min gap between watcher-agent calls
        "days_back": 60,
        "days_ahead": 120,
    },
    "kit_path": "",                          # plannerkit binary; "" = auto
}

LLM_PRESETS = {
    "gemini": {
        "provider": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-3.5-flash",
        "api_key_env": "GEMINI_API_KEY",
        "daily_budget": 1500,
        "watch_budget": 300,
    },
    "gemini-3.5": {
        "provider": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-3.5-flash",
        "api_key_env": "GEMINI_API_KEY",
        "daily_budget": 1500,
        "watch_budget": 300,
    },
    "gemini-3.8": {
        "provider": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-3.8-flash",
        "api_key_env": "GEMINI_API_KEY",
        "daily_budget": 1500,
        "watch_budget": 300,
    },
    "gemini-3.5-lite": {
        "provider": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-3.5-flash-lite",
        "api_key_env": "GEMINI_API_KEY",
        "daily_budget": 1500,
        "watch_budget": 300,
    },
    "openrouter": {
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openrouter/free",
        "api_key_env": "OPENROUTER_API_KEY",
        "daily_budget": 50,
        "watch_budget": 12,
    },
}


def apply_preset(cfg: dict, preset_name: str) -> dict:
    if preset_name in LLM_PRESETS:
        cfg.setdefault("llm", {}).update(deepcopy(LLM_PRESETS[preset_name]))
    return cfg


def _merge(base, over):
    out = deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str = CONFIG_PATH) -> dict:
    cfg = deepcopy(DEFAULTS)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = _merge(cfg, json.load(f))
    return cfg


def save(cfg: dict, path: str = CONFIG_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def set_path(cfg: dict, dotted: str, value) -> dict:
    """Set cfg['a']['b'] for 'a.b'."""
    node = cfg
    parts = dotted.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    return cfg


def expand(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path)) if path else ""

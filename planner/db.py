"""
db.py — SQLite persistence (one file, data/planner.db).

A single connection guarded by a re-entrant lock: the server is threaded
(HTTP handlers, the calendar watcher, agent jobs) but the write volume is tiny.
Rows come back as plain dicts; JSON columns end in `_json`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    input_text TEXT NOT NULL,
    title TEXT DEFAULT '',
    category TEXT DEFAULT '',            -- 'thesis' | 'uni:<Course folder>'
    summary TEXT DEFAULT '',
    status TEXT DEFAULT 'planning',      -- planning|draft|scheduled|done|failed|cancelled
    complexity TEXT DEFAULT '',          -- S|M|L|XL
    estimate_minutes INTEGER DEFAULT 0,
    confidence REAL DEFAULT 0,
    priority INTEGER DEFAULT 2,
    deadline TEXT DEFAULT '',            -- 'YYYY-MM-DDTHH:MM'
    deadline_source TEXT DEFAULT '',
    deep_work INTEGER DEFAULT 0,
    prefs_json TEXT DEFAULT '{}',        -- scheduling preferences from the agent
    rationale TEXT DEFAULT '',
    files_json TEXT DEFAULT '[]',
    warnings_json TEXT DEFAULT '[]',
    trace_json TEXT DEFAULT '[]',
    error TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS subtasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    title TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    notes TEXT DEFAULT '',
    status TEXT DEFAULT 'todo',          -- todo|done
    actual_minutes INTEGER,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    subtask_id INTEGER,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    status TEXT DEFAULT 'proposed',      -- proposed|scheduled|done|removed
    calendar TEXT DEFAULT '',
    event_id TEXT DEFAULT '',
    title TEXT DEFAULT '',
    pinned INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    days_json TEXT NOT NULL,             -- [0..6], Mon=0
    start TEXT NOT NULL,                 -- 'HH:MM'
    minutes INTEGER NOT NULL,
    notes TEXT DEFAULT '',
    source TEXT DEFAULT 'user',          -- user|calendar|agent
    status TEXT DEFAULT 'active',        -- active|removed
    series_id TEXT DEFAULT '',           -- EventKit eventIdentifier of the series
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS habit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    habit_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    planned_minutes INTEGER,
    actual_minutes INTEGER,
    status TEXT NOT NULL,                -- done|skipped|moved
    source TEXT NOT NULL,                -- checkin|calendar
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(habit_id, date, source)
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,                -- watcher|habits|task|calibration
    title TEXT NOT NULL,
    rationale TEXT DEFAULT '',
    actions_json TEXT NOT NULL,
    status TEXT DEFAULT 'pending',       -- pending|applied|rejected|failed
    result TEXT DEFAULT '',
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS beliefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,                 -- global|habits|<category id>
    text TEXT NOT NULL,
    source TEXT DEFAULT 'agent',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                  -- task|habit
    scope TEXT NOT NULL,                 -- category id | habit:<id>
    complexity TEXT DEFAULT '',
    label TEXT DEFAULT '',
    estimated INTEGER NOT NULL,
    actual INTEGER NOT NULL,
    ref TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread TEXT NOT NULL,                -- 'habits' | 'task:<id>'
    role TEXT NOT NULL,                  -- user|agent
    content TEXT NOT NULL,
    data_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    purpose TEXT NOT NULL,
    ok INTEGER NOT NULL,
    model TEXT DEFAULT '',
    error TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS ix_blocks_task ON blocks(task_id);
CREATE INDEX IF NOT EXISTS ix_subtasks_task ON subtasks(task_id);
CREATE INDEX IF NOT EXISTS ix_messages_thread ON messages(thread);
"""


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class DB:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)

    def close(self):
        with self.lock:
            try:
                self.conn.close()
            except Exception:
                pass

    # ---- primitives ----
    def q(self, sql: str, args=()) -> list:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid

    def insert(self, table: str, row: dict) -> int:
        cols = list(row.keys())
        vals = [json.dumps(v, ensure_ascii=False) if k.endswith("_json") and not isinstance(v, str) else v
                for k, v in row.items()]
        sql = "INSERT INTO %s (%s) VALUES (%s)" % (table, ",".join(cols), ",".join("?" * len(cols)))
        return self.x(sql, vals)

    def update(self, table: str, row_id: int, fields: dict) -> None:
        if not fields:
            return
        cols, vals = [], []
        for k, v in fields.items():
            cols.append("%s=?" % k)
            vals.append(json.dumps(v, ensure_ascii=False) if k.endswith("_json") and not isinstance(v, str) else v)
        vals.append(row_id)
        self.x("UPDATE %s SET %s WHERE id=?" % (table, ",".join(cols)), vals)

    # ---- kv ----
    def kv_get(self, key: str, default=None):
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def kv_set(self, key: str, value) -> None:
        self.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value, ensure_ascii=False)))

    # ---- activity log ----
    def log(self, kind: str, message: str, data=None) -> None:
        self.insert("activity", {"ts": now_iso(), "kind": kind, "message": message,
                                 "data_json": data or {}})


def loads(value, default=None):
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default

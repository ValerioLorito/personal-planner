"""
app.py — the shared application object (config, DB, calendar, LLM, bus) and
helpers every module uses: busy time, day load, toasts, background jobs.
"""
from __future__ import annotations

import threading
import traceback
from collections import defaultdict
from datetime import datetime

from . import config, sources
from .bus import bus
from .calendar import Calendar, CalendarError, busy_intervals, dt, tag_of
from .db import DB
from .llm import LLM


class App:
    def __init__(self, cfg: dict = None, db_path: str = config.DB_PATH):
        self.cfg = cfg or config.load()
        self.db = DB(db_path)
        self.db.x("DELETE FROM blocks WHERE task_id NOT IN (SELECT id FROM tasks)")
        self.cal = Calendar(self.cfg)
        self.llm = LLM(self.cfg, self.db)
        self.bus = bus
        self.watcher = None
        self.calendar_error = ""

    # ---- notifications ----
    def notify(self, kind: str, data=None) -> None:
        self.bus.publish(kind, data or {})

    def toast(self, message: str, level: str = "info", log_kind: str = "info") -> None:
        self.db.log(log_kind, message)
        self.bus.publish("toast", {"message": message, "level": level})

    def background(self, name: str, fn, *args, **kwargs) -> threading.Thread:
        def run():
            try:
                fn(*args, **kwargs)
            except Exception as e:
                traceback.print_exc()
                self.toast("%s failed: %s" % (name, e), "error", "error")
        t = threading.Thread(target=run, name=name, daemon=True)
        t.start()
        return t

    # ---- calendar views ----
    def events(self, max_age: float = 30.0, refresh: bool = False) -> list:
        try:
            evs = self.cal.window(max_age=max_age, refresh=refresh)
            self.calendar_error = ""
            return evs
        except CalendarError as e:
            self.calendar_error = str(e)
            return self.cal.cached()

    def task_event_ids(self, task_id: int) -> set:
        rows = self.db.q("SELECT event_id FROM blocks WHERE task_id=? AND event_id!=''", (task_id,))
        return {r["event_id"] for r in rows}

    def busy(self, exclude_task_id: int = None, from_dt: datetime = None) -> list:
        """Busy intervals: calendar (minus this task's own blocks) + proposed
        blocks of other drafts that are not in the calendar yet."""
        evs = self.events()
        exclude = self.task_event_ids(exclude_task_id) if exclude_task_id else set()
        if exclude_task_id:
            # tagged events of the task's blocks (in case ids changed)
            ids = {r["id"] for r in self.db.q("SELECT id FROM blocks WHERE task_id=?", (exclude_task_id,))}
            for e in evs:
                kind, ref = tag_of(e)
                if kind == "block" and ref in ids:
                    exclude.add(e.get("id"))
        out = busy_intervals(evs, exclude)
        rows = self.db.q("SELECT b.start, b.end, b.task_id FROM blocks b JOIN tasks t ON t.id = b.task_id WHERE b.status='proposed'")
        for r in rows:
            if r["task_id"] != exclude_task_id:
                out.append((dt(r["start"]), dt(r["end"])))
        return out

    def day_load(self, exclude_task_id: int = None) -> dict:
        load = defaultdict(int)
        rows = self.db.q("SELECT b.start, b.end, b.task_id FROM blocks b JOIN tasks t ON t.id = b.task_id WHERE b.status IN ('proposed','scheduled')")
        for r in rows:
            if r["task_id"] == exclude_task_id:
                continue
            load[r["start"][:10]] += int((dt(r["end"]) - dt(r["start"])).total_seconds() // 60)
        return dict(load)

    # ---- categories ----
    def categories(self) -> list:
        return sources.categories(self.cfg)

    def category(self, cat_id: str):
        try:
            return sources.get_category(self.cfg, cat_id)
        except sources.SourceError:
            return None

    def calendar_for(self, cat_id: str) -> str:
        c = self.cfg["calendars"]
        return c["thesis"] if (cat_id or "thesis") == "thesis" else c["university"]

    def save_config(self) -> None:
        config.save(self.cfg)

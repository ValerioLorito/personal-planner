"""
calendar.py — Python side of the EventKit bridge (native/helper → plannerkit).

Everything that touches Apple Calendar goes through `Calendar`. Results are
plain dicts; failures raise CalendarError with an actionable message. Planner
owned events carry a tag line in their notes (`planner:block=12`,
`planner:habit=3`) so they are recognised even if their identifiers change.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta

from .config import APP_ROOT, expand

TAG_RE = re.compile(r"planner:(block|habit)=(\d+)")


class CalendarError(Exception):
    pass


def find_kit(cfg: dict) -> str:
    candidates = [expand(cfg.get("kit_path") or ""), os.environ.get("PLANNER_KIT", ""),
                  os.path.join(APP_ROOT, "native", "build", "plannerkit")]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ""


def tag_of(event: dict):
    m = TAG_RE.search(event.get("notes") or "")
    return (m.group(1), int(m.group(2))) if m else (None, None)


def dt(s: str) -> datetime:
    return datetime.strptime(s[:16], "%Y-%m-%dT%H:%M")


def iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def event_key(e: dict) -> str:
    """Stable key of one occurrence."""
    return "%s|%s" % (e.get("item_id") or e.get("id"), e.get("occurrence") or e.get("start"))


class Calendar:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._snap = None        # (fetched_at, from, to, events)
        self.last_error = ""

    # ---- low level ----
    @property
    def kit(self) -> str:
        return find_kit(self.cfg)

    def run(self, *args, stdin=None, timeout=40) -> dict:
        kit = self.kit
        if not kit:
            raise CalendarError("plannerkit is not built — run `bash native/build.sh`")
        cmd_args = []
        for a in args:
            if isinstance(a, (list, tuple)):
                cmd_args.extend(str(x) for x in a)
            else:
                cmd_args.append(str(a))
        cmd_name = cmd_args[0] if cmd_args else "plannerkit"
        try:
            proc = subprocess.run([kit] + cmd_args, input=(json.dumps(stdin) if stdin is not None else None),
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise CalendarError("plannerkit %s timed out" % cmd_name)
        out = (proc.stdout or "").strip().splitlines()
        try:
            data = json.loads(out[-1]) if out else {}
        except ValueError:
            raise CalendarError("plannerkit %s: unreadable output %r" % (cmd_name, (proc.stdout or proc.stderr)[:200]))
        if "error" in data:
            self.last_error = data["error"]
            raise CalendarError(data["error"])
        return data

    # ---- access ----
    def status(self) -> str:
        try:
            return self.run("status").get("auth", "unknown")
        except CalendarError as e:
            self.last_error = str(e)
            return "unavailable"

    def request_access(self) -> dict:
        return self.run("request", timeout=130)

    def calendars(self) -> list:
        try:
            return self.run("calendars").get("calendars", [])
        except CalendarError as e:
            self.last_error = str(e)
            return []

    def refresh(self) -> dict:
        try:
            res = self.run("refresh", timeout=30)
            self.invalidate()
            return res
        except CalendarError as e:
            self.last_error = str(e)
            return {"ok": False, "error": str(e)}

    def ensure(self, name: str) -> dict:
        try:
            return self.run("ensure-calendar", name)
        except CalendarError as e:
            self.last_error = str(e)
            return {"ok": False, "error": str(e)}

    def ensure_planner_calendars(self) -> list:
        from . import sources
        made = []
        c = self.cfg["calendars"]
        names = []
        for key in ("thesis", "university", "habits"):
            val = (c.get(key) or "").strip()
            if val and val not in names:
                names.append(val)
        for src in sources.get_configured_sources(self.cfg):
            val = (src.get("calendar") or "").strip()
            if val and val not in names:
                names.append(val)
        for name in names:
            r = self.ensure(name)
            if r.get("created"):
                made.append(name)
        return made

    # ---- reading ----
    def _load_disk_cache(self) -> list:
        from .config import DATA_DIR
        cache_file = os.path.join(DATA_DIR, "calendar_cache.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("events", [])
            except Exception:
                pass
        return []

    def _save_disk_cache(self, events: list) -> None:
        from .config import DATA_DIR
        cache_file = os.path.join(DATA_DIR, "calendar_cache.json")
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": datetime.now().isoformat(), "events": events}, f)
        except Exception:
            pass

    def events(self, start: datetime, end: datetime, calendars=None, refresh: bool = False) -> list:
        args = ["events", "--from", iso(start), "--to", iso(end)]
        if calendars:
            args += ["--calendars", json.dumps(list(calendars), ensure_ascii=False)]
        if refresh:
            args.append("--refresh")
        try:
            evs = self.run(*args, timeout=60).get("events", [])
            self.last_error = ""
            self._save_disk_cache(evs)
        except CalendarError as e:
            self.last_error = str(e)
            evs = self._load_disk_cache()
        ignore = {n.strip().lower() for n in self.cfg["calendars"].get("ignore", [])}
        return [e for e in evs if e.get("calendar", "").strip().lower() not in ignore]

    def window(self, max_age: float = 30.0, refresh: bool = False) -> list:
        """Events in the watcher window (cached; invalidated on live changes)."""
        w = self.cfg["watch"]
        now = datetime.now()
        start = (now - timedelta(days=w["days_back"])).replace(hour=0, minute=0, second=0, microsecond=0)
        end = (now + timedelta(days=w["days_ahead"])).replace(hour=0, minute=0, second=0, microsecond=0)
        with self._lock:
            if not refresh and self._snap and time.time() - self._snap[0] < max_age:
                return self._snap[3]
        evs = self.events(start, end, refresh=refresh)
        with self._lock:
            self._snap = (time.time(), start, end, evs)
        return evs

    def invalidate(self) -> None:
        with self._lock:
            self._snap = None

    def cached(self) -> list:
        with self._lock:
            return list(self._snap[3]) if self._snap else []

    # ---- writing ----
    def create(self, calendar: str, title: str, start: str, end: str, notes: str = "",
               recurrence=None, location: str = "") -> dict:
        body = {"calendar": calendar, "title": title, "start": start, "end": end, "notes": notes}
        if location:
            body["location"] = location
        if recurrence:
            body["recurrence"] = recurrence
        ev = self.run("create", stdin=body)["event"]
        self.invalidate()
        return ev

    def update(self, event_id: str, occurrence=None, span: str = "this",
               item_id: str = None, ext_id: str = None, **fields) -> dict:
        body = {"id": event_id, "span": span}
        if occurrence:
            body["occurrence"] = occurrence
        if item_id:
            body["item_id"] = item_id
        if ext_id:
            body["ext_id"] = ext_id
        body.update(fields)
        ev = self.run("update", stdin=body)["event"]
        self.invalidate()
        return ev

    def delete(self, event_id: str, occurrence=None, span: str = "this",
               item_id: str = None, ext_id: str = None, start: str = None) -> bool:
        body = {"id": event_id, "span": span}
        if occurrence:
            body["occurrence"] = occurrence
        if item_id:
            body["item_id"] = item_id
        if ext_id:
            body["ext_id"] = ext_id
        if start:
            body["start"] = start
        try:
            self.run("delete", stdin=body)
        except CalendarError as e:
            if "not found" in str(e):
                return False
            raise
        finally:
            self.invalidate()
        return True

    def pdf_text(self, path: str, max_chars: int = 60000) -> dict:
        return self.run("pdf-text", path, str(max_chars), timeout=45)


def busy_intervals(events: list, exclude_ids=()) -> list:
    """(start, end) datetimes of timed events, minus the given event ids."""
    out = []
    ex = set(exclude_ids)
    for e in events:
        if e.get("all_day") or not e.get("start") or not e.get("end"):
            continue
        if e.get("id") in ex:
            continue
        out.append((dt(e["start"]), dt(e["end"])))
    return out

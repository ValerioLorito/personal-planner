"""
tools.py — the tools agents may call. All file tools are read-only and
sandboxed to a category root (see sources.resolve).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .. import memory, sources
from ..calendar import dt, tag_of
from .runtime import Tool


def fmt_event(e: dict) -> str:
    kind, _ = tag_of(e)
    s = dt(e["start"])
    if e.get("all_day"):
        when = "%s (all day)" % s.strftime("%a %Y-%m-%d")
    else:
        when = "%s %s–%s" % (s.strftime("%a %Y-%m-%d"), s.strftime("%H:%M"), e["end"][11:16])
    mark = {"block": " (planner work block)", "habit": " (habit)"}.get(kind, "")
    loc = (" @ " + e["location"]) if e.get("location") else ""
    return "%s [%s] %s%s%s" % (when, e.get("calendar", "").strip(), e.get("title", ""), loc, mark)


def source_tools(app) -> list:
    cfg = app.cfg

    def cat(category):
        return sources.get_category(cfg, category)

    def list_dir(category, path="", depth=1):
        return sources.list_dir(cfg, cat(category), path, depth=depth)

    def read_file(category, path, offset=0, max_chars=3500):
        return sources.read_file(cfg, cat(category), path, calendar=app.cal,
                                 max_chars=max(500, min(int(max_chars), 5000)), offset=int(offset or 0))

    def search(category, query, glob=""):
        return sources.search(cfg, cat(category), query, calendar=app.cal, glob=glob)

    return [
        Tool("list_dir", 'list_dir(category, path="", depth=1)',
             "list a folder of a category (depth 1–4). Paths are relative to the category root.", list_dir),
        Tool("read_file", 'read_file(category, path, offset=0, max_chars=9000)',
             "read a file's text (code, markdown, PDF, PPTX, DOCX, notebooks). Use offset to page.", read_file),
        Tool("search", 'search(category, query, glob="")',
             "case-insensitive search in file names and contents (PDF text included); "
             "use a|b for alternatives; glob filters paths, e.g. '*.py'.", search),
    ]


def calendar_tools(app) -> list:
    def calendar_events(start, end, calendars=None, query=""):
        s = datetime.strptime(str(start)[:10], "%Y-%m-%d")
        e = datetime.strptime(str(end)[:10], "%Y-%m-%d") + timedelta(days=1)
        if (e - s).days > 120:
            return "ERROR: range too large (max 120 days)"
        names = calendars if isinstance(calendars, list) and calendars else None
        evs = app.cal.events(s, e, names)
        q = (query or "").lower()
        if q:
            terms = [t for t in q.split("|") if t]
            evs = [x for x in evs if any(t in (x.get("title", "") + " " + x.get("notes", "")).lower()
                                         for t in terms)]
        lines = [fmt_event(x) for x in evs[:120]]
        return "\n".join(lines) or "(no events)"

    return [Tool("calendar_events", 'calendar_events(start, end, calendars=null, query="")',
                 "events between two dates (YYYY-MM-DD, inclusive) from all calendars or the given list; "
                 "query filters titles/notes (a|b alternatives). Use it to find deadlines, exams, lectures.",
                 calendar_events)]


def memory_tools(app, allowed_scopes: list) -> list:
    def remember(scope, text, replace_id=None):
        if scope not in allowed_scopes:
            return "ERROR: scope must be one of %s" % allowed_scopes
        bid = memory.remember(app.db, scope, text, replace_id=replace_id)
        app.db.log("belief", "Learned: %s" % text, {"scope": scope, "id": bid})
        return "stored as b%d" % bid

    return [Tool("remember", "remember(scope, text, replace_id=null)",
                 "store or revise a durable belief about the user (scopes: %s). Use replace_id to update "
                 "an existing belief instead of duplicating it." % ", ".join(allowed_scopes), remember)]

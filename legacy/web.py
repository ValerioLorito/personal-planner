"""
web.py — local single-page web UI for the thesis planner.

Standard library only: http.server, json, urllib.parse. No CSS/JS assets, no
framework, no pip installs. The whole page is one HTML string served from "/".

    python3 -m planner.web                 # http://127.0.0.1:8765/
    python3 -m planner.web --port 9000 --open
    python3 -m planner.web --db /tmp/x.db --config /tmp/config.json

JSON API (all bodies are JSON objects):

    GET  /                      the single page
    GET  /api/tasks?status=     {"tasks": [...], "count": n}
    POST /api/tasks             create -> 201 {"id": n, "task": {...}}
    GET  /api/tasks/<id>        {"task": {...}}
    POST /api/tasks/<id>        partial update -> {"task": {...}}
    POST /api/tasks/<id>/status {"status", "note"} -> {"task": {...}}
                                (status "dropped" also unschedules the task's
                                blocks and deletes their calendar events)
    DELETE /api/tasks/<id>      remove the task + its blocks + their calendar
                                events -> {"removed", "unscheduled"}
    GET  /api/plan?week=        saved plan for that week (or empty)
    POST /api/schedule          {"week", "save"} -> schedule_week() result
    GET  /api/log?limit=        recent events
    GET  /api/history?from=&to= history rows
    GET  /api/stats             store stats + a few derived counters
    GET  /api/today             today's blocks, next block, day counters
    GET  /widget                compact single-column view of /api/today
    GET  /api/habits            {"habits": [...templates...], "week",
                                "days": [{date, weekday, slots}], "calendar"}
    POST /api/habits            create one habit template -> {"habit": {...}};
                                the rolling week re-materializes at once
    POST /api/habits/<id>       update a template -> {"habit": {...}}
    DELETE /api/habits/<id>     delete for real: template, all instances
                                (past included) and their mirror events
    POST /api/habits/sync       re-materialize + re-mirror now (heals a
                                wiped or hand-edited Habits calendar)
    POST /api/habits/slots/<id> {"done": true|false} or {"skipped": true|false}
    POST /api/habits/parse      {"text"} -> ONE LLM call -> {"proposals": [...]}
                                (proposals only; nothing is saved)
    POST /api/habits/save       {"habits": [proposals...]} -> {"saved": [...]}
    POST /api/review            run one plan review now (ONE LLM call, gated
                                verdicts) -> the report stored under kv
                                "review:last", also shown by GET /api/state

Design notes
------------
* The HTTP server is threaded (``ThreadingMixIn`` + ``HTTPServer``). It used to
  be single-threaded, which looked simpler but deadlocked real browsers: a
  WebView preconnects *before* it navigates, so the accept loop sat waiting for
  a request line on one idle socket while the navigation request queued behind
  it — the page then failed with "The request timed out". One thread per
  connection plus a socket timeout (``PlannerHandler.timeout``) removes both
  the deadlock and the starvation. sqlite connections are bound to the thread
  that opened them, so each handler thread lazily opens its own connection to
  the same file (``PlannerHTTPServer.store``).
* Scheduling treats every already-saved slot in the week as busy, so the
  planner never double-books a block that is already on the calendar. That
  means a week that already has a plan needs the old plan considered — the UI
  shows the saved plan next to the grid.
* Validation lives here, not in store.py: bad input gets a 400 with a
  readable ``{"error": ...}`` instead of a sqlite error.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

if __package__ in (None, ""):  # allow `python3 planner/web.py` as well as -m
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from planner import auto as auto_mod
from planner import config as config_mod
from planner.scheduler import parse_hhmm, schedule_week, week_start_for, week_dates
from planner.store import Store

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
DEFAULT_DB = os.path.join(DATA_DIR, "planner.db")
DEFAULT_CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

COMPLEXITIES = ("S", "M", "L")
STATUSES = ("todo", "doing", "done", "blocked", "dropped")

TASK_PATH = re.compile(r"^/api/tasks/(\d+)$")
STATUS_PATH = re.compile(r"^/api/tasks/(\d+)/status$")
FEEDBACK_PATH = re.compile(r"^/api/tasks/(\d+)/feedback$")
SLOT_PATH = re.compile(r"^/api/slots/(\d+)$")
HABIT_PATH = re.compile(r"^/api/habits/(\d+)$")
HABIT_SLOT_PATH = re.compile(r"^/api/habits/slots/(\d+)$")
MEETING_PATH = re.compile(r"^/api/meeting$")
_HHMM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class BadRequest(HttpError):
    def __init__(self, message):
        super().__init__(400, message)


class NotFound(HttpError):
    def __init__(self, message="not found"):
        super().__init__(404, message)


class MethodNotAllowed(HttpError):
    def __init__(self, allowed):
        super().__init__(405, "method not allowed (use %s)" % allowed)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _int(value, name, default=None):
    if value is None or value == "":
        if default is not None:
            return default
        raise BadRequest("%s is required" % name)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise BadRequest("%s must be an integer" % name)


def _iso_date(value, name):
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except (TypeError, ValueError):
        raise BadRequest("%s must be YYYY-MM-DD" % name)


def _week_start(value):
    """Accept any ISO date in the week and return that week's Monday."""
    if not value:
        return week_start_for()
    return week_start_for(date.fromisoformat(_iso_date(value, "week")))


def _query_one(query, key, default=None):
    values = query.get(key)
    if not values:
        return default
    return values[0]


def calendar_busy(week, timeout=0.0):
    """{date: [(start_min, end_min)]} from the calendar snapshot.

    Never blocks unless asked: `timeout` waits for an in-flight background read
    (an explicit "Schedule" click can afford a few seconds to get this right).
    """
    week = week if isinstance(week, str) else week.isoformat()
    state = _calendar_state(week)
    if timeout and state.get("loading"):
        thread = _CAL["thread"]
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            state = _calendar_state(week, allow_refresh=False)
    busy = {}
    for ev in state.get("events") or []:
        if ev.get("all_day"):
            continue
        try:
            start = parse_hhmm(ev["start"])
            end = parse_hhmm(ev["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            busy.setdefault(ev["date"], []).append((start, end))
    return busy


def busy_by_date(store, week_start, include_calendar=True, wait=0.0,
                 pinned_only=False):
    """Saved slots for the week as {"YYYY-MM-DD": [(start_min, end_min), ...]}.

    Uses scheduler.parse_hhmm so the HH:MM -> minutes conversion matches the
    planner exactly. Slots with a broken time are skipped rather than crashing
    the request.

    `pinned_only` (used by re-plans) keeps only the slots the user moved by
    hand: they must be respected and carried over, while every other block is
    rebuilt from scratch instead of squeezing the new plan into the gaps of
    the one it replaces.
    """
    first = week_start.isoformat()
    last = (week_start + timedelta(days=6)).isoformat()
    busy = {}
    for slot in store.slots_between(first, last):
        if pinned_only and not slot.get("pinned"):
            continue
        try:
            start = parse_hhmm(slot["start"])
            end = parse_hhmm(slot["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        busy.setdefault(slot["date"], []).append((start, end))
    if include_calendar:
        for day, spans in calendar_busy(week_start, timeout=wait).items():
            busy.setdefault(day, []).extend(spans)
    for spans in busy.values():
        spans.sort()
    return busy


def clean_task_fields(data, creating):
    """Validate an incoming task payload -> kwargs for add_task/update_task."""
    if not isinstance(data, dict):
        raise BadRequest("body must be a JSON object")
    out = {}

    if creating or "title" in data:
        title = str(data.get("title") or "").strip()
        if not title:
            raise BadRequest("title is required")
        out["title"] = title[:300]

    for key in ("notes", "area", "files"):
        if key in data:
            out[key] = str(data.get(key) or "").strip()[:4000]

    if creating or "complexity" in data:
        complexity = str(data.get("complexity") or "M").strip().upper()
        if complexity not in COMPLEXITIES:
            raise BadRequest("complexity must be one of S, M, L")
        out["complexity"] = complexity

    if creating or "priority" in data:
        priority = _int(data.get("priority"), "priority", default=2)
        if not 1 <= priority <= 3:
            raise BadRequest("priority must be 1 (high), 2 or 3 (low)")
        out["priority"] = priority

    if creating or "est_minutes" in data:
        minutes = _int(data.get("est_minutes"), "est_minutes", default=60)
        if not 0 < minutes <= 10000:
            raise BadRequest("est_minutes must be between 1 and 10000")
        out["est_minutes"] = minutes

    if creating or "deadline" in data:
        raw = data.get("deadline")
        out["deadline"] = None if raw in (None, "") else _iso_date(raw, "deadline")

    if "within_weeks" in data:
        raw = data.get("within_weeks")
        if raw in (None, ""):
            out["within_weeks"] = None        # no window: any week is fair game
        else:
            weeks = _int(raw, "within_weeks", default=1)
            if not 1 <= weeks <= 26:
                raise BadRequest("within_weeks must be between 1 and 26")
            out["within_weeks"] = weeks

    if "status" in data:
        status = str(data.get("status") or "").strip()
        if status not in STATUSES:
            raise BadRequest("status must be one of %s" % ", ".join(STATUSES))
        out["status"] = status

    return out


def _task_or_404(store, task_id):
    task = store.get_task(task_id)
    if not task:
        raise NotFound("no task with id %s" % task_id)
    return task


# --------------------------------------------------------------------------- #
# API handlers
# --------------------------------------------------------------------------- #
def api_list_tasks(store, query):
    status = _query_one(query, "status")
    if status and status not in STATUSES:
        raise BadRequest("status must be one of %s" % ", ".join(STATUSES))
    tasks = store.list_tasks(status=status or None)
    return {"tasks": tasks, "count": len(tasks)}


def api_add_task(store, data, cfg=None):
    fields = clean_task_fields(data, creating=True)
    task_id = store.add_task(**fields)
    brain_result = None
    if data.get("enrich") and cfg is not None:
        try:
            from planner.brain import BrainError, enrich_task
            brain_result = enrich_task(store, cfg, task_id)
        except BrainError as e:          # never fail creation because of the brain
            brain_result = {"status": "error", "error": str(e)}
    return 201, {"id": task_id, "task": store.get_task(task_id),
                 "brain": brain_result}


def api_update_task(store, task_id, data):
    _task_or_404(store, task_id)
    fields = clean_task_fields(data, creating=False)
    if fields:
        store.update_task(task_id, **fields)
    return {"task": store.get_task(task_id)}


def api_set_status(store, cfg, task_id, data):
    _task_or_404(store, task_id)
    status = str((data or {}).get("status") or "").strip()
    if status not in STATUSES:
        raise BadRequest("status must be one of %s" % ", ".join(STATUSES))
    note = str((data or {}).get("note") or "")[:500]
    unscheduled = _unschedule_task(store, cfg, task_id) \
        if status == "dropped" else None
    store.set_status(task_id, status, note)
    return {"task": store.get_task(task_id), "status": status,
            "unscheduled": unscheduled}


def _unschedule_task(store, cfg, task_id):
    """Remove every saved block of one task and delete its Apple Calendar
    events (best-effort, same contract as single-slot sync: failures become
    data, never exceptions — the DB change already won). Returns a report."""
    slots = store.slots_for_task(task_id)
    report = {"blocks": len(slots), "calendar_removed": 0, "calendar_errors": 0}
    for slot in slots:
        uid = slot.get("calendar_event_id")
        store.delete_slot(slot["id"])
        rep = _slot_sync_calendar(cfg, "delete", uid, slot["date"],
                                  slot["start"], slot["end"])
        if rep.get("deleted"):
            report["calendar_removed"] += 1
        elif rep.get("error"):
            report["calendar_errors"] += 1
    return report


def api_task_delete(store, cfg, task_id):
    """Remove the task entirely: every block comes off the plan, its Apple
    Calendar events are deleted, the record itself is erased (a history
    'task_deleted' event keeps the breadcrumb)."""
    task = _task_or_404(store, task_id)
    unscheduled = _unschedule_task(store, cfg, task_id)
    store.delete_task(task_id)
    return {"removed": True, "task": task, "unscheduled": unscheduled}


def api_task_past(store, data):
    """Register a task completed before this app existed. It lands as done
    with backdated doing→done events, so the real time counts for
    calibration exactly like a task tracked live."""
    data = data or {}
    title = str(data.get("title") or "").strip()
    if not title:
        return {"error": "title is required"}, 400
    try:
        est = int(data.get("est_minutes") or 60)
        actual = data.get("actual_minutes")
        actual = int(actual) if actual not in (None, "") else None
    except (TypeError, ValueError):
        return {"error": "est_minutes/actual_minutes must be integers"}, 400
    if est <= 0 or (actual is not None and actual <= 0):
        return {"error": "minutes must be positive"}, 400
    done = str(data.get("done") or "").strip() or None
    if done:
        try:
            from datetime import date as _date
            _date.fromisoformat(done)
        except ValueError:
            return {"error": "done must be YYYY-MM-DD"}, 400
    tid = store.add_past_task(
        title[:200], area=str(data.get("area") or "").strip()[:60],
        est_minutes=est, actual_minutes=actual, done_date=done,
        priority=int(data.get("priority") or 2),
        complexity=str(data.get("complexity") or "M"),
        notes=str(data.get("notes") or "")[:500])
    ratio = None
    try:
        from planner.brain import update_calibration
        ratio = update_calibration(store)
    except Exception:
        pass
    return {"task": store.get_task(tid), "calibration": ratio}


def api_plan(store, query):
    week = _week_start(_query_one(query, "week"))
    key = week.isoformat()
    plan = store.get_plan(key)
    return {
        "week": key,
        "exists": plan is not None,
        "plan_id": plan["id"] if plan else None,
        "created_at": plan["created_at"] if plan else None,
        "note": plan["note"] if plan else "",
        "slots": plan["slots"] if plan else [],
    }


def api_schedule(store, cfg, data):
    data = data or {}
    week = _week_start(data.get("week"))
    save = bool(data.get("save", True))
    tasks = store.list_tasks()
    # Waiting for a fresh calendar read is a UI choice (it costs seconds); the
    # API default stays instant so tests and scripts never block. Re-plans
    # start fresh — only the user's pinned slots are busy, and save_plan()
    # carries them over so a manual correction survives the re-plan.
    try:
        wait = min(30.0, max(0.0, float(data.get("wait") or 0)))
    except (TypeError, ValueError):
        wait = 0.0
    busy = busy_by_date(store, week, wait=wait, pinned_only=True)
    # Hand-placing into a later week must respect each task's planning window
    # (within_weeks): the UI's week switch is a deliberate choice, but a task
    # promised to the current week does not silently jump three weeks ahead.
    weeks_out = max(0, (week - week_start_for()).days // 7)
    result = schedule_week(tasks, week, busy, cfg, weeks_out=weeks_out)
    result["calendar_events"] = sum(len(v) for v in busy.values())
    result["saved_plan_id"] = None
    # Saving a plan with zero blocks just clutters the log and makes the week
    # look "planned"; only persist when there is something to place.
    saved = bool(save and result["slots"])
    stale_ids = set()
    if saved:
        stale_ids = _week_calendar_ids(store, result["week"])
        result["saved_plan_id"] = store.save_plan(result["week"], result["slots"])
        stale_ids -= _week_calendar_ids(store, result["week"])   # carried pins
    result["saved"] = saved
    result["note"] = ("nothing to schedule — add a task first"
                      if save and not result["slots"] else "")
    if saved and data.get("write_calendar"):
        result["calendar_written"] = _write_week_events(store, cfg, result,
                                                        stale_ids)
    return result


def _week_calendar_ids(store, week):
    """Calendar event uids currently referenced by the saved plan of `week`."""
    plan = store.get_plan(week) if hasattr(store, "get_plan") else None
    if not plan:
        return set()
    return {s["calendar_event_id"] for s in plan["slots"]
            if s.get("calendar_event_id")}


def _slot_description(store, slot, task):
    """Apple Calendar description for one block: the planning rationale, the
    task number, and the subtasks this block covers — the named subtask when
    the agent split the task across blocks, else the task's whole list."""
    lines = []
    rationale = (slot.get("rationale") or "").strip()
    if rationale:
        lines.append("%.60s" % rationale)
    lines.append("Task #%s" % slot.get("task_id"))
    subs = task.get("subtasks") or []
    if isinstance(subs, str):
        try:
            subs = json.loads(subs)
        except (ValueError, TypeError):
            subs = []
    if not isinstance(subs, list):
        subs = []
    named = (slot.get("subtask") or "").strip()
    if named:
        match = next((x for x in subs
                      if str(x.get("title") or "") == named), None)
        if match:
            mins = match.get("minutes") or match.get("est_minutes") or 0
            lines.append("Subtask: %s (%s min)" % (match.get("title"), mins))
        else:
            lines.append("Subtask: %s" % named)
    elif subs:
        lines.append("Subtasks:")
        for x in subs:
            name = str(x.get("title") or "").strip()
            if name:
                mins = x.get("minutes") or x.get("est_minutes") or 0
                lines.append("  • %s (%s min)" % (name, mins))
    return "\n".join(lines)


def _write_week_events(store, cfg, result, stale_ids=()):
    """Submit step: create one Apple Calendar event per planned block.

    Best-effort, like every calendar write: a denied Automation grant or a
    missing calendar is reported as data, never an exception — the plan is
    already saved in the DB by the time this runs. `stale_ids` (events of the
    plan this one replaces, minus carried-over pinned blocks) are deleted
    afterwards, so repeated submits do not litter the calendar.
    """
    report = {"created": 0, "removed": 0, "failed": 0, "error": None}
    try:
        from planner.calendar_mac import delete_event, event_summary, write_event
    except Exception as exc:
        report["error"] = "calendar write unavailable: %s" % exc
        return report
    cal = (cfg or {}).get("calendar_name") or "Thesis"
    for s in result["slots"]:
        if s.get("calendar_event_id"):
            continue                    # carried pinned block: event lives
        task = store.get_task(s["task_id"]) or {}
        try:
            uid = write_event(
                event_summary(task, cfg),
                "%sT%s:00" % (s["date"], s["start"]),
                "%sT%s:00" % (s["date"], s["end"]),
                cal,
                description=_slot_description(store, s, task))
            if uid:
                report["created"] += 1
                if s.get("id"):
                    store.set_slot_calendar_id(s["id"], uid)
            else:
                report["failed"] += 1
                report["error"] = "no event uid came back"
        except Exception as exc:
            report["failed"] += 1
            report["error"] = str(exc)
    for uid in stale_ids:               # replaced plan's leftovers, if any
        try:
            rep = delete_event(uid, calendar_name=cal)
            if rep.get("deleted"):
                report["removed"] += 1
            elif rep.get("error") and not report["error"]:
                report["error"] = rep["error"]
        except Exception:
            pass
    return report


def api_log(store, query):
    limit = _int(_query_one(query, "limit"), "limit", default=50)
    limit = max(1, min(limit, 500))
    events = store.recent_events(limit)
    return {"events": events, "count": len(events)}


def api_history(store, query):
    date_from = _query_one(query, "from")
    date_to = _query_one(query, "to")
    limit = _int(_query_one(query, "limit"), "limit", default=2000)
    rows = store.list_history(
        _iso_date(date_from, "from") if date_from else None,
        _iso_date(date_to, "to") if date_to else None,
        max(1, min(limit, 5000)))
    return {"history": rows, "count": len(rows)}


def api_stats(store):
    stats = store.stats()
    tasks = store.list_tasks()
    today = date.today().isoformat()
    open_tasks = [t for t in tasks if t["status"] in ("todo", "doing")]
    stats.update({
        "tasks": len(tasks),
        "open_tasks": len(open_tasks),
        "open_minutes": sum(int(t.get("est_minutes") or 0) for t in open_tasks),
        "overdue": sorted(t["id"] for t in open_tasks
                          if t.get("deadline") and t["deadline"] < today),
        "today": today,
        "week": week_start_for().isoformat(),
        "estimated": sum(1 for t in tasks if t.get("est_source")),
        "unevaluated": sum(1 for t in open_tasks if not t.get("est_source")),
    })
    return stats


# --------------------------------------------------------------------------- #
# today / widget
# --------------------------------------------------------------------------- #
def _hhmm_minutes(value):
    """HH:MM -> minutes since midnight, or None when the value is unusable."""
    try:
        return parse_hhmm(value)
    except (KeyError, TypeError, ValueError):
        return None


def _slot_subtask(slot):
    """The chunk label of a saved slot.

    schedule_week() puts it in the slot dict, but the slots table has no
    subtask column; save_plan() keeps it inside the rationale ("covers: ...").
    """
    label = str(slot.get("subtask") or "").strip()
    if label:
        return label
    for part in str(slot.get("rationale") or "").split(";"):
        part = part.strip()
        if part.startswith("covers:"):
            return part[len("covers:"):].strip()
    return ""


def _today_slots(plan, tasks, today):
    """Today's slots from a saved plan, annotated with task title and status."""
    slots = []
    for slot in (plan or {}).get("slots", []):
        if slot.get("date") != today:
            continue
        try:
            minutes = int(slot.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        task = tasks.get(slot.get("task_id")) or {}
        slots.append({
            "task_id": slot.get("task_id"),
            "title": task.get("title") or "task #%s" % slot.get("task_id"),
            "start": str(slot.get("start") or ""),
            "end": str(slot.get("end") or ""),
            "minutes": minutes,
            "status": task.get("status") or "",
            "subtask": _slot_subtask(slot),
        })

    def sort_key(slot):
        start = _hhmm_minutes(slot["start"])
        return (start if start is not None else 24 * 60, slot["start"])

    slots.sort(key=sort_key)
    return slots


def _work_window_left(cfg, now):
    """Minutes still usable today inside the configured day window.

    A non-work day has no window at all, so nothing is left to plan into.
    """
    if now.weekday() not in ((cfg or {}).get("work_days") or []):
        return 0
    start = _hhmm_minutes((cfg or {}).get("day_start"))
    end = _hhmm_minutes((cfg or {}).get("day_end"))
    if start is None or end is None or end <= start:
        return 0
    return max(0, end - max(start, now.hour * 60 + now.minute))


def api_today(store, cfg=None, now=None):
    """Today's saved blocks plus the counters a small always-on view needs.

    The plan is read through store.get_plan() with this week's Monday, i.e.
    exactly the plan /api/plan shows, so the SQL is not repeated here.
    """
    cfg = cfg or config_mod.DEFAULT_CONFIG
    now = now or datetime.now()
    today = now.date().isoformat()
    week = _week_start(today)
    plan = store.get_plan(week.isoformat())
    tasks = {t["id"]: t for t in store.list_tasks()}
    slots = _today_slots(plan, tasks, today)

    now_min = now.hour * 60 + now.minute
    upcoming = None
    for slot in slots:
        end = _hhmm_minutes(slot["end"])
        if end is not None and end > now_min:   # a running block still counts
            upcoming = slot
            break

    # If today is finished, a small view is still more useful showing the next
    # block this week than an empty card.
    later = None
    if upcoming is None and plan:
        for slot in sorted(plan["slots"], key=lambda s: (s["date"], s["start"])):
            if slot["date"] > today:
                later = dict(slot)
                break

    planned = sum(slot["minutes"] for slot in slots)
    if upcoming is not None:
        nxt = {"start": upcoming["start"], "end": upcoming["end"],
               "title": upcoming["title"], "minutes": upcoming["minutes"],
               "date": today, "is_today": True}
    elif later is not None:
        task = tasks.get(later["task_id"]) or {}
        nxt = {"start": later["start"], "end": later["end"],
               "title": task.get("title") or "(task removed)",
               "minutes": later["minutes"],
               "date": later["date"], "is_today": False}
    else:
        nxt = None
    return {
        "date": today,
        "planned_minutes": planned,
        "blocks": len(slots),
        "slots": slots,
        "next": nxt,
        "open_tasks": sum(1 for t in tasks.values()
                          if t["status"] in ("todo", "doing")),
        "free_minutes_left": max(0, _work_window_left(cfg, now) - planned),
    }


# --------------------------------------------------------------------------- #
# agentic + calendar state for the single-page UI
# --------------------------------------------------------------------------- #
# Reading Apple Calendar shells out to osascript, which can block for as long as
# macOS takes to answer (a permission prompt can stall it indefinitely). The UI
# must never wait for that, so a background thread fills a cache and requests
# return the previous snapshot immediately.
_CAL = {"week": None, "data": None, "ts": 0.0, "thread": None}
_CAL_TTL = 30.0


CALENDAR_TIMEOUT = 20.0
CALENDAR_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "data", "calendar_cache.json")


def _cache_read(week):
    try:
        with open(CALENDAR_CACHE, encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if blob.get("week") != week:
        return None
    return blob


def _cache_write(week, payload):
    # Guard against a stray week (typo, stale client) poisoning the snapshot.
    try:
        if abs((date.fromisoformat(week) - date.today()).days) > 28:
            return
    except ValueError:
        return
    try:
        os.makedirs(os.path.dirname(CALENDAR_CACHE), exist_ok=True)
        with open(CALENDAR_CACHE, "w", encoding="utf-8") as fh:
            json.dump({"week": week, "fetched_at": datetime.now().isoformat(
                timespec="seconds"), **payload}, fh, ensure_ascii=False)
    except OSError:
        pass


def _calendar_fetch(week):
    """Read the week in a subprocess with a hard timeout.

    In-process Apple Events block until macOS answers a TCC prompt, which can
    be forever for a background server; a subprocess can simply be killed.
    The probe writes to a file rather than a pipe for the same reason.
    """
    start = date.fromisoformat(week) if isinstance(week, str) else week
    end = start + timedelta(days=6)
    import subprocess
    import tempfile

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fd, path = tempfile.mkstemp(prefix="calendar_", suffix=".json")
    os.close(fd)
    try:
        subprocess.run(
            [sys.executable, "-m", "planner.calendar_probe",
             start.isoformat(), end.isoformat(), "--out", path],
            cwd=root, timeout=CALENDAR_TIMEOUT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=dict(os.environ, PYTHONPATH=root))
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except subprocess.TimeoutExpired:
        cached = _cache_read(week if isinstance(week, str) else week.isoformat())
        if cached:
            return {"available": True, "events": cached.get("events") or [],
                    "calendars": cached.get("calendars") or [],
                    "cached_at": cached.get("fetched_at"),
                    "error": None}
        return {"available": False, "events": [], "calendars": [],
                "error": ("Calendar did not answer in %ds. Run "
                          "`python3 -m planner.cli calendar-sync` in a terminal to "
                          "grant access and cache the week."
                          % int(CALENDAR_TIMEOUT))}
    except Exception as exc:
        return {"available": False, "events": [], "calendars": [],
                "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    result = {"available": not payload.get("error"),
              "error": payload.get("error"),
              "events": payload.get("events") or [],
              "calendars": payload.get("calendars") or []}
    if not result["error"]:
        _cache_write(start.isoformat(), {"events": result["events"],
                                         "calendars": result["calendars"]})
    return result


def _calendar_refresh(week):
    try:
        data = _calendar_fetch(week)
    except Exception as exc:
        data = {"available": False, "error": str(exc), "events": [], "calendars": []}
    _CAL.update(week=week, data=data, ts=time.time())


def _calendar_state(week, allow_refresh=True):
    """Never block: serve the cached snapshot, refresh in the background."""
    week = week if isinstance(week, str) else week.isoformat()
    cached = _CAL["data"] if _CAL["week"] == week else None
    if cached is not None and cached.get("error"):
        # A failed read is not a fact about the week: prefer the last good
        # snapshot on disk over remembering "calendar is broken" for 2 minutes.
        disk = _cache_read(week)
        if disk:
            cached = {"available": True, "error": None,
                      "events": disk.get("events") or [],
                      "calendars": disk.get("calendars") or [],
                      "cached_at": disk.get("fetched_at"), "stale": True}
            _CAL["data"] = cached
        else:
            cached = None
    fresh = cached is not None and (time.time() - _CAL["ts"]) < _CAL_TTL
    if not fresh and allow_refresh:
        thread = _CAL["thread"]
        if thread is None or not thread.is_alive():
            t = threading.Thread(target=_calendar_refresh, args=(week,), daemon=True)
            _CAL["thread"] = t
            t.start()
    if cached is not None:
        out = dict(cached)
        out["stale"] = fresh is False
        return out
    # No in-memory snapshot yet: the on-disk cache still beats an empty rail,
    # and a refresh is already running behind this answer.
    disk = _cache_read(week)
    if disk:
        _CAL.update(week=week, data={"available": True, "error": None,
                                     "events": disk.get("events") or [],
                                     "calendars": disk.get("calendars") or []},
                    ts=0.0)
        return {"available": True, "error": None,
                "events": disk.get("events") or [],
                "calendars": disk.get("calendars") or [],
                "cached_at": disk.get("fetched_at"), "stale": True}
    return {"available": False, "error": "reading calendar…", "events": [],
            "calendars": [], "loading": True}


def calendar_snapshot(week):
    """Blocking read, used by tests and by explicit CLI commands."""
    try:
        return _calendar_fetch(week if isinstance(week, str) else week.isoformat())
    except Exception as exc:
        return {"available": False, "error": str(exc), "events": [], "calendars": []}


def _calibration_state(store):
    try:
        from planner import intake
        return intake.calibration_summary(store)
    except Exception:
        ratio = store.kv_get("calibration:ratio")
        return {"ratio": float(ratio) if ratio else 1.0, "samples": 0,
                "by_area": {}, "recent": []}


def _review_state(store):
    """The last periodic review report (kv "review:last"), or None."""
    try:
        from planner import review as review_mod
        return review_mod.last_review(store)
    except Exception:
        return None


def api_review(store, cfg, data):
    """POST: run one plan review now (same gate? no — the user asked).

    A manual review skips the every_hours gate but keeps the shape: signals,
    ONE LLM call, validated verdicts, safe applications. Force=True so the
    user can always ask "review it now" from the card.
    """
    del data
    try:
        from planner import review as review_mod
    except Exception as exc:
        return {"error": "review module unavailable: %s" % exc}, 503
    report = review_mod.run_review(store, cfg, force=True)
    if report.get("error"):
        return report, 200
    return report


def _llm_ready(cfg):
    """'API estimate' is attemptable: base_url + model set, and a key that
    resolves (env var or brain.llm.key_file) — or an explicitly keyless
    endpoint (brain.llm.api_key_env = ""). Anything less fails soft to the
    heuristic anyway; readiness is exactly what /api/state exposes."""
    llm = (cfg.get("brain") or {}).get("llm") or {}
    base = str(llm.get("base_url") or "").strip()
    model = str(llm.get("model") or "").strip()
    if not (base and model):
        return False
    from planner import brain as _brain
    return _brain.llm_key_source(cfg)[1] in ("env", "file", "keyless")


def _categories(cfg):
    """Task categories: Thesis + the subject folders under university_root."""
    try:
        from planner import sources
        return sources.categories(cfg)
    except Exception:
        return ["Thesis"]


def _university_root(cfg):
    """Whether the university folder is wired up (for UI hints)."""
    try:
        from planner import sources
        return sources.university_root(cfg)
    except Exception:
        return ""


def api_state(store, cfg, query):
    """Everything the single-page UI needs, in one round-trip."""
    import calendar as _cal
    week = _week_start(_query_one(query, "week"))
    today = date.today()
    now = datetime.now()

    tasks = []
    for t in store.list_tasks():
        row = dict(t)
        try:
            row["subtasks"] = json.loads(row.get("subtasks") or "[]")
        except (json.JSONDecodeError, TypeError):
            row["subtasks"] = []
        tasks.append(row)

    plan = store.get_plan(week.isoformat())
    slots = []
    if plan:
        by_id = {t["id"]: t for t in tasks}
        for s in plan["slots"]:
            task = by_id.get(s["task_id"]) or {}
            slots.append({
                "id": s["id"],
                "task_id": s["task_id"],
                "pinned": int(s.get("pinned") or 0),
                "title": task.get("title") or "(removed task)",
                "status": task.get("status") or "todo",
                "date": s["date"], "start": s["start"], "end": s["end"],
                "minutes": s["minutes"],
                "subtask": s.get("subtask") or "",
                "rationale": s.get("rationale") or "",
            })

    open_tasks = [t for t in tasks if t["status"] in ("todo", "doing")]
    cal_state = _calendar_state(week)

    busy = {}
    for d in week_dates(week):
        key = d.isoformat()
        spans = [[s["start"], s["end"]] for s in slots if s["date"] == key]
        busy[key] = spans

    return {
        "today": today.isoformat(),
        "now": now.strftime("%H:%M"),
        "week": week.isoformat(),
        "week_days": [{
            "date": d.isoformat(),
            "label": d.strftime("%a"),
            "num": d.day,
            "is_today": d == today,
            "is_weekend": d.weekday() >= 5,
        } for d in week_dates(week)],
        "tasks": tasks,
        "slots": slots,
        "calendar": cal_state,
        "busy": busy,
        "stats": {
            "open_tasks": len(open_tasks),
            "open_minutes": sum(int(t.get("est_minutes") or 0) for t in open_tasks),
            "planned_minutes": sum(s["minutes"] for s in slots),
            "blocks": len(slots),
            "overdue": sorted(t["id"] for t in open_tasks
                              if t.get("deadline") and t["deadline"] < today.isoformat()),
        },
        "calibration": _calibration_state(store),
        "review": _review_state(store),
        "agent": {
            "estimated": sum(1 for t in tasks if t.get("est_source")),
            "unevaluated": sum(1 for t in open_tasks if not t.get("est_source")),
            "backend": ((cfg.get("brain") or {}).get("backend") or "llm"),
        },
        "settings": {
            "day_start": cfg.get("day_start"),
            "day_end": cfg.get("day_end"),
            "max_block_min": cfg.get("max_block_min"),
            "deep_min_block": cfg.get("deep_min_block"),
            "horizon_weeks": max(1, int(cfg.get("horizon_weeks", 1) or 1)),
            "llm_ready": _llm_ready(cfg),
            "categories": _categories(cfg),
            "university_root": _university_root(cfg),
        },
        "meeting": _meeting_state(store),
    }


def _meeting_state(store):
    """The flagged next presentation meeting + days until it (+ plan coverage)."""
    meeting = store.get_next_meeting() if hasattr(store, "get_next_meeting") \
        else None
    if not meeting or not meeting.get("date"):
        return None
    try:
        mdate = date.fromisoformat(str(meeting["date"])[:10])
    except ValueError:
        return None
    days = (mdate - date.today()).days
    out = dict(meeting)
    out["days_until"] = days
    out["passed"] = days < 0
    return out


def api_meeting(store, cfg, data):
    """Flag (or clear) the next presentation meeting the agent plans around."""
    data = data or {}
    if data.get("clear") or data.get("date") in (None, ""):
        store.set_next_meeting(None)
        return {"meeting": None, "cleared": True}
    try:
        mdate = date.fromisoformat(str(data.get("date")).strip()[:10])
    except ValueError:
        return {"error": "date must be YYYY-MM-DD"}, 400
    meeting = store.set_next_meeting({
        "date": mdate.isoformat(),
        "title": (data.get("title") or "").strip() or "PI meeting",
        "calendar": (data.get("calendar") or ""),
        "id": (data.get("id") or ""),
    })
    return {"meeting": _meeting_state(store), "set": True}


def api_intake(store, cfg, data):
    """Describe a task in one line; the LLM derives everything else.

    Optional fields: `backend` ("heuristic" forces the offline estimate);
    `within_weeks` sets the task's planning window.
    """
    data = data or {}
    text = (data.get("description") or "").strip()
    if not text:
        return {"error": "describe the task first"}, 400
    if (data.get("mode") or "") == "offline":
        data["backend"] = "heuristic"
    try:
        from planner import intake
    except Exception as exc:
        return {"error": "intake module unavailable: %s" % exc}, 503
    try:
        res = intake.create_from_description(
            store, cfg, text,
            repo_root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            deadline=data.get("deadline"),
            backend_override=(data.get("backend") or "").strip().lower() or None,
            category=data.get("category"),
            within_weeks=data.get("within_weeks"))
    except ValueError as exc:
        return {"error": str(exc)}, 400
    except Exception as exc:
        return {"error": str(exc)}, 500
    res.setdefault("title", text[:70])
    return res


# --------------------------------------------------------------------------- #
# habits — recurring personal commitments, materialized + mirrored weekly
# --------------------------------------------------------------------------- #
def _habit_materialize(store, cfg):
    """Re-materialize + mirror right away after a user change (best-effort)."""
    try:
        from planner import habits as habits_mod
        return habits_mod.materialize_and_mirror(store, cfg)
    except Exception as exc:
        return {"error": str(exc)}


def api_habits(store, cfg, query):
    """GET: the habit templates plus this week's materialized instances."""
    from datetime import timedelta as _td
    from planner.scheduler import week_start_for
    ws = week_start_for()
    days = []
    for i in range(7):
        d = ws + _td(days=i)
        slots = store.habit_slots_between(d.isoformat(), d.isoformat())
        days.append({"date": d.isoformat(),
                     "weekday": d.weekday(),
                     "slots": slots})
    return {"habits": store.habits(),
            "week": ws.isoformat(),
            "days": days,
            "calendar": (cfg or {}).get("habits_calendar_name") or "Habits"}


def _habit_fields(data):
    """Validated habit template fields from a POST body (None on bad input)."""
    name = str((data or {}).get("name") or "").strip()
    if not name:
        return None
    weekdays = data.get("weekdays") or []
    if not isinstance(weekdays, list):
        return None
    try:
        weekdays = sorted({int(w) for w in weekdays if 0 <= int(w) <= 6})
    except (TypeError, ValueError):
        return None
    if not weekdays:
        return None                          # repeating on no day is nothing
    try:
        minutes = max(5, int(data.get("minutes") or 20))
    except (TypeError, ValueError):
        return None
    start = str(data.get("start") or "").strip()
    end = str(data.get("end") or "").strip()
    for value in (start, end):
        if value and not re.match(r"^\d{2}:\d{2}$", value):
            return None
    if bool(start) != bool(end):
        return None
    prefer = str(data.get("prefer") or "day").strip().lower()
    if prefer not in ("morning", "day", "evening"):
        prefer = "day"
    return {"name": name[:120], "weekdays": weekdays, "minutes": minutes,
            "start": start, "end": end, "prefer": prefer}


def api_habits_add(store, cfg, data):
    """POST: create one habit template; the week re-materializes at once."""
    fields = _habit_fields(data)
    if not fields:
        return {"error": "name, weekdays ([0..6]) and a valid window or "
                         "minutes are required"}, 400
    hid = store.add_habit(active=1, **fields)
    report = _habit_materialize(store, cfg)
    return {"habit": store.get_habit(hid), "materialize": report}


def api_habits_update(store, cfg, habit_id, data):
    """POST: change a template; the week re-materializes at once."""
    if not store.get_habit(habit_id):
        return {"error": "no habit #%s" % habit_id}, 404
    fields = _habit_fields(data)
    if not fields:
        return {"error": "name, weekdays ([0..6]) and a valid window or "
                         "minutes are required"}, 400
    store.update_habit(habit_id, **fields)
    report = _habit_materialize(store, cfg)
    return {"habit": store.get_habit(habit_id), "materialize": report}


def api_habits_delete(store, cfg, habit_id):
    """DELETE: remove the habit for real — template, all instances (past
    included) and their mirror events. "Delete" must mean deleted."""
    habit = store.get_habit(habit_id)
    if not habit:
        return {"error": "no habit #%s" % habit_id}, 404
    gone_rows = store.delete_habit(habit_id)
    removed_events, missed = 0, 0
    cal = (cfg or {}).get("habits_calendar_name") or "Habits"
    for row in gone_rows:
        if not row.get("calendar_event_id"):
            continue
        try:
            from planner.calendar_mac import delete_event
            res = delete_event(row["calendar_event_id"], cal)
            if isinstance(res, dict) and res.get("deleted"):
                removed_events += 1
            else:
                missed += 1
        except Exception:
            missed += 1
    report = _habit_materialize(store, cfg)
    return {"deleted": habit_id, "removed_events": removed_events,
            "events_left_behind": missed, "materialize": report}


def api_habits_sync(store, cfg):
    """POST: re-materialize + re-mirror right now.

    The manual healing path: after the Habits calendar was wiped or its
    events edited by hand, this re-reads the calendar, drops the stale
    stamps and re-creates what is missing.
    """
    return {"materialize": _habit_materialize(store, cfg)}


def api_habit_state(store, cfg, slot_id, data):
    """POST: mark one materialized instance done or skipped."""
    data = data or {}
    if not store.habit_slot(slot_id):
        return {"error": "no habit slot #%s" % slot_id}, 404
    done = data.get("done")
    skipped = data.get("skipped")
    if done is None and skipped is None:
        return {"error": "pass done or skipped"}, 400
    row = store.set_habit_slot_state(
        slot_id,
        done=None if done is None else bool(done),
        skipped=None if skipped is None else bool(skipped))
    return {"slot": row}


def api_habits_parse(store, cfg, data):
    """POST: one LLM call turning free text into habit PROPOSALS.

    Nothing is saved — the UI shows the list, the user ticks what to keep,
    and only then calls /api/habits/save.
    """
    text = str((data or {}).get("text") or "").strip()
    if not text:
        return {"error": "describe the habits first"}, 400
    try:
        from planner import habits as habits_mod
    except Exception as exc:
        return {"error": "habits module unavailable: %s" % exc}, 503
    res = habits_mod.parse_proposals(text, cfg)
    if res.get("error"):
        return {"error": res["error"], "proposals": []}, 200
    return res


def api_habits_save(store, cfg, data):
    """POST: save the reviewed proposals as habit templates."""
    proposals = (data or {}).get("habits") or []
    if not isinstance(proposals, list) or not proposals:
        return {"error": "no habits to save"}, 400
    saved = []
    for raw in proposals[:8]:
        fields = _habit_fields(raw)
        if not fields:
            continue
        saved.append(store.get_habit(store.add_habit(active=1, **fields)))
    if not saved:
        return {"error": "none of the proposals was valid"}, 400
    report = _habit_materialize(store, cfg)
    return {"saved": saved, "materialize": report}


def api_feedback(store, cfg, task_id, data):
    """'It took 20 minutes, not 60' — folds into the calibration."""
    data = data or {}
    try:
        minutes = int(data.get("minutes") or 0)
    except (TypeError, ValueError):
        return {"error": "minutes must be a number"}, 400
    if minutes <= 0:
        return {"error": "minutes must be positive"}, 400
    try:
        from planner import intake
    except Exception as exc:
        return {"error": "intake module unavailable: %s" % exc}, 503
    try:
        return intake.apply_feedback(store, cfg, task_id, minutes,
                                     note=(data.get("note") or "").strip())
    except Exception as exc:
        return {"error": str(exc)}, 500


# --------------------------------------------------------------------------- #
# manual slot moves — the user's correction of a wrong agent placement
# --------------------------------------------------------------------------- #
def _slot_sync_calendar(cfg, action, uid, date_iso, start=None, end=None):
    """Best-effort follow-up on the Apple Calendar event of a moved slot.

    Returns a small report dict; a missing permission or a renamed calendar
    becomes data in `error`, never an exception (the DB move already won).
    """
    if not uid:
        return {"skipped": "slot has no calendar event"}
    try:
        from planner.calendar_mac import delete_event, update_event_times
    except Exception as exc:
        return {"error": str(exc)}
    cal = (cfg or {}).get("calendar_name") or "Thesis"
    if action == "update":
        report = update_event_times(uid, "%sT%s" % (date_iso, start),
                                    "%sT%s" % (date_iso, end), calendar_name=cal)
    else:
        report = delete_event(uid, calendar_name=cal)
    return report


def _validate_slot_fields(data, require_all):
    """{date, start, end} -> validated strings; 400BadRequest on garbage."""
    out = {}
    date_raw = data.get("date")
    start_raw = data.get("start")
    end_raw = data.get("end")
    if date_raw in (None, ""):
        if require_all:
            raise BadRequest("date is required")
    else:
        out["date"] = _iso_date(date_raw, "date")
    for key, raw, label in (("start", start_raw, "start"),
                            ("end", end_raw, "end")):
        if raw in (None, ""):
            if require_all:
                raise BadRequest("%s is required" % label)
            continue
        value = str(raw).strip()
        if not _HHMM.match(value):
            raise BadRequest("%s must be HH:MM (24h), got %r" % (label, raw))
        out[key] = value
    if "start" in out and "end" in out:
        if parse_hhmm(out["end"]) <= parse_hhmm(out["start"]):
            raise BadRequest("end must be after start")
    return out


def api_slot_move(store, cfg, slot_id, data):
    """Move a planned block to a new date/time; pins it so re-plans keep it."""
    try:
        slot = store.get_slot(slot_id)
    except Exception as exc:
        return {"error": str(exc)}, 500
    if not slot:
        raise NotFound("no slot #%s" % slot_id)
    fields = _validate_slot_fields(data or {}, require_all=False)
    if not fields:
        raise BadRequest("nothing to change: pass date, start and/or end")
    uid = slot.get("calendar_event_id")
    try:
        moved = store.move_slot(slot_id, fields.get("date"),
                                fields.get("start"), fields.get("end"))
    except ValueError as exc:
        return {"error": str(exc)}, 400
    except LookupError:
        raise NotFound("no slot #%s" % slot_id)
    if uid:
        calendar = _slot_sync_calendar(cfg, "update", uid,
                                       moved["date"], moved["start"],
                                       moved["end"])
    else:
        # self-heal: a block written before the calendar existed (or created
        # by the calendar import before its write) gets its event now, so
        # app-side moves can never leave Apple Calendar behind
        calendar = _create_slot_event(store, cfg, moved)
    return {"slot": moved, "pinned": True, "calendar": calendar}


def _create_slot_event(store, cfg, slot):
    """Write the Apple Calendar event for a slot that has none; stamps the
    uid on success. Best-effort: failures become data, never exceptions."""
    try:
        from planner.calendar_mac import event_summary, write_event
    except Exception as exc:
        return {"error": str(exc)}
    cal = (cfg or {}).get("calendar_name") or "Thesis"
    task = store.get_task(slot["task_id"]) or {}
    try:
        uid = write_event(
            event_summary(task, cfg),
            "%sT%s:00" % (slot["date"], slot["start"]),
            "%sT%s:00" % (slot["date"], slot["end"]),
            cal,
            description=_slot_description(store, slot, task))
        if uid:
            store.set_slot_calendar_id(slot["id"], uid)
            return {"created": uid}
        return {"error": "no event uid came back"}
    except Exception as exc:
        return {"error": str(exc)}


def api_slot_remove(store, cfg, slot_id):
    """Unschedule one block (the task itself stays); syncs Calendar."""
    try:
        slot = store.get_slot(slot_id)
    except Exception as exc:
        return {"error": str(exc)}, 500
    if not slot:
        raise NotFound("no slot #%s" % slot_id)
    uid = slot.get("calendar_event_id")
    store.delete_slot(slot_id)
    calendar = _slot_sync_calendar(cfg, "delete", uid, slot["date"],
                                   slot["start"], slot["end"])
    return {"removed": True, "task_id": slot.get("task_id"),
            "calendar": calendar}


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
class PlannerHandler(BaseHTTPRequestHandler):
    # HTTP/1.0 keeps the connection handling trivial: one request, then close.
    server_version = "ThesisPlanner/1.0"
    # Drop a socket that connects and then says nothing (preconnects, port
    # scanners) instead of holding a worker thread for it.
    timeout = 20

    # ---- plumbing ----
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def log_message(self, fmt, *args):
        if getattr(self.server, "quiet", False):
            return
        sys.stderr.write("[web] %s\n" % (fmt % args))

    def _dispatch(self, method):
        try:
            self._route(method)
        except HttpError as exc:
            self._send_json({"error": exc.message}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover - defensive
            traceback.print_exc()
            self._send_json({"error": "server error: %s" % exc}, 500)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("bad Content-Length")
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise BadRequest("request body too large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BadRequest("invalid JSON: %s" % exc)
        if not isinstance(data, dict):
            raise BadRequest("body must be a JSON object")
        return data

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _respond(self, result, status=200):
        """Send a handler result; handlers may return (payload, status)."""
        if isinstance(result, tuple) and len(result) == 2 \
                and isinstance(result[1], int):
            return self._send_json(result[0], result[1])
        return self._send_json(result, status)

    def _send_html(self, text, status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_file(self, path, content_type):
        """Serve a file from planner/ui/; 404 rather than 500 when missing."""
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._send_json({"error": "not found: %s" % path}, 404)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    @staticmethod
    def _allow(method, *allowed):
        if method not in allowed:
            raise MethodNotAllowed("/".join(allowed))

    # ---- routing ----
    def _route(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        query = parse_qs(parsed.query)
        store = self.server.store()
        cfg = self.server.cfg

        if path in ("/", "/index.html"):
            self._allow(method, "GET")
            return self._send_html(render_page())

        if path.startswith("/ui/"):
            self._allow(method, "GET")
            name = os.path.basename(path)
            ctype = {"css": "text/css; charset=utf-8",
                     "js": "application/javascript; charset=utf-8",
                     "svg": "image/svg+xml",
                     "woff2": "font/woff2"}.get(name.rsplit(".", 1)[-1], "text/plain")
            return self._send_file(os.path.join(UI_DIR, name), ctype)

        if path == "/api/state":
            self._allow(method, "GET")
            return self._send_json(api_state(store, cfg, query))

        if path == "/api/intake":
            self._allow(method, "POST")
            return self._respond(api_intake(store, cfg, self._read_json()))

        if path == "/widget":
            self._allow(method, "GET")
            return self._send_html(render_widget())

        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/api/tasks/past":
            self._allow(method, "POST")
            return self._respond(api_task_past(store, self._read_json()))

        if path == "/api/tasks":
            if method == "GET":
                return self._send_json(api_list_tasks(store, query))
            if method == "POST":
                status, payload = api_add_task(store, self._read_json(), cfg)
                return self._send_json(payload, status)
            raise MethodNotAllowed("GET/POST")

        match = TASK_PATH.match(path)
        if match:
            task_id = int(match.group(1))
            if method == "GET":
                return self._send_json({"task": _task_or_404(store, task_id)})
            if method == "POST":
                return self._send_json(api_update_task(store, task_id,
                                                       self._read_json()))
            if method == "DELETE":
                return self._respond(api_task_delete(store, cfg, task_id))
            raise MethodNotAllowed("GET/POST/DELETE")

        match = FEEDBACK_PATH.match(path)
        if match:
            self._allow(method, "POST")
            return self._respond(api_feedback(store, cfg, int(match.group(1)),
                                              self._read_json()))

        match = STATUS_PATH.match(path)
        if match:
            self._allow(method, "POST")
            return self._send_json(api_set_status(store, cfg,
                                                  int(match.group(1)),
                                                  self._read_json()))

        match = SLOT_PATH.match(path)
        if match:
            slot_id = int(match.group(1))
            if method == "POST":
                return self._respond(api_slot_move(store, cfg, slot_id,
                                                   self._read_json()))
            if method == "DELETE":
                return self._respond(api_slot_remove(store, cfg, slot_id))
            raise MethodNotAllowed("POST/DELETE")

        if path == "/api/meeting":
            self._allow(method, "POST")
            return self._respond(api_meeting(store, cfg, self._read_json()))

        if path == "/api/plan":
            self._allow(method, "GET")
            return self._send_json(api_plan(store, query))

        if path == "/api/schedule":
            self._allow(method, "POST")
            return self._send_json(api_schedule(store, cfg, self._read_json()))

        if path == "/api/log":
            self._allow(method, "GET")
            return self._send_json(api_log(store, query))

        if path == "/api/history":
            self._allow(method, "GET")
            return self._send_json(api_history(store, query))

        if path == "/api/stats":
            self._allow(method, "GET")
            return self._send_json(api_stats(store))

        if path == "/api/today":
            self._allow(method, "GET")
            return self._send_json(api_today(store, cfg))

        if path == "/api/habits":
            if method == "GET":
                return self._send_json(api_habits(store, cfg, query))
            if method == "POST":
                return self._respond(api_habits_add(store, cfg,
                                                    self._read_json()))
            raise MethodNotAllowed("GET/POST")

        if path == "/api/habits/parse":
            self._allow(method, "POST")
            return self._respond(api_habits_parse(store, cfg,
                                                  self._read_json()))
        if path == "/api/habits/save":
            self._allow(method, "POST")
            return self._respond(api_habits_save(store, cfg,
                                                 self._read_json()))

        if path == "/api/habits/sync":
            self._allow(method, "POST")
            return self._respond(api_habits_sync(store, cfg))

        match = HABIT_PATH.match(path)
        if match:
            habit_id = int(match.group(1))
            if method == "POST":
                return self._respond(api_habits_update(store, cfg, habit_id,
                                                       self._read_json()))
            if method == "DELETE":
                return self._respond(api_habits_delete(store, cfg, habit_id))
            raise MethodNotAllowed("POST/DELETE")

        match = HABIT_SLOT_PATH.match(path)
        if match:
            self._allow(method, "POST")
            return self._respond(api_habit_state(store, cfg,
                                                 int(match.group(1)),
                                                 self._read_json()))

        if path == "/api/review":
            self._allow(method, "POST")
            return self._respond(api_review(store, cfg, {}))

        raise NotFound("no route for %s" % path)


class PlannerHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server carrying the shared Store/config.

    One thread per connection: browsers open several sockets in parallel and
    preconnect before navigating, and a serial accept loop cannot serve that
    without starving the real request.
    """

    allow_reuse_address = True
    daemon_threads = True          # a stuck request must not block shutdown
    block_on_close = False

    def __init__(self, address, handler, store=None, cfg=None, quiet=False):
        self.given_store = store
        self.cfg = cfg
        self.quiet = quiet
        self._local = threading.local()
        self._extra_stores = []
        self._extra_lock = threading.Lock()
        HTTPServer.__init__(self, address, handler)

    def store(self):
        """Store usable from the calling thread.

        Returns the Store handed to make_server() when sqlite allows using its
        connection here; otherwise opens one connection per thread, because a
        sqlite connection may only be used by the thread that created it.
        """
        store = getattr(self._local, "store", None)
        if store is not None:
            return store
        store = self.given_store
        try:
            store.conn.execute("SELECT 1").fetchone()
        except sqlite3.ProgrammingError:
            store = Store(self.given_store.path)
            with self._extra_lock:
                self._extra_stores.append(store)
        self._local.store = store
        return store

    def server_close(self):
        with self._extra_lock:
            extras, self._extra_stores = self._extra_stores, []
        for extra in extras:
            try:
                extra.conn.close()
            except sqlite3.Error:
                pass
        HTTPServer.server_close(self)


def make_server(store, cfg, port=8765, host="127.0.0.1", quiet=False):
    """Build (but do not start) the HTTP server. port=0 picks a free port."""
    return PlannerHTTPServer((host, int(port)), PlannerHandler,
                             store=store, cfg=cfg, quiet=quiet)


def serve(store, cfg, port=8765, open_browser=False):
    """Serve the UI until Ctrl-C. Returns the server object after shutdown."""
    server = make_server(store, cfg, port=port)
    host, real_port = server.server_address[0], server.server_address[1]
    url = "http://%s:%d/" % (host, real_port)
    # The planner's heartbeat: estimation fallback, calibration, re-plans and
    # the Apple Calendar reconciliation all ride this watcher's ticks.
    watcher = auto_mod.start_cycle_watcher(
        api_root=os.path.dirname(HERE), db_path=getattr(store, "path", None),
        cfg_path=os.environ.get("PLANNER_CONFIG") or DEFAULT_CONFIG_PATH)
    print("planner web UI: %s" % url, flush=True)
    print("  db:     %s" % store.path, flush=True)
    print("  config: work_days=%s %s-%s, max_block=%s min, max_day=%s min"
          % (cfg.get("work_days"), cfg.get("day_start"), cfg.get("day_end"),
             cfg.get("max_block_min"), cfg.get("max_day_min")), flush=True)
    if (cfg.get("auto") or {}).get("watch", True):
        print("  cycle:  watcher on every %ss (estimates, re-plans,"
              " calendar sync)"
              % ((cfg.get("auto") or {}).get("watch_seconds", 20)), flush=True)
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        server.server_close()
    return server


# --------------------------------------------------------------------------- #
# the page
# --------------------------------------------------------------------------- #
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")


def render_page(week=None):
    """The UI shell. Real markup lives in planner/ui/ so it stays editable."""
    try:
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ("<!doctype html><title>Thesis Ledger</title>"
                "<p>planner/ui/index.html is missing from the checkout.</p>")


def render_widget():
    """The compact page. Static HTML; it fills itself from /api/today."""
    return WIDGET


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Thesis Planner</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    font-size: 14px; line-height: 1.45;
    background: #f6f7f9; color: #1f2328;
  }
  header { max-width: 1080px; margin: 0 auto; padding: 24px 24px 6px; }
  h1 { margin: 0; font-size: 21px; font-weight: 600; letter-spacing: -0.01em; }
  header p { margin: 4px 0 0; color: #6a737d; font-size: 13px; }
  main { max-width: 1080px; margin: 0 auto; padding: 12px 24px 80px;
         display: grid; gap: 16px; }
  .card { background: #fff; border: 1px solid #e2e5e9; border-radius: 10px;
          padding: 16px 18px 18px; }
  h2 { margin: 0 0 12px; font-size: 15px; font-weight: 600; }
  h3 { margin: 16px 0 6px; font-size: 13px; font-weight: 600; }
  .muted { color: #6a737d; }
  .small { font-size: 12px; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         font-size: 12px; }

  /* stats strip */
  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
  .chip { background: #fff; border: 1px solid #e2e5e9; border-radius: 999px;
          padding: 2px 10px; font-size: 12px; color: #57606a; }
  .chip b { color: #1f2328; font-weight: 600; }

  /* forms */
  form.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
              gap: 10px 14px; align-items: end; }
  .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .table-wrap table { min-width: 720px; }
  @media (max-width: 640px) {
    header, main { padding-left: 14px; padding-right: 14px; }
    form.grid { grid-template-columns: 1fr 1fr; }
    label.wide { grid-column: 1 / -1; }
  }
  label { display: flex; flex-direction: column; gap: 4px;
          font-size: 12px; color: #57606a; }
  label.wide { grid-column: 1 / -1; }
  input, select, textarea {
    font: inherit; color: #1f2328; background: #fff;
    border: 1px solid #d0d7de; border-radius: 7px; padding: 6px 8px; width: 100%;
  }
  input:focus, select:focus, textarea:focus {
    outline: 2px solid #cfe3ff; border-color: #6aa6ff;
  }
  textarea { resize: vertical; }
  button {
    font: inherit; border: 0; border-radius: 7px; padding: 7px 14px;
    background: #1f6feb; color: #fff; cursor: pointer;
  }
  button:hover { background: #1a5fd0; }
  button.ghost { background: #fff; color: #1f2328; border: 1px solid #d0d7de; }
  button.ghost:hover { background: #f3f4f6; }
  button.small { padding: 2px 9px; font-size: 12px; }
  .row { display: flex; flex-wrap: wrap; gap: 10px; align-items: end; }

  /* tables */
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid #eef0f3;
           vertical-align: top; }
  th { font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
       color: #6a737d; font-weight: 600; border-bottom: 1px solid #e2e5e9; }
  tbody tr:hover { background: #fafbfc; }
  td.num { white-space: nowrap; font-variant-numeric: tabular-nums; }
  td.c { text-align: center; }
  .title { display: block; font-weight: 500; }
  .note { display: block; color: #6a737d; font-size: 12px; margin-top: 2px; }
  tr.st-done .title { color: #6a737d; text-decoration: line-through; }
  tr.st-dropped .title { color: #8b949e; text-decoration: line-through; }
  .pill { display: inline-block; padding: 1px 8px; border-radius: 999px;
          font-size: 11px; border: 1px solid #d0d7de; color: #57606a; }
  .pill.ok { background: #eaf7ee; border-color: #b9e3c6; color: #1a7f37; }
  .pill.warn { background: #fff5e6; border-color: #f0d3a0; color: #9a6700; }

  /* week grid */
  .day { border-top: 1px solid #eef0f3; padding: 8px 0 6px; }
  .day:first-child { border-top: 0; }
  .day-head { display: flex; gap: 8px; align-items: baseline; font-size: 13px; }
  .day-head .min { margin-left: auto; font-size: 12px; color: #6a737d; }
  .slot { display: grid; grid-template-columns: 104px 56px 1fr; gap: 10px;
          padding: 3px 0 3px 4px; align-items: baseline; }
  .slot .time { font-variant-numeric: tabular-nums; color: #1f2328; }
  .slot .mins { color: #6a737d; font-size: 12px; }
  .slot .what { font-weight: 500; }
  .slot .why { display: block; color: #8b949e; font-size: 11.5px; font-weight: 400; }
  ul.warn { margin: 4px 0 0; padding-left: 18px; }
  ul.warn li { color: #9a6700; margin: 3px 0; }
  ul.log { list-style: none; margin: 0; padding: 0; }
  ul.log li { padding: 4px 0; border-bottom: 1px solid #f2f4f6; font-size: 13px; }
  ul.log li:last-child { border-bottom: 0; }
  ul.log .ts { color: #8b949e; font-variant-numeric: tabular-nums; font-size: 12px; }
  ul.log .kind { background: #f3f4f6; border-radius: 4px; padding: 0 5px;
                 font-size: 11.5px; color: #57606a; }
  .msg { font-size: 12px; color: #57606a; }
  .msg.err { color: #cf222e; }
  .spacer { flex: 1; }
</style>
</head>
<body>
<header>
  <h1>Thesis Planner</h1>
  <p>Local only &middot; deterministic scheduler &middot; no data leaves this machine</p>
  <div class="chips" id="stats"></div>
</header>

<main>
  <section class="card">
    <h2>Add task</h2>
    <form class="grid" id="task-form" autocomplete="off">
      <label class="wide">Title
        <input name="title" placeholder="e.g. Draft section 3.2 (evaluation)" required>
      </label>
      <label>Complexity
        <select name="complexity">
          <option value="S">S &middot; small</option>
          <option value="M" selected>M &middot; medium</option>
          <option value="L">L &middot; large</option>
        </select>
      </label>
      <label>Estimated minutes
        <input name="est_minutes" type="number" value="60" min="5" max="10000" step="5">
      </label>
      <label>Priority
        <select name="priority">
          <option value="1">1 &middot; high</option>
          <option value="2" selected>2 &middot; normal</option>
          <option value="3">3 &middot; low</option>
        </select>
      </label>
      <label>Deadline
        <input name="deadline" type="date">
      </label>
      <label>Area
        <input name="area" placeholder="writing / experiments">
      </label>
      <label class="wide">Notes
        <textarea name="notes" rows="2" placeholder="optional context"></textarea>
      </label>
      <div class="row">
        <label class="check" title="The planner agent reads the project, then sets complexity, time and subtasks">
          <input type="checkbox" name="enrich" checked>
          <span>Agent estimates complexity &amp; time</span>
        </label>
        <button type="submit">Add task</button>
        <span class="msg" id="form-msg"></span>
      </div>
    </form>
  </section>

  <section class="card">
    <h2>Tasks <span class="muted small" id="task-count"></span></h2>
    <div class="row" style="margin-bottom:8px">
      <label style="max-width:190px">Status filter
        <select id="status-filter">
          <option value="all" selected>all</option>
          <option value="todo">todo</option>
          <option value="doing">doing</option>
          <option value="done">done</option>
          <option value="blocked">blocked</option>
          <option value="dropped">dropped</option>
        </select>
      </label>
      <span class="spacer"></span>
      <button class="ghost small" id="refresh">Refresh</button>
    </div>
    <div class="table-wrap">
    <table id="task-table">
      <thead>
        <tr>
          <th style="width:42px">#</th>
          <th>Task</th>
          <th style="width:110px">Area</th>
          <th style="width:60px">Cx</th>
          <th style="width:60px">Min</th>
          <th style="width:70px">Prio</th>
          <th style="width:100px">Deadline</th>
          <th style="width:110px">Status</th>
          <th style="width:80px"></th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
    </div>
  </section>

  <section class="card">
    <h2>Week plan</h2>
    <div class="row" style="margin-bottom:10px">
      <label style="max-width:190px">Week of (Monday)
        <input type="date" id="week">
      </label>
      <button id="schedule">Schedule this week</button>
      <button class="ghost" id="load-plan">Show saved plan</button>
      <span class="msg" id="plan-msg"></span>
    </div>
    <div id="week-grid"></div>
    <div id="warnings"></div>
  </section>

  <section class="card">
    <h2>Recent log</h2>
    <ul class="log" id="log"></ul>
  </section>
</main>

<noscript><p style="padding:0 24px">This page needs JavaScript enabled.</p></noscript>

<script>
(function () {
  "use strict";

  var DEFAULT_WEEK = "__DEFAULT_WEEK__";
  var DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  var STATUSES = ["todo", "doing", "done", "blocked", "dropped"];
  var titles = {};

  function $(sel) { return document.querySelector(sel); }

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
      });
  }

  function api(path, options) {
    return fetch(path, options).then(function (res) {
      return res.text().then(function (text) {
        var body = {};
        if (text) { try { body = JSON.parse(text); } catch (e) { body = { error: text }; } }
        if (!res.ok) { throw new Error(body.error || ("HTTP " + res.status)); }
        return body;
      });
    });
  }

  function postJSON(path, data) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data)
    });
  }

  function say(el, text, isError) {
    el.textContent = text || "";
    el.className = "msg" + (isError ? " err" : "");
  }

  // ---------- tasks ----------
  function loadTasks() {
    var filter = $("#status-filter").value;
    var q = filter && filter !== "all" ? "?status=" + encodeURIComponent(filter) : "";
    return api("/api/tasks" + q).then(function (data) {
      var list = data.tasks || [];
      list.forEach(function (t) { titles[t.id] = t.title; });
      $("#task-count").textContent = list.length + " shown";
      var body = $("#task-table tbody");
      if (!list.length) {
        body.innerHTML = '<tr><td colspan="9" class="muted">No tasks yet.</td></tr>';
        return;
      }
      body.innerHTML = list.map(rowHTML).join("");
    });
  }

  function rowHTML(t) {
    var prio = { 1: "1 high", 2: "2", 3: "3 low" }[t.priority] || ("P" + t.priority);
    var deadline = t.deadline ? esc(t.deadline) : '<span class="muted">&mdash;</span>';
    var options = STATUSES.map(function (s) {
      return '<option value="' + s + '"' + (s === t.status ? " selected" : "") + ">" + s + "</option>";
    }).join("");
    var action = t.status === "done"
      ? '<span class="pill ok">done</span>'
      : '<button class="small" data-done="' + t.id + '">Done</button>';
    return '<tr class="st-' + esc(t.status) + '">' +
      '<td class="num">' + t.id + "</td>" +
      '<td><span class="title">' + esc(t.title) + "</span>" +
        (t.notes ? '<span class="note">' + esc(t.notes) + "</span>" : "") + "</td>" +
      '<td class="small">' + (t.area ? esc(t.area) : '<span class="muted">&mdash;</span>') + "</td>" +
      '<td class="c">' + esc(t.complexity) + "</td>" +
      '<td class="num">' + t.est_minutes + "</td>" +
      '<td class="num">' + esc(prio) + "</td>" +
      '<td class="num">' + deadline + "</td>" +
      '<td><select data-status="' + t.id + '">' + options + "</select></td>" +
      '<td class="c">' + action + "</td>" +
      "</tr>";
  }

  function addTask(event) {
    event.preventDefault();
    var form = event.target;
    var f = form.elements;   // form.title would be the form's title attribute
    var payload = {
      title: f.title.value.trim(),
      notes: f.notes.value.trim(),
      area: f.area.value.trim(),
      complexity: f.complexity.value,
      est_minutes: Number(f.est_minutes.value || 60),
      priority: Number(f.priority.value || 2),
      deadline: f.deadline.value || null,
      enrich: !!(f.enrich && f.enrich.checked)
    };
    if (!payload.title) { say($("#form-msg"), "Title is required.", true); return; }
    postJSON("/api/tasks", payload).then(function (data) {
      var msg = "Added task #" + data.id + ".";
      var b = data.brain;
      if (b && b.status === "applied") {
        var p = b.plan || {};
        msg += " Estimate: " + p.complexity + " · " + p.estimate_minutes + " min · P" + p.priority + ".";
        if ((p.subtasks || []).length) msg += " " + p.subtasks.length + " subtask(s).";
      } else if (b && b.status === "error") {
        msg += " Estimate error: " + b.error;
      }
      say($("#form-msg"), msg);
      form.reset();
      f.est_minutes.value = "60";
      f.priority.value = "2";
      f.complexity.value = "M";
      return Promise.all([loadTasks(), loadStats(), loadLog()]);
    }).catch(function (err) {
      say($("#form-msg"), "Error: " + err.message, true);
    });
  }

  function setStatus(id, status, note) {
    return postJSON("/api/tasks/" + id + "/status", { status: status, note: note || "" })
      .then(function () { return Promise.all([loadTasks(), loadStats(), loadLog()]); })
      .catch(function (err) { say($("#form-msg"), "Error: " + err.message, true); });
  }

  // ---------- week ----------
  function addDays(iso, n) {
    var d = new Date(iso + "T00:00:00");
    d.setDate(d.getDate() + n);
    return d;
  }

  function isoOf(d) {
    var m = String(d.getMonth() + 1).padStart(2, "0");
    var day = String(d.getDate()).padStart(2, "0");
    return d.getFullYear() + "-" + m + "-" + day;
  }

  function renderWeek(slots, unscheduled, week) {
    slots = slots || [];
    unscheduled = unscheduled || [];
    var byDate = {};
    slots.forEach(function (s) {
      (byDate[s.date] = byDate[s.date] || []).push(s);
    });
    var html = "";
    for (var i = 0; i < 7; i++) {
      var d = addDays(week, i);
      var iso = isoOf(d);
      var list = byDate[iso] || [];
      var total = list.reduce(function (a, s) { return a + s.minutes; }, 0);
      html += '<div class="day"><div class="day-head"><strong>' + DAY_NAMES[d.getDay()] +
        '</strong><span class="muted">' + iso + "</span>" +
        (list.length ? '<span class="min">' + total + " min</span>" : "") + "</div>";
      if (!list.length) {
        html += '<div class="small muted" style="padding:3px 0 3px 4px">nothing planned</div>';
      } else {
        html += list.map(function (s) {
          var name = titles[s.task_id] || ("task #" + s.task_id);
          return '<div class="slot"><span class="time">' + esc(s.start) + "&ndash;" +
            esc(s.end) + '</span><span class="mins">' + s.minutes + "m</span>" +
            '<span class="what">' + esc(name) +
            '<span class="why">' + esc(s.rationale) + "</span></span></div>";
        }).join("");
      }
      html += "</div>";
    }
    $("#week-grid").innerHTML = html;

    var warn = "";
    if (unscheduled.length) {
      warn = '<h3 class="pill warn">' + unscheduled.length + " block(s) could not be placed</h3>" +
        '<ul class="warn">' + unscheduled.map(function (u) {
          return "<li><strong>" + esc(u.title) + "</strong> &mdash; " + esc(u.reason) +
            ' <span class="muted">(chunk ' + esc(u.chunk) + ", " + u.minutes + " min)</span></li>";
        }).join("") + "</ul>";
    } else if (slots.length) {
      warn = '<p class="small muted">Every open task fits this week.</p>';
    }
    $("#warnings").innerHTML = warn;
  }

  function scheduleWeek() {
    var week = $("#week").value || DEFAULT_WEEK;
    say($("#plan-msg"), "Scheduling\u2026");
    postJSON("/api/schedule", { week: week, save: true }).then(function (data) {
      $("#week").value = data.week;
      var msg = "Planned " + data.stats.slots + " block(s), " +
        data.stats.planned_minutes + " min";
      if (data.saved_plan_id) { msg += " \u00b7 saved as plan #" + data.saved_plan_id; }
      say($("#plan-msg"), msg);
      renderWeek(data.slots, data.unscheduled, data.week);
      return Promise.all([loadLog(), loadStats()]);
    }).catch(function (err) {
      say($("#plan-msg"), "Error: " + err.message, true);
    });
  }

  function loadSavedPlan(quiet) {
    var week = $("#week").value || DEFAULT_WEEK;
    return api("/api/plan?week=" + encodeURIComponent(week)).then(function (data) {
      renderWeek(data.slots, [], data.week);
      if (data.exists) {
        if (!quiet) {
          say($("#plan-msg"), "Saved plan #" + data.plan_id + " from " +
            String(data.created_at || "").replace("T", " ") + " (" + data.slots.length + " blocks)");
        }
      } else if (!quiet) {
        say($("#plan-msg"), "No saved plan for that week yet.", false);
      }
    }).catch(function (err) {
      say($("#plan-msg"), "Error: " + err.message, true);
    });
  }

  // ---------- log / stats ----------
  function payloadSummary(e) {
    var p = e.payload || {};
    var bits = [];
    if (p.title) { bits.push(p.title); }
    if (p.week) { bits.push("week " + p.week); }
    if (p.slots !== undefined && p.slots !== null) { bits.push(p.slots + " slots"); }
    if (p.status) { bits.push("\u2192 " + p.status); }
    if (p.note) { bits.push(p.note); }
    if (!bits.length) { bits.push(JSON.stringify(p).slice(0, 90)); }
    return bits.join(" \u00b7 ");
  }

  function loadLog() {
    return api("/api/log?limit=25").then(function (data) {
      var events = data.events || [];
      $("#log").innerHTML = events.length
        ? events.map(function (e) {
            return '<li><span class="ts">' + esc(String(e.ts || "").replace("T", " ")) +
              '</span> <span class="kind">' + esc(e.kind) + "</span> " +
              '<span class="muted">' + esc(payloadSummary(e)) + "</span></li>";
          }).join("")
        : '<li class="muted">No events yet.</li>';
    });
  }

  function loadStats() {
    return api("/api/stats").then(function (s) {
      var by = s.tasks_by_status || {};
      var items = [
        ["open", s.open_tasks], ["todo", by.todo || 0], ["doing", by.doing || 0],
        ["done", by.done || 0], ["blocked", by.blocked || 0],
        ["open min", s.open_minutes], ["overdue", (s.overdue || []).length],
        ["plans", s.plans]
      ];
      $("#stats").innerHTML = items.map(function (kv) {
        return '<span class="chip"><b>' + kv[1] + "</b> " + kv[0] + "</span>";
      }).join("");
    });
  }

  // ---------- wiring ----------
  function bind() {
    $("#task-form").addEventListener("submit", addTask);
    $("#status-filter").addEventListener("change", function () { loadTasks().catch(function () {}); });
    $("#refresh").addEventListener("click", function () {
      loadTasks().then(function () { return Promise.all([loadStats(), loadLog()]); }).catch(function () {});
    });
    $("#schedule").addEventListener("click", scheduleWeek);
    $("#load-plan").addEventListener("click", function () { loadSavedPlan(false); });
    $("#task-table").addEventListener("change", function (ev) {
      var sel = ev.target.closest("select[data-status]");
      if (sel) { setStatus(sel.getAttribute("data-status"), sel.value, "via web UI"); }
    });
    $("#task-table").addEventListener("click", function (ev) {
      var btn = ev.target.closest("button[data-done]");
      if (btn) { setStatus(btn.getAttribute("data-done"), "done", "done via web UI"); }
    });
  }

  function init() {
    $("#week").value = DEFAULT_WEEK;
    bind();
    Promise.all([loadTasks(), loadStats(), loadLog()])
      .then(function () { return loadSavedPlan(true); })
      .catch(function (err) { say($("#plan-msg"), "Error: " + err.message, true); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# the widget page (small window / phone)
# --------------------------------------------------------------------------- #
WIDGET = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Today &middot; Planner</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f6f7f9;
    --card: #ffffff;
    --ink: #16181d;
    --muted: #6a737d;
    --line: #e4e7eb;
    --accent: #1f6feb;
    --error: #cf222e;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #131519;
      --card: #1c1f24;
      --ink: #f0f2f5;
      --muted: #9aa4af;
      --line: #2c3138;
      --accent: #6ea8ff;
      --error: #ff7b72;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    background: var(--bg); color: var(--ink);
    padding: 20px 16px 26px;
    -webkit-text-size-adjust: 100%;
  }
  .wrap { max-width: 420px; margin: 0 auto; }

  .top { display: flex; align-items: center; gap: 10px; margin: 0 2px 14px;
         flex-wrap: wrap; }
  .top .day { font-size: 15px; font-weight: 600; letter-spacing: -0.01em;
              min-width: 0; flex: 1 1 auto; }
  .top .day span { display: block; margin-bottom: 2px; font-size: 11px;
                   font-weight: 600; letter-spacing: .08em;
                   text-transform: uppercase; color: var(--muted); }
  .refresh {
    margin-left: auto; flex: none; font: inherit; font-size: 12px; line-height: 1;
    color: var(--muted); background: transparent; cursor: pointer;
    border: 1px solid var(--line); border-radius: 999px; padding: 7px 12px;
  }
  .refresh:hover { color: var(--ink); border-color: var(--muted); }
  .refresh[disabled] { opacity: .45; cursor: default; }

  .next { background: var(--card); border: 1px solid var(--line);
          border-radius: 16px; padding: 18px 20px 20px; }
  .kicker { font-size: 11px; font-weight: 600; letter-spacing: .09em;
            text-transform: uppercase; color: var(--muted); }
  .next .time { margin-top: 10px; font-size: 2rem; line-height: 1.05;
                font-weight: 600; letter-spacing: -0.03em;
                font-variant-numeric: tabular-nums; }
  .next .title { margin-top: 8px; font-size: 1.15rem; line-height: 1.3;
                 font-weight: 500; overflow-wrap: anywhere; }
  .next .title.rest { margin-top: 12px; font-size: 1.35rem; font-weight: 600; }
  .next .meta { margin-top: 8px; font-size: 13px; color: var(--muted);
                font-variant-numeric: tabular-nums; }

  h2 { margin: 22px 2px 4px; font-size: 11px; font-weight: 600;
       letter-spacing: .09em; text-transform: uppercase; color: var(--muted); }
  ul { list-style: none; margin: 0; padding: 0; }
  li { display: flex; align-items: baseline; gap: 12px;
       padding: 10px 2px; border-bottom: 1px solid var(--line); }
  li:last-child { border-bottom: 0; }
  li .t { flex: none; width: 88px; font-size: 13px; color: var(--muted);
          font-variant-numeric: tabular-nums; }
  li .n { flex: 1; font-size: 15px; line-height: 1.3; overflow-wrap: anywhere; }
  li .m { flex: none; font-size: 12px; color: var(--muted);
          font-variant-numeric: tabular-nums; }
  li.done .n { color: var(--muted); text-decoration: line-through; }
  .empty { margin: 6px 2px 0; font-size: 13px; color: var(--muted); }

  .foot { display: flex; gap: 10px; flex-wrap: wrap; margin: 18px 2px 0;
          padding-top: 12px;
          border-top: 1px solid var(--line); font-size: 12px;
          color: var(--muted); }
  .foot .stamp { margin-left: auto; font-variant-numeric: tabular-nums; }
  .error { margin: 8px 2px 0; font-size: 12px; color: var(--error); }
  [hidden] { display: none !important; }

  @media (max-width: 360px) {
    .next .time { font-size: 1.7rem; }
    li .t { width: 74px; }
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="day"><span>Today</span><b id="date">&mdash;</b></div>
    <button class="refresh" id="refresh" type="button">Refresh</button>
  </div>

  <section class="next" id="next" aria-live="polite">
    <div class="kicker">next block</div>
    <div class="time" id="next-time">&mdash;</div>
    <div class="title" id="next-title">Loading&hellip;</div>
    <div class="meta" id="next-meta" hidden></div>
  </section>

  <h2>Later today</h2>
  <ul id="rest"></ul>
  <p class="empty" id="rest-empty" hidden>Nothing else planned.</p>

  <div class="foot">
    <span id="counts">&mdash;</span>
    <span class="stamp" id="stamp"></span>
  </div>
  <p class="error" id="error" hidden></p>
</div>

<noscript><p class="empty" style="padding:0 16px">This page needs JavaScript.</p></noscript>

<script>
(function () {
  "use strict";

  var REFRESH_MS = 60000;
  var DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
              "Friday", "Saturday"];
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"];

  function $(id) { return document.getElementById(id); }

  function weekdayLabel(iso) {
    var d = new Date(iso + "T00:00:00");
    if (isNaN(d)) { return ""; }
    return ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"][d.getDay()] + " " + d.getDate();
  }

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
                 '"': "&quot;", "'": "&#39;" }[c];
      });
  }

  function pad(n) { return String(n).padStart(2, "0"); }

  function clock() {
    var d = new Date();
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function longDate(iso) {
    var p = String(iso || "").split("-");
    if (p.length !== 3) { return iso || ""; }
    var d = new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
    if (isNaN(d.getTime())) { return iso; }
    return DAYS[d.getDay()] + ", " + d.getDate() + " " + MONTHS[d.getMonth()];
  }

  function row(slot) {
    return '<li' + (slot.status === "done" ? ' class="done"' : "") + ">" +
      '<span class="t">' + esc(slot.start) + " \u2013 " + esc(slot.end) + "</span>" +
      '<span class="n">' + esc(slot.title) + "</span>" +
      '<span class="m">' + esc(slot.minutes) + "m</span></li>";
  }

  function render(data) {
    $("date").textContent = longDate(data.date);

    var next = data.next;
    var time = $("next-time");
    var title = $("next-title");
    var meta = $("next-meta");
    if (next) {
      time.textContent = next.start + " \u2013 " + next.end;
      time.hidden = false;
      title.textContent = next.title;
      title.className = "title";
      meta.textContent = next.minutes + " min"
        + (next.is_today === false && next.date ? " \u00b7 " + weekdayLabel(next.date) : "");
      meta.hidden = false;
    } else {
      time.hidden = true;
      title.textContent = "Nothing planned";
      title.className = "title rest";
      meta.hidden = true;
    }

    var slots = data.slots || [];
    var nowMin = (function () {
      var d = new Date(); return d.getHours() * 60 + d.getMinutes();
    })();
    function endMin(s) {
      var p = (s.end || "00:00").split(":");
      return parseInt(p[0], 10) * 60 + parseInt(p[1], 10);
    }
    var remaining = slots.filter(function (s) { return endMin(s) > nowMin; });
    var rest = [];
    if (next && next.is_today !== false) {
      var at = -1;
      for (var i = 0; i < remaining.length; i++) {
        if (remaining[i].start === next.start && remaining[i].end === next.end) {
          at = i;
          break;
        }
      }
      rest = at < 0 ? [] : remaining.slice(at + 1);
    }
    $("rest").innerHTML = rest.map(row).join("");
    $("rest-empty").hidden = rest.length > 0;

    $("counts").textContent = data.open_tasks + " open tasks \u00b7 " +
      data.planned_minutes + " min planned";
    $("stamp").textContent = "updated " + clock();
    $("error").hidden = true;
  }

  function load() {
    var button = $("refresh");
    button.disabled = true;
    return fetch("/api/today", { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) { throw new Error("HTTP " + res.status); }
        return res.json();
      })
      .then(render)
      .catch(function () {
        $("error").textContent = "Can't reach the planner \u2014 retrying.";
        $("error").hidden = false;
        $("stamp").textContent = "";
      })
      .then(function () { button.disabled = false; });
  }

  function init() {
    $("refresh").addEventListener("click", load);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) { load(); }
    });
    load();
    setInterval(load, REFRESH_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m planner.web",
        description="Local single-page UI for the thesis planner (stdlib only).")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="sqlite file (default: planner/data/planner.db)")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help="config json (default: planner/data/config.json)")
    parser.add_argument("--port", type=int, default=8765,
                        help="port to bind on 127.0.0.1 (default: 8765)")
    parser.add_argument("--open", dest="open_browser", action="store_true",
                        help="open the page in the default browser once serving")
    args = parser.parse_args(argv)

    cfg = config_mod.load(args.config)
    store = Store(args.db)
    print("planner: %d task(s) in %s" % (len(store.list_tasks()), args.db),
          flush=True)
    serve(store, cfg, port=args.port, open_browser=args.open_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

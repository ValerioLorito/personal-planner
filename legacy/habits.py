"""
habits.py — recurring personal commitments as first-class plan material.

A habit is a template: a name, the weekdays it repeats on (0=Mon..6=Sun), and
either a fixed clock window ("Gym", Mon/Wed/Fri 07:00-08:00) or a flexible
duration with a preferred part of the day ("Spanish drill", 20 min, evening).

Every instance that lands in a week is materialized into `habit_slots` — the
DB is the source of truth. Two consumers read those spans:

  * auto.busy_map unions them into the busy time, so task blocks never
    collide with a habit;
  * mirror() writes each instance into the Apple Calendar named by
    `habits_calendar_name` (default "Habits"), one-way: Calendar.app is a
    mirror for these, not an editor (the two-way sync only reads the planner
    calendar).

The chat box ("gym Mon Wed Fri 7-8am, spanish 20min every evening") goes
through ONE LLM call into proposals the user reviews before anything is
saved — parsing never writes.
"""
import json
import re
from datetime import date, timedelta

from planner.scheduler import parse_hhmm

HABIT_SYSTEM_PROMPT = """You turn one loose sentence into recurring personal
habits for a weekly planner. Return STRICT JSON only:
{"habits": [{"name": str (2-4 words, lowercase), "weekdays": [0..6] (0=Mon),
 "minutes": int, "start": "HH:MM" or "", "end": "HH:MM" or "",
 "prefer": "morning"|"day"|"evening"}]}
Rules
- "start"/"end" only when the user named a clock time; then minutes = end-start
  and prefer is ignored.
- Flexible habits have start=end="" and a duration; prefer marks the part of
  the day it should sit in.
- Never invent habits beyond what the sentence says; 0 habits is a valid answer
  when the text names none.
- Weekdays: Mon=0 .. Sun=6. "daily" = [0,1,2,3,4,5,6], "weekdays" = [0..4],
  "weekends" = [5,6].
"""

VALID_PREFERS = ("morning", "day", "evening")


# ------------------------------------------------------------- materializing
def _gaps(spans, lo, hi):
    """Free intervals inside [lo, hi) minus the busy spans (sorted, merged)."""
    spans = sorted((max(lo, s), min(hi, e)) for s, e in spans if e > lo and s < hi)
    out, cur = [], lo
    for s, e in spans:
        if s > cur:
            out.append((cur, s))
        cur = max(cur, e)
    if cur < hi:
        out.append((cur, hi))
    return [(s, e) for s, e in out if e - s > 0]


def _place_flexible(minutes, prefer, occupied, cfg):
    """First free gap in the work day for a flexible habit (None if full).

    Occupied spans come from pinned plan slots + already-placed fixed habit
    windows — deliberately NOT from Apple Calendar, so placement stays
    deterministic (no osascript) and re-materialization never churns.
    """
    day_start = parse_hhmm(cfg["day_start"])
    day_end = parse_hhmm(cfg["day_end"])
    lunch = [parse_hhmm(x) for x in (cfg.get("lunch") or [None, None])]
    blocked = list(occupied)
    if len(lunch) == 2 and lunch[0] is not None and lunch[1] is not None:
        blocked.append((lunch[0], lunch[1]))       # habits respect lunch too
    gaps = _gaps(blocked, day_start, day_end)
    if prefer == "evening":
        gaps = list(reversed(gaps))
    for s, e in gaps:
        if e - s >= minutes:
            return s if prefer != "evening" else e - minutes
    return None


def week_spans(habits, week_start, cfg, occupied=None):
    """One week of instances for the given habit templates.

    Returns {"spans": [{habit_id, name, date, start, end, minutes}],
             "unplaced": [{habit_id, name, date, reason}]}. Fixed windows land
    exactly at their clock time; flexible habits take the first free gap for
    their preferred part of the day. A habit may fill several days a week —
    one instance per habit per date.
    """
    occupied = {k: list(v) for k, v in (occupied or {}).items()}
    spans, unplaced, flex = [], [], []
    for h in habits:
        if not h.get("active", 1):
            continue
        weekdays = set(h.get("weekdays") or [])
        for i in range(7):
            d = week_start + timedelta(days=i)
            iso = d.isoformat()
            if d.weekday() not in weekdays:
                continue
            if h.get("start") and h.get("end"):
                start, end = parse_hhmm(h["start"]), parse_hhmm(h["end"])
                minutes = end - start
                if minutes <= 0:
                    continue
                spans.append({"habit_id": h["id"], "name": h["name"],
                              "date": iso, "start": start, "end": end,
                              "minutes": minutes})
                occupied.setdefault(iso, []).append((start, end))
            else:
                flex.append((h, iso))
    for h, iso in flex:
        minutes = max(1, int(h.get("minutes") or 20))
        start = _place_flexible(minutes, h.get("prefer") or "day",
                                occupied.get(iso, []), cfg)
        if start is None:
            unplaced.append({"habit_id": h["id"], "name": h["name"],
                             "date": iso,
                             "reason": "no free gap in the work day"})
            continue
        end = start + minutes
        spans.append({"habit_id": h["id"], "name": h["name"], "date": iso,
                      "start": start, "end": end, "minutes": minutes})
        occupied.setdefault(iso, []).append((start, end))
    spans.sort(key=lambda s: (s["date"], s["start"], s["name"]))
    return {"spans": spans, "unplaced": unplaced}


# --------------------------------------------------------------- calendar
def _fmt(minutes):
    return "%02d:%02d" % (int(minutes) // 60, int(minutes) % 60)


def mirror(store, cfg, d1, d2, report=None, writer=None, verify_uids=None):
    """Write habit instances to the Habits calendar, one-way.

    Creates missing events, updates moved ones (in place, by uid), and leaves
    done/skipped ones alone.  `writer` injects calendar functions for tests —
    a 3-tuple (write, update, delete) or a 4th `ensure` element for the
    calendar-healing path.  `verify_uids` is the set of uids actually present
    in the Habits calendar right now (one week_events read); slots claiming a
    uid that is not there get their stamps cleared and the event re-created,
    so a wiped or hand-edited calendar converges back to the plan instead of
    staying silently out of sync.  Returns the report; every failure becomes
    data, never an exception.
    """
    report = report if report is not None else {
        "mirrored": 0, "updated": 0, "remirrored": 0, "removed": 0,
        "errors": []}
    cal = (cfg or {}).get("habits_calendar_name") or "Habits"
    try:
        if writer:
            write_event, update_times, delete_event = writer[:3]
            ensure = writer[3] if len(writer) > 3 else None
        else:
            from planner.calendar_mac import (delete_event, ensure_calendar,
                                              update_event_times, write_event)
            write_event, update_times, delete_event = (write_event,
                                                        update_event_times,
                                                        delete_event)
            ensure = ensure_calendar
    except Exception as exc:
        report["errors"].append("calendar write unavailable: %s" % exc)
        return report

    def _create(slot, start_iso, end_iso):
        """Write one event; heal a wiped calendar once, then retry."""
        summary = "Habit: %s" % slot["name"]
        desc = "Habit #%s (%s)" % (slot["habit_id"], slot["name"])
        try:
            return write_event(summary, start_iso, end_iso, cal,
                               description=desc)
        except Exception:
            if ensure is None or not ensure(cal):
                raise
            return write_event(summary, start_iso, end_iso, cal,
                               description=desc)

    for slot in store.habit_slots_between(d1, d2):
        span = "%s|%s" % (slot["start"], slot["end"])
        start_iso = "%sT%s:00" % (slot["date"], slot["start"])
        end_iso = "%sT%s:00" % (slot["date"], slot["end"])
        try:
            uid = slot["calendar_event_id"]
            if uid:
                if verify_uids is not None and uid not in verify_uids:
                    store.clear_habit_mirror(slot["id"])
                    uid = None        # the calendar lost it: re-create below
                elif slot["mirrored_span"] == span:
                    continue          # already on the calendar
                else:
                    res = update_times(uid, start_iso, end_iso, cal)
                    if isinstance(res, dict) and res.get("found") \
                            and not res.get("error"):
                        store.mark_habit_mirrored(slot["id"], span)
                        report["updated"] += 1
                    elif isinstance(res, dict) and not res.get("found") \
                            and not res.get("error"):
                        uid = None    # event gone: re-create below
                    else:
                        report["errors"].append(
                            "update failed for %s (%s)" % (slot["name"],
                                                           slot["date"]))
            if uid is None:
                new_uid = _create(slot, start_iso, end_iso)
                if new_uid:
                    store.mark_habit_mirrored(slot["id"], span, new_uid)
                    if slot["calendar_event_id"]:
                        report["remirrored"] += 1
                    else:
                        report["mirrored"] += 1
                else:
                    report["errors"].append(
                        "write failed for %s (%s)" % (slot["name"],
                                                      slot["date"]))
        except Exception as exc:
            report["errors"].append("%s (%s): %s" % (slot["name"],
                                                     slot["date"], exc))
    return report


def _drop_event(gone, cal, report):
    """Best-effort delete of one removed instance's mirror event."""
    if not gone.get("calendar_event_id"):
        return
    report["removed_events"] += 1
    try:
        from planner.calendar_mac import delete_event
        delete_event(gone["calendar_event_id"], cal)
    except Exception:
        pass                          # the row is gone either way


def materialize_and_mirror(store, cfg, weeks_ahead=1, today=None,
                           mirror_cal=True):
    """The cycle's habit step: fill habit_slots for the rolling window and
    keep the Habits calendar in step. Pure DB unless something changed."""
    from planner.scheduler import week_start_for

    today = today or date.today()
    habits = store.habits(active_only=True)
    cal = (cfg or {}).get("habits_calendar_name") or "Habits"
    report = {"materialized": 0, "added": 0, "moved": 0, "unplaced": 0,
              "removed_events": 0, "mirror": None, "error": None}
    try:
        ws = week_start_for()
        window = (ws.isoformat(),
                  (ws + timedelta(days=7 * weeks_ahead + 6)).isoformat())
        if habits:
            # deterministic occupancy: pinned plan slots only (no osascript)
            occupied = {}
            for d in (ws + timedelta(days=i)
                      for w in range(weeks_ahead + 1)
                      for i in range(7)):
                iso = d.isoformat()
                occupied.setdefault(iso, [])
                for s in store.pinned_slots_between(iso, iso):
                    occupied[iso].append((parse_hhmm(s["start"]),
                                          parse_hhmm(s["end"])))
            # plan every week first, then materialize each habit ONCE over
            # the whole window: set_habit_slots removes rows inside the
            # window that no longer belong (weekday dropped), so the window
            # must cover all materialized weeks — a per-week window would
            # never see the others
            by_habit, unplaced_total = {}, 0
            for w in range(weeks_ahead + 1):
                week = ws + timedelta(days=7 * w)
                week_res = week_spans(habits, week, cfg, occupied=occupied)
                for s in week_res["spans"]:
                    by_habit.setdefault(s["habit_id"], []).append(
                        {"date": s["date"], "start": _fmt(s["start"]),
                         "end": _fmt(s["end"]), "minutes": s["minutes"]})
                unplaced_total += len(week_res["unplaced"])
            for hid, spans in by_habit.items():
                res = store.set_habit_slots(hid, spans, window=window)
                for gone in res["removed"]:
                    _drop_event(gone, cal, report)
                report["materialized"] += len(spans)
                report["added"] += res["added"]
                report["moved"] += res["moved"]
            report["unplaced"] += unplaced_total
        # deactivated templates (legacy soft-deletes): their stale in-window
        # rows (and events) go EVEN when no active habits remain — the old
        # early return stranded the last deleted habit's instances in the
        # week view forever
        for h in store.habits(active_only=False):
            if h["active"]:
                continue
            res = store.set_habit_slots(h["id"], [], window=window)
            for gone in res["removed"]:
                _drop_event(gone, cal, report)
        if mirror_cal and habits:
            # one read of what the calendar actually holds: slots claiming a
            # uid that is not there (wiped calendar, hand-deleted event)
            # get re-created instead of silently staying out of sync
            verify_uids = None
            try:
                from planner.calendar_mac import week_events
                evs = week_events(window[0], window[1], [cal])
                verify_uids = None if evs is None else {
                    str(e["uid"]) for e in evs
                    if isinstance(e, dict) and e.get("uid")}
            except Exception:
                verify_uids = None       # verification is best-effort
            report["mirror"] = mirror(store, cfg, window[0], window[1],
                                      verify_uids=verify_uids)
    except Exception as exc:                  # habits must never break the cycle
        report["error"] = str(exc)
    return report


# ------------------------------------------------------------- chat parsing
def _clean_proposal(raw):
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:120]
    if not name:
        return None
    weekdays = []
    for w in (raw.get("weekdays") or []):
        try:
            w = int(w)
        except (TypeError, ValueError):
            continue
        if 0 <= w <= 6:
            weekdays.append(w)
    try:
        minutes = max(5, int(raw.get("minutes") or 20))
    except (TypeError, ValueError):
        minutes = 20
    start = str(raw.get("start") or "").strip()
    end = str(raw.get("end") or "").strip()
    if not re.match(r"^\d{2}:\d{2}$", start or ""):
        start = ""
    if not re.match(r"^\d{2}:\d{2}$", end or ""):
        end = ""
    if bool(start) != bool(end):
        start = end = ""                      # half a window is no window
    if start and end:
        try:
            minutes = max(5, parse_hhmm(end) - parse_hhmm(start))
        except Exception:
            start = end = ""
    prefer = str(raw.get("prefer") or "day").strip().lower()
    if prefer not in VALID_PREFERS:
        prefer = "day"
    return {"name": name, "weekdays": sorted(set(weekdays)),
            "minutes": minutes, "start": start, "end": end, "prefer": prefer}


def parse_proposals(text, cfg):
    """One LLM call: free text -> reviewed-then-saved habit proposals.

    Returns {"proposals": [...], "error": None} — nothing is saved here.
    An LLM failure is data ("error"), never an exception.
    """
    from planner import brain
    text = str(text or "").strip()
    if not text:
        return {"proposals": [], "error": "say what the habit should be"}
    try:
        answer = brain.llm_json(
            HABIT_SYSTEM_PROMPT,
            'HABITS:\n%s\n\nReturn the JSON object only.' % text[:2000], cfg)
        proposals = []
        for raw in (answer.get("habits") or [])[:8]:
            clean = _clean_proposal(raw)
            if clean:
                proposals.append(clean)
        return {"proposals": proposals, "error": None}
    except Exception as exc:                   # BrainError, transport, JSON
        return {"proposals": [], "error": str(exc)}
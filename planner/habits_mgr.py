"""
habits_mgr.py — manager for habits lifecycle and Apple Calendar synchronization.

Guarantees:
  * Creating a habit creates a true recurring EKRecurrenceRule series in the
    "Habits" calendar.
  * Deleting a habit cleanly deletes the entire recurring series (span="all")
    and purges any orphaned events.
  * Resync heals missing series and imports external recurring events on "Habits".
  * Check-ins feed the calibration memory with actual vs planned duration.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import memory
from .calendar import CalendarError, dt, tag_of
from .db import now_iso


def _next_date_for_weekday(wd: int, start_time: str) -> datetime:
    """Finds the next upcoming datetime matching weekday (0=Mon..6=Sun) and start_time."""
    now = datetime.now()
    days_ahead = (wd - now.weekday()) % 7
    candidate = now + timedelta(days=days_ahead)
    sh, sm = [int(x) for x in start_time.split(":")[:2]]
    candidate = candidate.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if candidate <= now and days_ahead == 0:
        candidate += timedelta(days=7)
    return candidate


def create_habit(app, name: str, days: list[int], start: str, minutes: int, notes: str = "",
                 source: str = "user") -> dict:
    c = app.cfg["calendars"]
    cal_name = c["habits"]
    app.cal.ensure(cal_name)

    days = sorted(set(int(d) for d in days if 0 <= int(d) <= 6))
    if not days:
        days = [0, 1, 2, 3, 4]  # default weekdays
    minutes = max(10, min(int(minutes or 30), 480))
    start = (start or "08:00").strip()

    row_id = app.db.insert("habits", {
        "name": name.strip(),
        "days_json": json.dumps(days),
        "start": start,
        "minutes": minutes,
        "notes": notes.strip(),
        "source": source,
        "status": "active",
        "series_id": "",
        "created_at": now_iso(),
        "updated_at": now_iso()
    })

    # Create recurring event in Calendar
    series_id = ""
    try:
        first_date = _next_date_for_weekday(days[0], start)
        end_date = first_date + timedelta(minutes=minutes)
        notes_body = f"{notes}\n\nplanner:habit={row_id}"
        rec_rule = {"freq": "weekly", "days": days, "interval": 1}

        ev = app.cal.create(
            calendar=cal_name,
            title=name,
            start=first_date.strftime("%Y-%m-%dT%H:%M:00"),
            end=end_date.strftime("%Y-%m-%dT%H:%M:00"),
            notes=notes_body,
            recurrence=rec_rule
        )
        series_id = ev.get("id") or ""
        app.db.update("habits", row_id, {"series_id": series_id})
    except Exception as e:
        app.toast(f"Habit created in DB, but calendar event failed: {e}", "warning")

    app.db.log("habit_created", f"Created habit '{name}'", {"id": row_id, "series_id": series_id})
    app.notify("habits_changed", {"action": "created", "id": row_id})
    return app.db.one("SELECT * FROM habits WHERE id=?", (row_id,))


def update_habit(app, habit_id: int, **fields) -> dict:
    h = app.db.one("SELECT * FROM habits WHERE id=?", (habit_id,))
    if not h:
        raise ValueError(f"Habit #{habit_id} not found")

    name = fields.get("name", h["name"]).strip()
    start = fields.get("start", h["start"]).strip()
    minutes = int(fields.get("minutes", h["minutes"]))
    days = fields.get("days")
    if days is not None:
        days = sorted(set(int(d) for d in days if 0 <= int(d) <= 6))
    else:
        days = json.loads(h["days_json"])
    notes = fields.get("notes", h.get("notes", "")).strip()

    app.db.update("habits", habit_id, {
        "name": name,
        "days_json": json.dumps(days),
        "start": start,
        "minutes": minutes,
        "notes": notes,
        "updated_at": now_iso()
    })

    # Update or recreate in Apple Calendar
    cal_name = app.cfg["calendars"]["habits"]
    series_id = h.get("series_id")
    rec_rule = {"freq": "weekly", "days": days, "interval": 1}
    first_date = _next_date_for_weekday(days[0], start)
    end_date = first_date + timedelta(minutes=minutes)
    notes_body = f"{notes}\n\nplanner:habit={habit_id}"

    if series_id:
        try:
            app.cal.update(
                event_id=series_id,
                span="all",
                calendar=cal_name,
                title=name,
                start=first_date.strftime("%Y-%m-%dT%H:%M:00"),
                end=end_date.strftime("%Y-%m-%dT%H:%M:00"),
                notes=notes_body,
                recurrence=rec_rule
            )
        except Exception:
            # Recreate if update failed
            try:
                ev = app.cal.create(calendar=cal_name, title=name,
                                    start=first_date.strftime("%Y-%m-%dT%H:%M:00"),
                                    end=end_date.strftime("%Y-%m-%dT%H:%M:00"),
                                    notes=notes_body, recurrence=rec_rule)
                app.db.update("habits", habit_id, {"series_id": ev.get("id", "")})
            except Exception as e:
                app.toast(f"Calendar update failed: {e}", "warning")
    else:
        try:
            ev = app.cal.create(calendar=cal_name, title=name,
                                start=first_date.strftime("%Y-%m-%dT%H:%M:00"),
                                end=end_date.strftime("%Y-%m-%dT%H:%M:00"),
                                notes=notes_body, recurrence=rec_rule)
            app.db.update("habits", habit_id, {"series_id": ev.get("id", "")})
        except Exception as e:
            app.toast(f"Calendar create failed: {e}", "warning")

    app.db.log("habit_updated", f"Updated habit #{habit_id} '{name}'")
    app.notify("habits_changed", {"action": "updated", "id": habit_id})
    return app.db.one("SELECT * FROM habits WHERE id=?", (habit_id,))


def rename_habit(app, habit_id: int, new_name: str) -> dict:
    h = app.db.one("SELECT * FROM habits WHERE id=?", (habit_id,))
    if not h:
        raise ValueError(f"Habit #{habit_id} not found")
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("Habit name cannot be empty")
    app.db.update("habits", habit_id, {"name": new_name, "updated_at": now_iso()})
    series_id = h.get("series_id") or ""
    if series_id:
        try:
            app.cal.update(series_id, title=new_name, span="all")
        except Exception as e:
            app.toast(f"Calendar update warning: {e}", "warning")
    app.db.log("habit_renamed", f"Renamed habit #{habit_id} to '{new_name}'")
    app.notify("habits_changed", {"action": "renamed", "id": habit_id})
    return app.db.one("SELECT * FROM habits WHERE id=?", (habit_id,))


def delete_habit(app, habit_id: int) -> bool:
    h = app.db.one("SELECT * FROM habits WHERE id=?", (habit_id,))
    if not h:
        return False

    series_id = h.get("series_id") or ""
    cal_name = app.cfg["calendars"]["habits"]

    # 1. Delete main series if known
    if series_id:
        try:
            app.cal.delete(event_id=series_id, span="all")
        except Exception:
            pass

    # 2. Sweep the Habits calendar for any stray occurrences or tagged events
    try:
        now = datetime.now()
        start = now - timedelta(days=14)
        end = now + timedelta(days=90)
        evs = app.cal.events(start, end, [cal_name])
        seen_del = set()
        for e in evs:
            kind, ref = tag_of(e)
            eid = e.get("id") or ""
            if eid in seen_del:
                continue
            match_tag = (kind == "habit" and ref == habit_id)
            match_series = bool(series_id and eid == series_id)
            match_untagged_title = (kind is None and e.get("title", "").strip().lower() == h["name"].strip().lower())
            if match_tag or match_series or match_untagged_title:
                seen_del.add(eid)
                try:
                    app.cal.delete(
                        event_id=eid,
                        span="all",
                        item_id=e.get("item_id"),
                        ext_id=e.get("ext_id"),
                        start=e.get("start")
                    )
                except Exception:
                    pass
    except Exception:
        pass

    # 3. Mark removed in DB
    app.db.x("DELETE FROM habit_logs WHERE habit_id=?", (habit_id,))
    app.db.x("DELETE FROM habits WHERE id=?", (habit_id,))
    app.db.log("habit_deleted", f"Deleted habit #{habit_id} '{h['name']}'")
    app.notify("habits_changed", {"action": "deleted", "id": habit_id})
    return True


def resync_habits(app) -> dict:
    """Reconciles database habits with Apple Calendar 'Habits' calendar."""
    cal_name = app.cfg["calendars"]["habits"]
    app.cal.ensure(cal_name)
    now = datetime.now()
    start_range = now - timedelta(days=7)
    end_range = now + timedelta(days=35)

    evs = app.cal.events(start_range, end_range, [cal_name])

    active_habits = app.db.q("SELECT * FROM habits WHERE status='active'")
    db_series_ids = {h["series_id"]: h for h in active_habits if h.get("series_id")}
    recreated = 0
    imported = 0

    # 1. Ensure all active DB habits have live calendar series
    cal_event_ids = {e.get("id") for e in evs}
    for h in active_habits:
        s_id = h.get("series_id")
        # Check if s_id exists in fetched events
        matching = [e for e in evs if e.get("id") == s_id or tag_of(e) == ("habit", h["id"])]
        if not matching:
            # Recreate missing series
            days = json.loads(h["days_json"])
            first_date = _next_date_for_weekday(days[0], h["start"])
            end_date = first_date + timedelta(minutes=h["minutes"])
            notes_body = f"{h.get('notes','')}\n\nplanner:habit={h['id']}"
            rec_rule = {"freq": "weekly", "days": days, "interval": 1}
            try:
                ev = app.cal.create(calendar=cal_name, title=h["name"],
                                    start=first_date.strftime("%Y-%m-%dT%H:%M:00"),
                                    end=end_date.strftime("%Y-%m-%dT%H:%M:00"),
                                    notes=notes_body, recurrence=rec_rule)
                app.db.update("habits", h["id"], {"series_id": ev.get("id", "")})
                recreated += 1
            except Exception as e:
                app.toast(f"Failed to restore habit '{h['name']}' in calendar: {e}", "error")

    # 2. Check for recurring events in Calendar that aren't in DB yet
    seen_ids = set()
    for e in evs:
        e_id = e.get("id")
        if not e_id or e_id in seen_ids or not e.get("recurring"):
            continue
        seen_ids.add(e_id)
        kind, ref = tag_of(e)
        if ref and any(h["id"] == ref for h in active_habits):
            continue
        if e_id in db_series_ids:
            continue

        # Found an external recurring event on Habits calendar!
        title = e.get("title") or "Unnamed Habit"
        s_dt = dt(e["start"])
        e_dt = dt(e["end"])
        dur = max(10, int((e_dt - s_dt).total_seconds() // 60))
        rule = e.get("rule") or {}
        days = rule.get("days") or [s_dt.weekday()]
        start_time = s_dt.strftime("%H:%M")

        row_id = app.db.insert("habits", {
            "name": title,
            "days_json": json.dumps(days),
            "start": start_time,
            "minutes": dur,
            "notes": e.get("notes", ""),
            "source": "calendar",
            "status": "active",
            "series_id": e_id,
            "created_at": now_iso(),
            "updated_at": now_iso()
        })
        imported += 1

    app.cal.invalidate()
    app.notify("habits_changed", {"action": "resynced"})
    app.toast(f"Habits synced: {recreated} restored to calendar, {imported} imported from calendar", "success")
    return {"recreated": recreated, "imported": imported, "total_active": len(app.db.q("SELECT id FROM habits WHERE status='active'"))}


def checkin_habit(app, habit_id: int, date_str: str, status: str, actual_minutes: int = None,
                  note: str = "") -> dict:
    h = app.db.one("SELECT * FROM habits WHERE id=?", (habit_id,))
    if not h:
        raise ValueError(f"Habit #{habit_id} not found")

    planned = h["minutes"]
    actual = actual_minutes if actual_minutes is not None else planned

    # Insert log
    app.db.x("""
    INSERT INTO habit_logs (habit_id, date, planned_minutes, actual_minutes, status, source, note, created_at)
    VALUES (?, ?, ?, ?, ?, 'checkin', ?, ?)
    ON CONFLICT(habit_id, date, source) DO UPDATE SET
      actual_minutes=excluded.actual_minutes,
      status=excluded.status,
      note=excluded.note
    """, (habit_id, date_str, planned, actual, status, note, now_iso()))

    # If completed and actual duration reported, record calibration sample!
    if status == "done" and actual:
        memory.add_sample(app.db, "habit", f"habit:{habit_id}", planned, actual,
                          label=h["name"], ref=f"habit_log:{habit_id}:{date_str}")
        app.toast(f"Logged {actual}m for {h['name']} (planned {planned}m) — calibration updated", "info")

    app.notify("habits_changed", {"action": "checkin", "id": habit_id, "date": date_str})
    return {"ok": True, "habit_id": habit_id, "status": status, "actual_minutes": actual}

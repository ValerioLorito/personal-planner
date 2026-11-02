"""
tasks_mgr.py — service layer for planning, scheduling, modifying, and calibrating tasks.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import memory
from .agents import task_agent
from .calendar import dt, tag_of
from .db import now_iso
from .scheduler import Scheduler, parse_deadline


def submit_task_input(app, input_text: str, category: str = "", target_task_id: int = None, on_trace=None) -> dict:
    """Runs the task agent to either edit/reschedule existing tasks or create & plan a new task."""
    app.notify("tasks_changed", {"action": "planning_started", "id": target_task_id or 0})

    stub = {
        "id": int(target_task_id) if target_task_id else 0,
        "target_task_id": int(target_task_id) if target_task_id else None,
        "input_text": input_text.strip(),
        "category": category.strip(),
    }
    plan, trace = task_agent.run(app, stub, on_trace=on_trace)

    # Mode A: Modify / reschedule existing task(s)
    if plan.get("mode") == "edit_existing":
        res = apply_task_edits(app, plan.get("edits") or [], reply=plan.get("reply") or "",
                               trace=trace, fallback_task_id=target_task_id)
        if on_trace:
            on_trace({"type": "final", "thought": res["reply"]})
        return res

    # Mode B: Create and plan a new task
    task_id = app.db.insert("tasks", {
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "input_text": input_text.strip(),
        "title": plan["title"],
        "category": plan["category"],
        "summary": plan["summary"],
        "complexity": plan["complexity"],
        "estimate_minutes": plan["estimate_minutes"],
        "confidence": plan["confidence"],
        "deadline": plan["deadline"],
        "deadline_source": plan["deadline_source"],
        "priority": plan["priority"],
        "deep_work": 1 if plan["deep_work"] else 0,
        "prefs_json": plan["prefs"],
        "rationale": plan["rationale"],
        "files_json": plan["files"],
        "trace_json": trace,
        "status": "draft",
    })

    sub_keys = []
    for i, s in enumerate(plan["subtasks"]):
        sid = app.db.insert("subtasks", {
            "task_id": task_id,
            "idx": i,
            "title": s["title"],
            "minutes": s["minutes"],
            "notes": s.get("notes", ""),
            "status": "todo"
        })
        sub_keys.append({"key": sid, "title": s["title"], "minutes": s["minutes"]})

    prefs = plan.get("prefs") or {}
    fixed_blocks = prefs.get("fixed_blocks")
    target_cal = app.calendar_for(plan["category"])

    if isinstance(fixed_blocks, list) and fixed_blocks:
        placed_any = False
        for i, fb in enumerate(fixed_blocks):
            if not isinstance(fb, dict) or not fb.get("start"):
                continue
            s_str = str(fb["start"]).strip()[:16]
            e_str = str(fb.get("end") or "").strip()[:16]
            if not e_str:
                dur_m = sub_keys[min(i, len(sub_keys) - 1)]["minutes"] if sub_keys else 60
                e_str = (dt(s_str) + timedelta(minutes=dur_m)).strftime("%Y-%m-%dT%H:%M")
            sub_ref = sub_keys[min(i, len(sub_keys) - 1)]["key"] if sub_keys else None
            b_title = fb.get("title") or (f"{plan['title']} ({sub_keys[min(i, len(sub_keys)-1)]['title']})" if len(sub_keys) > 1 else plan["title"])
            app.db.insert("blocks", {
                "task_id": task_id,
                "subtask_id": sub_ref,
                "start": s_str,
                "end": e_str,
                "status": "proposed",
                "calendar": target_cal,
                "event_id": "",
                "title": b_title,
                "pinned": 1
            })
            placed_any = True
        if placed_any:
            app.notify("tasks_changed", {"action": "draft_ready", "id": task_id})
            return get_task_full(app, task_id)

    # Run deterministic placement into calendar gaps
    busy = app.busy(exclude_task_id=task_id)
    load = app.day_load(exclude_task_id=task_id)
    sch = Scheduler(app.cfg, busy=busy, load=load)

    placement = sch.place(
        subtasks=sub_keys,
        deadline=plan["deadline"],
        earliest=prefs.get("earliest"),
        pref=prefs.get("preferred_time", "any"),
        max_block=prefs.get("max_block_minutes"),
        spread=prefs.get("spread", True)
    )

    for b in placement["blocks"]:
        app.db.insert("blocks", {
            "task_id": task_id,
            "subtask_id": b["key"],
            "start": b["start"],
            "end": b["end"],
            "status": "proposed",
            "calendar": target_cal,
            "event_id": "",
            "title": f"{plan['title']} ({b['title']})" if len(sub_keys) > 1 else plan['title'],
            "pinned": 0
        })

    if placement.get("warnings"):
        app.db.update("tasks", task_id, {"warnings_json": placement["warnings"]})

    app.notify("tasks_changed", {"action": "draft_ready", "id": task_id})
    return get_task_full(app, task_id)


def reschedule_task_blocks(app, task_id: int, earliest: str = None, preferred_time: str = None,
                           deadline: str = None) -> dict:
    """Re-places all blocks of an existing task under updated scheduling constraints."""
    t = app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise ValueError(f"Task #{task_id} not found")

    prefs = json.loads(t.get("prefs_json") or "{}")
    if earliest is not None:
        prefs["earliest"] = earliest
    if preferred_time in ("morning", "afternoon", "evening", "any"):
        prefs["preferred_time"] = preferred_time
    new_dl = t.get("deadline") or ""
    if deadline:
        parsed = parse_deadline(str(deadline))
        if parsed:
            new_dl = parsed.strftime("%Y-%m-%dT%H:%M")

    app.db.update("tasks", task_id, {
        "prefs_json": prefs,
        "deadline": new_dl,
        "updated_at": now_iso()
    })

    # Remove old calendar events if the task was scheduled
    old_blocks = app.db.q("SELECT * FROM blocks WHERE task_id=?", (task_id,))
    for ob in old_blocks:
        if ob.get("event_id"):
            try:
                app.cal.delete(ob["event_id"])
            except Exception:
                pass
    app.db.x("DELETE FROM blocks WHERE task_id=?", (task_id,))

    subs = app.db.q("SELECT * FROM subtasks WHERE task_id=? ORDER BY idx", (task_id,))
    sub_keys = [{"key": s["id"], "title": s["title"], "minutes": s["minutes"]}
                for s in subs if s.get("status") != "done"]
    if not sub_keys:
        sub_keys = [{"key": None, "title": t["title"] or "Work", "minutes": max(30, int(t.get("estimate_minutes") or 60))}]

    busy = app.busy(exclude_task_id=task_id)
    load = app.day_load(exclude_task_id=task_id)
    sch = Scheduler(app.cfg, busy=busy, load=load)
    placement = sch.place(
        subtasks=sub_keys,
        deadline=new_dl,
        earliest=prefs.get("earliest"),
        pref=prefs.get("preferred_time", "any"),
        max_block=prefs.get("max_block_minutes"),
        spread=prefs.get("spread", True)
    )

    target_cal = app.calendar_for(t["category"])
    for b in placement["blocks"]:
        app.db.insert("blocks", {
            "task_id": task_id,
            "subtask_id": b["key"],
            "start": b["start"],
            "end": b["end"],
            "status": "proposed",
            "calendar": target_cal,
            "event_id": "",
            "title": f"{t['title']} ({b['title']})" if len(sub_keys) > 1 else t["title"],
            "pinned": 0
        })

    if t.get("status") == "scheduled":
        return schedule_task(app, task_id)

    app.notify("tasks_changed", {"action": "rescheduled", "id": task_id})
    return get_task_full(app, task_id)


def update_task_deadline(app, task_id: int, deadline: str, replan: bool = False) -> dict:
    t = app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise ValueError(f"Task #{task_id} not found")
    raw = str(deadline or "").strip()
    if not raw:
        dl_str = ""
    else:
        parsed = parse_deadline(raw)
        dl_str = parsed.strftime("%Y-%m-%dT%H:%M") if parsed else raw[:16]
    app.db.update("tasks", task_id, {
        "deadline": dl_str,
        "deadline_source": "user" if dl_str else "",
        "updated_at": now_iso()
    })
    if replan and dl_str:
        return reschedule_task_blocks(app, task_id, deadline=dl_str)
    app.notify("tasks_changed", {"action": "deadline_updated", "id": task_id})
    return get_task_full(app, task_id)


def apply_task_edits(app, edits: list, reply: str = "", trace: list = None,
                     fallback_task_id: int = None) -> dict:
    """Executes one or more agent-requested edits on existing tasks/blocks."""
    last_tid = int(fallback_task_id) if fallback_task_id else None
    applied = []

    for e in edits:
        if not isinstance(e, dict):
            continue
        action = (e.get("action") or "move_block").strip().lower()
        tid = e.get("task_id") or fallback_task_id
        bid = e.get("block_id")

        if not tid and bid:
            b_row = app.db.one("SELECT task_id FROM blocks WHERE id=?", (int(bid),))
            if b_row:
                tid = b_row["task_id"]
        if tid:
            tid = int(tid)
            last_tid = tid

        if action == "move_block":
            b = None
            if bid:
                b = app.db.one("SELECT * FROM blocks WHERE id=?", (int(bid),))
            elif tid:
                b_list = app.db.q("SELECT * FROM blocks WHERE task_id=? ORDER BY start", (tid,))
                if b_list:
                    b = b_list[0]
            if b and e.get("new_start"):
                ns = str(e["new_start"]).strip()[:16]
                if len(ns) == 10:
                    ns += "T09:00"
                if e.get("new_end"):
                    ne = str(e["new_end"]).strip()[:16]
                else:
                    dur = dt(b["end"]) - dt(b["start"])
                    ne = (dt(ns) + dur).strftime("%Y-%m-%dT%H:%M")
                move_block(app, b["id"], ns, ne)
                if e.get("deadline") and tid:
                    update_task_deadline(app, tid, e["deadline"])
                applied.append(f"Moved block #{b['id']} to {ns}–{ne}")

        elif action == "reschedule_task" and tid:
            reschedule_task_blocks(
                app, tid,
                earliest=e.get("earliest"),
                preferred_time=e.get("preferred_time"),
                deadline=e.get("deadline")
            )
            applied.append(f"Rescheduled task #{tid}")

        elif action == "update_deadline" and tid:
            update_task_deadline(app, tid, e.get("deadline") or "")
            applied.append(f"Updated deadline of task #{tid}")

        elif action == "rename_task" and tid and e.get("title"):
            rename_task(app, tid, str(e["title"]))
            applied.append(f"Renamed task #{tid}")

        elif action == "delete_task" and tid:
            delete_task(app, tid)
            applied.append(f"Deleted task #{tid}")

        elif action == "remove_block" and bid:
            remove_block(app, int(bid))
            applied.append(f"Removed block #{bid}")

        elif action == "add_block" and tid and e.get("new_start"):
            t_row = app.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
            if t_row:
                ns = str(e["new_start"]).strip()[:16]
                if len(ns) == 10:
                    ns += "T09:00"
                ne = str(e.get("new_end") or "").strip()[:16]
                if not ne:
                    ne = (dt(ns) + timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M")
                b_title = str(e.get("title") or t_row["title"] or "Work block").strip()
                target_cal = app.calendar_for(t_row["category"])
                app.db.insert("blocks", {
                    "task_id": tid,
                    "subtask_id": None,
                    "start": ns,
                    "end": ne,
                    "status": "proposed",
                    "calendar": target_cal,
                    "event_id": "",
                    "title": b_title,
                    "pinned": 1
                })
                if t_row.get("status") == "scheduled":
                    schedule_task(app, tid)
                applied.append(f"Added block to task #{tid} ({ns}–{ne})")

    if last_tid and trace:
        t_exists = app.db.one("SELECT id FROM tasks WHERE id=?", (last_tid,))
        if t_exists:
            app.db.update("tasks", last_tid, {"trace_json": trace, "updated_at": now_iso()})

    msg = reply or ("; ".join(applied) if applied else "Task schedule updated")
    app.toast(msg, "success")
    app.notify("tasks_changed", {"action": "edited", "id": last_tid, "reply": msg})
    app.notify("events_changed", {"action": "edited", "id": last_tid})
    return {
        "mode": "edit_existing",
        "reply": msg,
        "applied": applied,
        "task_id": last_tid,
        "task": get_task_full(app, last_tid) if last_tid else None
    }


def get_task_full(app, task_id: int) -> dict:
    t = app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        return None
    subs = app.db.q("SELECT * FROM subtasks WHERE task_id=? ORDER BY idx", (task_id,))
    blocks = app.db.q("SELECT * FROM blocks WHERE task_id=? ORDER BY start", (task_id,))
    d = dict(t)
    d["prefs"] = json.loads(t.get("prefs_json") or "{}")
    d["files"] = json.loads(t.get("files_json") or "[]")
    d["warnings"] = json.loads(t.get("warnings_json") or "[]")
    d["trace"] = json.loads(t.get("trace_json") or "[]")
    d["subtasks"] = subs
    d["blocks"] = blocks
    return d


def schedule_task(app, task_id: int) -> dict:
    """Approves proposed blocks and writes them as events to Apple Calendar."""
    t = app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise ValueError(f"Task #{task_id} not found")

    target_cal = app.calendar_for(t["category"])
    app.cal.ensure(target_cal)

    blocks = app.db.q("SELECT * FROM blocks WHERE task_id=? ORDER BY start", (task_id,))
    if not blocks:
        raise ValueError("No blocks to schedule")

    created_events = 0
    for b in blocks:
        notes = f"Planner Task #{task_id}: {t['title']}\n"
        if t.get("deadline"):
            notes += f"Deadline: {t['deadline']}\n"
        notes += f"\nplanner:block={b['id']}"

        event_id = b.get("event_id") or ""
        try:
            if event_id:
                ev = app.cal.update(event_id, title=b["title"], start=b["start"], end=b["end"],
                                    calendar=target_cal, notes=notes)
            else:
                ev = app.cal.create(calendar=target_cal, title=b["title"],
                                    start=b["start"], end=b["end"], notes=notes)
                event_id = ev.get("id") or ""
                app.db.update("blocks", b["id"], {"event_id": event_id})
            created_events += 1
        except Exception as e:
            app.toast(f"Error creating event for block #{b['id']}: {e}", "warning")

    app.db.x("UPDATE blocks SET status='scheduled' WHERE task_id=?", (task_id,))
    app.db.update("tasks", task_id, {"status": "scheduled", "updated_at": now_iso()})
    app.toast(f"Scheduled #{task_id} '{t['title']}' ({created_events} calendar events written)", "success")
    app.notify("tasks_changed", {"action": "scheduled", "id": task_id})
    return get_task_full(app, task_id)


def move_block(app, block_id: int, new_start: str, new_end: str) -> dict:
    b = app.db.one("SELECT * FROM blocks WHERE id=?", (block_id,))
    if not b:
        raise ValueError(f"Block #{block_id} not found")

    try:
        s_dt = datetime.fromisoformat(str(new_start))
        e_dt = datetime.fromisoformat(str(new_end))
    except ValueError:
        raise ValueError("Invalid start/end timestamp (expected YYYY-MM-DDTHH:MM)")
    if e_dt <= s_dt:
        raise ValueError("A block must end after it starts")

    app.db.update("blocks", block_id, {
        "start": new_start,
        "end": new_end,
        "pinned": 1
    })

    # Update calendar event if already scheduled
    if b.get("event_id"):
        try:
            app.cal.update(b["event_id"], start=new_start, end=new_end)
        except Exception as e:
            app.toast(f"Failed to move calendar event: {e}", "warning")

    app.notify("tasks_changed", {"action": "block_moved", "id": b["task_id"]})
    return app.db.one("SELECT * FROM blocks WHERE id=?", (block_id,))


def remove_block(app, block_id: int) -> bool:
    b = app.db.one("SELECT * FROM blocks WHERE id=?", (block_id,))
    if not b:
        return False

    if b.get("event_id"):
        try:
            app.cal.delete(b["event_id"])
        except Exception:
            pass

    app.db.x("DELETE FROM blocks WHERE id=?", (block_id,))
    app.notify("tasks_changed", {"action": "block_removed", "id": b["task_id"]})
    return True


def complete_subtask(app, subtask_id: int, actual_minutes: int = None, done: bool = True) -> dict:
    s = app.db.one("SELECT * FROM subtasks WHERE id=?", (subtask_id,))
    if not s:
        raise ValueError(f"Subtask #{subtask_id} not found")

    task = app.db.one("SELECT * FROM tasks WHERE id=?", (s["task_id"],))
    now = now_iso()
    if not done:
        app.db.update("subtasks", subtask_id, {
            "status": "todo",
            "done_at": ""
        })
        if task and task.get("status") == "done":
            has_sched = bool(app.db.q("SELECT id FROM blocks WHERE task_id=? AND status='scheduled'", (s["task_id"],)))
            app.db.update("tasks", s["task_id"], {"status": "scheduled" if has_sched else "draft", "updated_at": now})
        app.notify("tasks_changed", {"action": "subtask_reopened", "id": s["task_id"]})
        return get_task_full(app, s["task_id"])

    app.db.update("subtasks", subtask_id, {
        "status": "done",
        "actual_minutes": actual_minutes or s["minutes"],
        "done_at": now
    })

    # Check if all subtasks are done
    all_subs = app.db.q("SELECT status FROM subtasks WHERE task_id=?", (s["task_id"],))
    if all(r["status"] == "done" for r in all_subs):
        app.db.update("tasks", s["task_id"], {"status": "done", "updated_at": now})

    # Record calibration sample
    if actual_minutes and task:
        memory.add_sample(app.db, "task", task["category"] or "thesis", s["minutes"], actual_minutes,
                          label=s["title"], ref=f"subtask:{subtask_id}")
        app.toast(f"Subtask '{s['title']}' completed ({actual_minutes}m) — calibration updated", "info")

    app.notify("tasks_changed", {"action": "subtask_done", "id": s["task_id"]})
    return get_task_full(app, s["task_id"])


def task_feedback(app, task_id: int, actual_minutes: int, note: str = "") -> dict:
    """User reports how long the entire task took, recalibrating future estimates."""
    t = app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise ValueError(f"Task #{task_id} not found")

    est = t["estimate_minutes"] or 1
    category = t["category"] or "thesis"
    memory.add_sample(app.db, "task", category, est, actual_minutes, label=t["title"], ref=f"task:{task_id}")

    if note:
        memory.remember(app.db, category, f"Task '{t['title']}': took {actual_minutes}m (est {est}m). Note: {note}")

    r = memory.ratio(app.db, "task", category)
    app.toast(f"Recalibrated {category}: new multiplier is {r['ratio']}× ({r['n']} samples)", "success")
    app.notify("tasks_changed", {"action": "feedback", "id": task_id})
    return {"ok": True, "task_id": task_id, "new_ratio": r}


def rename_task(app, task_id: int, new_title: str) -> dict:
    t = app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise ValueError(f"Task #{task_id} not found")
    new_title = new_title.strip()
    if not new_title:
        raise ValueError("Task title cannot be empty")
    old_title = t.get("title") or t.get("input_text") or ""
    app.db.update("tasks", task_id, {"title": new_title, "updated_at": now_iso()})

    # Update blocks in DB and Apple Calendar
    blocks = app.db.q("SELECT * FROM blocks WHERE task_id=?", (task_id,))
    for b in blocks:
        b_title = b.get("title", "")
        if old_title and old_title in b_title:
            new_b_title = b_title.replace(old_title, new_title)
        else:
            new_b_title = f"{new_title} ({b_title})" if b_title else new_title
        app.db.update("blocks", b["id"], {"title": new_b_title})
        if b.get("event_id"):
            try:
                app.cal.update(b["event_id"], title=new_b_title)
            except Exception:
                pass

    app.db.log("task_renamed", f"Renamed task #{task_id} to '{new_title}'")
    app.notify("tasks_changed", {"action": "renamed", "id": task_id})
    return get_task_full(app, task_id)


def delete_task(app, task_id: int) -> bool:
    t = app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        return False

    blocks = app.db.q("SELECT event_id FROM blocks WHERE task_id=?", (task_id,))
    for b in blocks:
        if b.get("event_id"):
            try:
                app.cal.delete(b["event_id"])
            except Exception:
                pass

    app.db.x("DELETE FROM blocks WHERE task_id=?", (task_id,))
    app.db.x("DELETE FROM subtasks WHERE task_id=?", (task_id,))
    app.db.x("DELETE FROM tasks WHERE id=?", (task_id,))
    app.db.log("task_deleted", f"Deleted task #{task_id} '{t['title']}'")
    app.notify("tasks_changed", {"action": "deleted", "id": task_id})
    return True

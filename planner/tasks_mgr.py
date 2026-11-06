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
from .scheduler import Scheduler, parse_deadline, hm


def find_conflicting_events(app, start, end, exclude_task_id: int = None, exclude_block_id: int = None) -> list:
    """Finds all calendar events and other task blocks that overlap with [start, end]."""
    s_dt = dt(str(start)[:16]) if isinstance(start, str) else start
    e_dt = dt(str(end)[:16]) if isinstance(end, str) else end
    conflicts = []
    seen = set()

    # 1. Calendar events
    try:
        evs = app.events(max_age=30)
    except Exception:
        evs = []

    ex_block_ids = set()
    ex_event_ids = set()
    if exclude_block_id:
        ex_block_ids.add(int(exclude_block_id))
        brow = app.db.one("SELECT event_id FROM blocks WHERE id=?", (int(exclude_block_id),))
        if brow and brow.get("event_id"):
            ex_event_ids.add(brow["event_id"])
    if exclude_task_id:
        t_b_rows = app.db.q("SELECT id, event_id FROM blocks WHERE task_id=?", (int(exclude_task_id),))
        for r in t_b_rows:
            ex_block_ids.add(r["id"])
            if r.get("event_id"):
                ex_event_ids.add(r["event_id"])

    for e in evs:
        if e.get("all_day") or not e.get("start") or not e.get("end"):
            continue
        eid = e.get("id") or ""
        if eid and eid in ex_event_ids:
            continue
        kind, ref = tag_of(e)
        if kind == "block" and ref in ex_block_ids:
            continue
        try:
            ev_s = dt(e["start"])
            ev_e = dt(e["end"])
        except Exception:
            continue
        if ev_s < e_dt and ev_e > s_dt:
            key = (e.get("title"), ev_s.isoformat(), ev_e.isoformat())
            if key not in seen:
                seen.add(key)
                conflicts.append({
                    "title": e.get("title") or "Event",
                    "calendar": e.get("calendar") or "",
                    "start": ev_s,
                    "end": ev_e,
                    "source": "calendar",
                    "event_id": eid
                })

    # 2. Database blocks (other proposed or scheduled blocks)
    rows = app.db.q("SELECT b.*, t.title as task_title FROM blocks b JOIN tasks t ON t.id = b.task_id WHERE b.status IN ('proposed', 'scheduled')")
    for b in rows:
        if b["id"] in ex_block_ids:
            continue
        if exclude_task_id and b["task_id"] == int(exclude_task_id):
            continue
        if b.get("event_id") and (b["event_id"] in ex_event_ids or any(c.get("event_id") == b["event_id"] for c in conflicts)):
            continue
        try:
            bs = dt(b["start"])
            be = dt(b["end"])
        except Exception:
            continue
        if bs < e_dt and be > s_dt:
            b_title = b.get("title") or b.get("task_title") or "Planned Task"
            key = (b_title, bs.isoformat(), be.isoformat())
            if key not in seen:
                seen.add(key)
                conflicts.append({
                    "title": b_title,
                    "calendar": b.get("calendar") or "",
                    "start": bs,
                    "end": be,
                    "source": "block"
                })
    return conflicts


def find_nearest_free_slot(app, target_start, duration_minutes: int, exclude_task_id: int = None, exclude_block_id: int = None) -> tuple[datetime, datetime]:
    """Finds the closest non-overlapping free slot inside working hours fitting duration_minutes."""
    s_dt = dt(str(target_start)[:16]) if isinstance(target_start, str) else target_start
    dur = max(15, int(duration_minutes or 30))
    e_dt = s_dt + timedelta(minutes=dur)

    conflicts = find_conflicting_events(app, s_dt, e_dt, exclude_task_id=exclude_task_id, exclude_block_id=exclude_block_id)
    w = app.cfg.get("work", {})
    day_start_h, day_start_m = hm(w.get("day_start", "09:00"))
    day_end_h, day_end_m = hm(w.get("day_end", "19:00"))
    ws = s_dt.replace(hour=day_start_h, minute=day_start_m, second=0, microsecond=0)
    we = s_dt.replace(hour=day_end_h, minute=day_end_m, second=0, microsecond=0)

    if not conflicts and s_dt >= ws and e_dt <= we:
        return s_dt, e_dt

    busy = app.busy(exclude_task_id=exclude_task_id)
    if exclude_block_id and not exclude_task_id:
        b_row = app.db.one("SELECT start, end FROM blocks WHERE id=?", (int(exclude_block_id),))
        if b_row:
            b_s, b_e = dt(b_row["start"]), dt(b_row["end"])
            busy = [inv for inv in busy if not (inv[0] == b_s and inv[1] == b_e)]

    sch = Scheduler(app.cfg, busy=busy, now=min(datetime.now(), s_dt))
    candidates = []

    for day_offset in range(14):
        d_day = (s_dt + timedelta(days=day_offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        is_work_day = d_day.weekday() in w.get("days", [0, 1, 2, 3, 4])
        if not is_work_day and not w.get("weekend_if_needed", True):
            continue
        gaps = sch.gaps(d_day)
        for gs, ge in gaps:
            gap_dur = (ge - gs).total_seconds() / 60
            if gap_dur < dur:
                continue
            if gs <= s_dt and (s_dt + timedelta(minutes=dur)) <= ge:
                return s_dt, s_dt + timedelta(minutes=dur)
            elif gs >= s_dt:
                cand_s = gs
                cand_e = cand_s + timedelta(minutes=dur)
                dist = (cand_s - s_dt).total_seconds()
                candidates.append((dist, cand_s, cand_e))
            elif ge <= s_dt:
                cand_s = ge - timedelta(minutes=dur)
                cand_e = ge
                dist = (s_dt - cand_s).total_seconds() * 1.5
                candidates.append((dist, cand_s, cand_e))
            else:
                cand_s = ge - timedelta(minutes=dur)
                cand_e = ge
                dist = abs((cand_s - s_dt).total_seconds()) * 1.2
                candidates.append((dist, cand_s, cand_e))

        if candidates:
            break

    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1], candidates[0][2]

    return s_dt, e_dt


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
            dur_m = sub_keys[min(i, len(sub_keys) - 1)]["minutes"] if sub_keys else 60
            if not e_str:
                e_str = (dt(s_str) + timedelta(minutes=dur_m)).strftime("%Y-%m-%dT%H:%M")
            else:
                dur_m = max(15, int((dt(e_str) - dt(s_str)).total_seconds() // 60))

            conflicts = find_conflicting_events(app, s_str, e_str, exclude_task_id=task_id)
            if conflicts:
                free_s, free_e = find_nearest_free_slot(app, s_str, dur_m, exclude_task_id=task_id)
                s_str = free_s.strftime("%Y-%m-%dT%H:%M")
                e_str = free_e.strftime("%Y-%m-%dT%H:%M")
                c_names = ", ".join(c["title"] for c in conflicts[:2])
                app.toast(f"Shifted fixed block to {s_str[11:16]}–{e_str[11:16]} to avoid conflict with '{c_names}'", "info")

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


def _clean_task_calendar_events(app, task_id: int, known_event_ids: list = None) -> int:
    """Deletes all Apple Calendar events associated with task_id.
    Deletes known event IDs and scans live calendar for any event tagged
    with this task or its blocks, ensuring no ghost/duplicate events are left behind.
    """
    deleted = 0
    to_delete = set(known_event_ids or [])

    block_rows = app.db.q("SELECT id, event_id FROM blocks WHERE task_id=?", (task_id,))
    task_block_ids = {int(b["id"]) for b in block_rows if b.get("id")}
    for b in block_rows:
        if b.get("event_id"):
            to_delete.add(str(b["event_id"]).strip())

    try:
        events = app.events(max_age=10, refresh=True)
    except Exception:
        events = []

    for ev in events:
        eid = str(ev.get("id") or ev.get("item_id") or "").strip()
        notes = str(ev.get("notes") or "")
        k, ref = tag_of(ev)

        matches = False
        if eid and eid in to_delete:
            matches = True
        elif f"Planner Task #{task_id}" in notes or f"planner:task={task_id}" in notes:
            matches = True
        elif k == "block" and ref in task_block_ids:
            matches = True

        if matches and eid:
            to_delete.add(eid)

    for eid in to_delete:
        if not eid:
            continue
        try:
            if app.cal.delete(eid):
                deleted += 1
        except Exception:
            pass

    return deleted


def reschedule_task_blocks(app, task_id: int, earliest: str = None, preferred_time: str = None,
                           deadline: str = None) -> dict:
    """Re-places all blocks of an existing task under updated scheduling constraints."""
    t = app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise ValueError(f"Task #{task_id} not found")

    prefs = json.loads(t.get("prefs_json") or "{}")
    if earliest is not None:
        if earliest:
            prefs["earliest"] = str(earliest).strip()
        else:
            prefs.pop("earliest", None)
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
    _clean_task_calendar_events(app, task_id)
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
        tid = int(fallback_task_id) if fallback_task_id else (int(e.get("task_id")) if e.get("task_id") else None)
        bid = e.get("block_id")

        if bid:
            b_row = app.db.one("SELECT task_id FROM blocks WHERE id=?", (int(bid),))
            if b_row:
                if fallback_task_id and b_row["task_id"] != int(fallback_task_id):
                    # Re-anchor to the fallback task's block if block_id belongs to another task
                    t_blocks = app.db.q("SELECT id FROM blocks WHERE task_id=? ORDER BY start", (int(fallback_task_id),))
                    bid = t_blocks[0]["id"] if t_blocks else None
                elif not tid:
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
                moved = move_block(app, b["id"], ns, ne)
                if e.get("deadline") and tid:
                    update_task_deadline(app, tid, e["deadline"])
                final_s = moved.get("start") if moved else ns
                final_e = moved.get("end") if moved else ne
                note = " (shifted to avoid conflict)" if final_s != ns else ""
                applied.append(f"Moved block #{b['id']} to {final_s}–{final_e}{note}")

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
                dur_m = max(15, int((dt(ne) - dt(ns)).total_seconds() // 60))
                conflicts = find_conflicting_events(app, ns, ne, exclude_task_id=tid)
                if conflicts:
                    free_s, free_e = find_nearest_free_slot(app, ns, dur_m, exclude_task_id=tid)
                    ns = free_s.strftime("%Y-%m-%dT%H:%M")
                    ne = free_e.strftime("%Y-%m-%dT%H:%M")
                    c_names = ", ".join(c["title"] for c in conflicts[:2])
                    app.toast(f"Shifted new block to {ns[11:16]}–{ne[11:16]} to avoid conflict with '{c_names}'", "info")
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
                realign_task_subtask_order(app, tid)
                applied.append(f"Added block to task #{tid} ({ns}–{ne})")

        elif action == "swap_blocks" and bid and e.get("other_block_id"):
            other_bid = int(e["other_block_id"])
            swap_res = swap_blocks(app, int(bid), other_bid)
            applied.append(swap_res.get("summary") or f"Swapped block #{bid} with block #{other_bid}")

        elif action in ("cascade_subtasks", "cascade_from_block") and (tid or bid):
            reschedule_task_blocks(app, tid, earliest=e.get("new_start") or e.get("earliest"))
            applied.append(f"Rescheduled subtasks in sequential order for task #{tid}")

        elif action in ("realign_subtasks", "reorder_subtasks") and tid:
            changed = realign_task_subtask_order(app, tid)
            applied.append(f"Realigned {len(changed)} subtask block(s) in sequential order for task #{tid}")

    t_row = app.db.one("SELECT id, title FROM tasks WHERE id=?", (last_tid,)) if last_tid else None
    if last_tid and trace and t_row:
        app.db.update("tasks", last_tid, {"trace_json": trace, "updated_at": now_iso()})

    if t_row and reply:
        import re
        real_title = t_row["title"]
        if fallback_task_id:
            reply = re.sub(r"Task #\d+\s*(\([^)]+\))?", f"Task #{last_tid} ({real_title})", reply)
        else:
            def _fix_task_mention(m):
                claimed = m.group(1).strip()
                if claimed.lower() != real_title.lower() and not real_title.lower().startswith(claimed.lower()[:15]):
                    return f"Task #{last_tid} ({real_title})"
                return m.group(0)
            reply = re.sub(rf"Task #{last_tid}\s*\(([^)]+)\)", _fix_task_mention, reply, flags=re.IGNORECASE)

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
    sample = app.db.one("SELECT actual FROM samples WHERE kind='task' AND ref=? ORDER BY id DESC LIMIT 1",
                        (f"task:{task_id}",))
    d = dict(t)
    d["prefs"] = json.loads(t.get("prefs_json") or "{}")
    d["files"] = json.loads(t.get("files_json") or "[]")
    d["warnings"] = json.loads(t.get("warnings_json") or "[]")
    d["trace"] = json.loads(t.get("trace_json") or "[]")
    d["subtasks"] = subs
    d["blocks"] = blocks
    d["logged_actual_minutes"] = sample["actual"] if sample else None
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


def _find_live_event_for_block(app, block_id: int, event_id: str = "") -> dict | None:
    try:
        evs = app.events(max_age=30)
    except Exception:
        evs = []
    for e in evs:
        k, ref = tag_of(e)
        if k == "block" and ref == int(block_id):
            return e
    if event_id:
        for e in evs:
            if e.get("id") == event_id or e.get("item_id") == event_id or e.get("ext_id") == event_id:
                return e
    return None


def _sync_block_event(app, block_row: dict, old_start: str = None) -> dict | None:
    """Reliably updates (or creates if missing on a scheduled task) the Apple Calendar event for a block."""
    if not block_row:
        return None
    bid = int(block_row["id"])
    tid = int(block_row["task_id"])
    t = app.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
    event_id = (block_row.get("event_id") or "").strip()
    live_ev = _find_live_event_for_block(app, bid, event_id)

    notes = f"Planner Task #{tid}: {t['title'] if t else block_row['title']}\n"
    if t and t.get("deadline"):
        notes += f"Deadline: {t['deadline']}\n"
    notes += f"\nplanner:block={bid}"
    target_cal = block_row.get("calendar") or (app.calendar_for(t["category"]) if t else "Calendar")

    if live_ev or event_id:
        eid = (live_ev.get("id") if live_ev else None) or event_id
        occ = (live_ev.get("occurrence") or live_ev.get("start")) if live_ev else (old_start or block_row.get("start"))
        item_id = live_ev.get("item_id") if live_ev else None
        ext_id = live_ev.get("ext_id") if live_ev else None
        try:
            ev = app.cal.update(
                eid,
                occurrence=occ,
                item_id=item_id,
                ext_id=ext_id,
                title=block_row["title"],
                start=block_row["start"],
                end=block_row["end"],
                notes=notes
            )
            new_eid = (ev or {}).get("id") or eid
            if new_eid and new_eid != event_id:
                app.db.update("blocks", bid, {"event_id": new_eid})
            return ev
        except Exception:
            pass

    if block_row.get("status") == "scheduled" or (t and t.get("status") == "scheduled"):
        try:
            app.cal.ensure(target_cal)
            ev = app.cal.create(
                calendar=target_cal,
                title=block_row["title"],
                start=block_row["start"],
                end=block_row["end"],
                notes=notes
            )
            new_eid = (ev or {}).get("id") or ""
            app.db.update("blocks", bid, {"event_id": new_eid, "status": "scheduled"})
            return ev
        except Exception as e:
            app.toast(f"Failed to sync calendar event for block #{bid}: {e}", "warning")
    return None


def realign_task_subtask_order(app, task_id: int) -> list:
    """Ensures that all unfinished subtask blocks of a task follow strict sequential subtask order (idx 0, 1, 2...) chronologically."""
    if not task_id:
        return []
    t = app.db.one("SELECT * FROM tasks WHERE id=?", (int(task_id),))
    if not t:
        return []
    subs = app.db.q("SELECT * FROM subtasks WHERE task_id=? ORDER BY idx, id", (int(task_id),))
    if len(subs) <= 1:
        return []

    sub_by_id = {s["id"]: s for s in subs}
    blocks = app.db.q("SELECT * FROM blocks WHERE task_id=? AND status!='removed' ORDER BY start, end, id", (int(task_id),))
    if not blocks:
        return []

    todo_subs = [s for s in subs if s.get("status") != "done"]
    if not todo_subs:
        todo_subs = list(subs)

    now_dt = datetime.now()
    active_blocks = []
    for b in blocks:
        sid = b.get("subtask_id")
        sub = sub_by_id.get(sid) if sid else None
        if not sub:
            b_title = (b.get("title") or "").lower()
            for s in subs:
                if s["title"] and s["title"].lower() in b_title:
                    sub = s
                    break
        # Leave already-completed past blocks untouched
        if sub and sub.get("status") == "done" and dt(b["end"]) <= now_dt:
            continue
        active_blocks.append((b, sub))

    if not active_blocks:
        return []

    # Build the ordered sequence of subtasks for active_blocks (which are already sorted chronologically by start, end, id)
    valid_assigned = [sub for _, sub in active_blocks if sub is not None and sub.get("status") != "done"]
    if len(valid_assigned) == len(active_blocks):
        ordered_subs = sorted(valid_assigned, key=lambda s: (s.get("idx", 0), s["id"]))
    else:
        ordered_subs = [todo_subs[min(i, len(todo_subs) - 1)] for i in range(len(active_blocks))]

    task_title = (t.get("title") or t.get("input_text") or "Task").strip()

    updated = []
    for (b, old_sub), new_sub in zip(active_blocks, ordered_subs):
        new_title = f"{task_title} ({new_sub['title']})"
        if b.get("subtask_id") != new_sub["id"] or b.get("title") != new_title:
            app.db.update("blocks", b["id"], {
                "subtask_id": new_sub["id"],
                "title": new_title
            })
            updated_b = app.db.one("SELECT * FROM blocks WHERE id=?", (b["id"],))
            _sync_block_event(app, updated_b, old_start=b["start"])
            updated.append({
                "block_id": b["id"],
                "start": b["start"],
                "end": b["end"],
                "old_subtask": old_sub["title"] if old_sub else "",
                "new_subtask": new_sub["title"],
                "title": new_title
            })
    return updated


def move_block(app, block_id: int, new_start: str, new_end: str, preserve_subtask_order: bool = True,
               auto_nudge: bool = True) -> dict:
    b = app.db.one("SELECT * FROM blocks WHERE id=?", (block_id,))
    if not b:
        raise ValueError(f"Block #{block_id} not found")

    old_start = b["start"]
    try:
        s_dt = datetime.fromisoformat(str(new_start)[:16])
        e_dt = datetime.fromisoformat(str(new_end)[:16])
    except ValueError:
        raise ValueError("Invalid start/end timestamp (expected YYYY-MM-DDTHH:MM)")
    if e_dt <= s_dt:
        raise ValueError("A block must end after it starts")

    dur_mins = max(15, int((e_dt - s_dt).total_seconds() // 60))
    if auto_nudge:
        conflicts = find_conflicting_events(app, s_dt, e_dt, exclude_block_id=block_id)
        if conflicts:
            free_s, free_e = find_nearest_free_slot(app, s_dt, dur_mins, exclude_block_id=block_id)
            if free_s != s_dt:
                c_names = ", ".join(c["title"] for c in conflicts[:2])
                s_dt, e_dt = free_s, free_e
                app.toast(f"Shifted block to {s_dt.strftime('%H:%M')}–{e_dt.strftime('%H:%M')} to avoid conflict with '{c_names}'", "info")

    ns_clean = s_dt.strftime("%Y-%m-%dT%H:%M")
    ne_clean = e_dt.strftime("%Y-%m-%dT%H:%M")

    app.db.update("blocks", block_id, {
        "start": ns_clean,
        "end": ne_clean,
        "pinned": 1
    })

    updated_b = app.db.one("SELECT * FROM blocks WHERE id=?", (block_id,))
    _sync_block_event(app, updated_b, old_start=old_start)

    realigned = []
    if preserve_subtask_order and b.get("task_id"):
        realigned = realign_task_subtask_order(app, b["task_id"])

    app.notify("tasks_changed", {"action": "block_moved", "id": b["task_id"], "realigned": len(realigned)})
    app.notify("events_changed", {"action": "block_moved", "id": b["task_id"]})
    out = app.db.one("SELECT * FROM blocks WHERE id=?", (block_id,))
    if out is not None:
        out = dict(out)
        out["realigned"] = realigned
    return out


def swap_blocks(app, block_a_id, block_b_id) -> dict:
    """Swaps the scheduled time slots of two blocks (across different tasks or within the same task) and preserves sequential subtask order."""
    def _resolve_block(val):
        if val is None:
            return None
        try:
            b = app.db.one("SELECT * FROM blocks WHERE id=?", (int(val),))
            if b:
                return b
        except (ValueError, TypeError):
            pass
        s_val = str(val).strip()
        if s_val:
            return app.db.one("SELECT * FROM blocks WHERE event_id=?", (s_val,))
        return None

    ba = _resolve_block(block_a_id)
    bb = _resolve_block(block_b_id)
    if not ba or not bb:
        raise ValueError("Both blocks must exist to swap their slots")
    if ba["id"] == bb["id"]:
        raise ValueError("Cannot swap a block with itself")

    old_a_start = ba["start"]
    old_b_start = bb["start"]
    sa_dt, ea_dt = dt(ba["start"]), dt(ba["end"])
    sb_dt, eb_dt = dt(bb["start"]), dt(bb["end"])
    dur_a = ea_dt - sa_dt
    dur_b = eb_dt - sb_dt

    # Order the two blocks chronologically as (first, second) so adjacent/same-day blocks of different durations never overlap after swapping
    if sa_dt <= sb_dt:
        first_is_a = True
        s1, e1, d1 = sa_dt, ea_dt, dur_a
        s2, e2, d2 = sb_dt, eb_dt, dur_b
    else:
        first_is_a = False
        s1, e1, d1 = sb_dt, eb_dt, dur_b
        s2, e2, d2 = sa_dt, ea_dt, dur_a

    # The later block (2) moves to the earlier block's start (s1)
    new_2_start_dt = s1
    new_2_end_dt = s1 + d2

    # The earlier block (1) moves to the later block's slot; if they are adjacent or would overlap, place block 1 right after block 2's new end
    gap = s2 - e1
    if s1.date() == s2.date() and (abs(gap.total_seconds()) <= 900 or new_2_end_dt > s2):
        preserved_gap = gap if gap.total_seconds() > 0 else timedelta(0)
        new_1_start_dt = new_2_end_dt + preserved_gap
    else:
        new_1_start_dt = s2
    new_1_end_dt = new_1_start_dt + d1

    if first_is_a:
        new_a_start, new_a_end = new_1_start_dt.strftime("%Y-%m-%dT%H:%M"), new_1_end_dt.strftime("%Y-%m-%dT%H:%M")
        new_b_start, new_b_end = new_2_start_dt.strftime("%Y-%m-%dT%H:%M"), new_2_end_dt.strftime("%Y-%m-%dT%H:%M")
    else:
        new_b_start, new_b_end = new_1_start_dt.strftime("%Y-%m-%dT%H:%M"), new_1_end_dt.strftime("%Y-%m-%dT%H:%M")
        new_a_start, new_a_end = new_2_start_dt.strftime("%Y-%m-%dT%H:%M"), new_2_end_dt.strftime("%Y-%m-%dT%H:%M")

    app.db.update("blocks", ba["id"], {"start": new_a_start, "end": new_a_end, "pinned": 1})
    app.db.update("blocks", bb["id"], {"start": new_b_start, "end": new_b_end, "pinned": 1})

    # If swapping two blocks of the SAME task, also swap their subtask order (idx) so realign does not undo the user's explicit swap
    realigned_a, realigned_b = [], []
    if ba.get("task_id") and ba.get("task_id") == bb.get("task_id"):
        if ba.get("subtask_id") and bb.get("subtask_id") and ba["subtask_id"] != bb["subtask_id"]:
            sub_a = app.db.one("SELECT * FROM subtasks WHERE id=?", (int(ba["subtask_id"]),))
            sub_b = app.db.one("SELECT * FROM subtasks WHERE id=?", (int(bb["subtask_id"]),))
            if sub_a and sub_b:
                app.db.update("subtasks", sub_a["id"], {"idx": sub_b.get("idx", 0)})
                app.db.update("subtasks", sub_b["id"], {"idx": sub_a.get("idx", 0)})
        _sync_block_event(app, app.db.one("SELECT * FROM blocks WHERE id=?", (ba["id"],)), old_start=old_a_start)
        _sync_block_event(app, app.db.one("SELECT * FROM blocks WHERE id=?", (bb["id"],)), old_start=old_b_start)
    else:
        _sync_block_event(app, app.db.one("SELECT * FROM blocks WHERE id=?", (ba["id"],)), old_start=old_a_start)
        _sync_block_event(app, app.db.one("SELECT * FROM blocks WHERE id=?", (bb["id"],)), old_start=old_b_start)
        if ba.get("task_id"):
            realigned_a = realign_task_subtask_order(app, ba["task_id"])
        if bb.get("task_id"):
            realigned_b = realign_task_subtask_order(app, bb["task_id"])

    ba_final = app.db.one("SELECT * FROM blocks WHERE id=?", (ba["id"],))
    bb_final = app.db.one("SELECT * FROM blocks WHERE id=?", (bb["id"],))
    summary = f"Swapped '{ba_final['title']}' ({new_a_start.replace('T', ' ')}) ⇄ '{bb_final['title']}' ({new_b_start.replace('T', ' ')})"
    app.toast(summary, "success")
    app.notify("tasks_changed", {"action": "blocks_swapped", "a": ba["id"], "b": bb["id"]})
    app.notify("events_changed", {"action": "blocks_swapped", "a": ba["id"], "b": bb["id"]})
    return {
        "ok": True,
        "summary": summary,
        "block_a": ba_final,
        "block_b": bb_final,
        "realigned": realigned_a + realigned_b
    }


def cascade_task_from_block(app, task_id: int = None, block_id=None, earliest_start: str = None, event_id: str = None) -> dict:
    """Pushes a subtask block (e.g. when the previous subtask overran its slot) and cascades all subsequent subtasks forward in strict sequential order."""
    target_block = None
    if block_id is not None:
        try:
            target_block = app.db.one("SELECT * FROM blocks WHERE id=?", (int(block_id),))
        except (ValueError, TypeError):
            target_block = app.db.one("SELECT * FROM blocks WHERE event_id=?", (str(block_id).strip(),))
        if target_block and not task_id:
            task_id = target_block["task_id"]

    if not target_block and event_id:
        target_block = app.db.one("SELECT * FROM blocks WHERE event_id=?", (str(event_id).strip(),))
        if target_block and not task_id:
            task_id = target_block["task_id"]

    if not task_id:
        raise ValueError("task_id or block_id is required")

    t = app.db.one("SELECT * FROM tasks WHERE id=?", (int(task_id),))
    if not t:
        raise ValueError(f"Task #{task_id} not found")

    # First ensure all existing blocks of this task have sequential subtask assignments
    realign_task_subtask_order(app, int(task_id))

    all_blocks = app.db.q("SELECT * FROM blocks WHERE task_id=? AND status!='removed' ORDER BY start, end, id", (int(task_id),))
    if not all_blocks:
        raise ValueError(f"Task #{task_id} has no blocks to cascade")

    if not target_block:
        target_block = all_blocks[0]
    else:
        target_block = app.db.one("SELECT * FROM blocks WHERE id=?", (target_block["id"],)) or all_blocks[0]

    old_target_start = target_block["start"]
    dur_mins = max(15, int((dt(target_block["end"]) - dt(target_block["start"])).total_seconds() // 60))
    last_block_end = max(dt(b["end"]) for b in all_blocks)

    # Determine where the newly added replacement slot at the end of the chain should start searching:
    # By shifting target_block's slot to the next free slot after the chain (or after earliest_start) and realigning,
    # Subtask k moves into Block k+1's slot, Subtask k+1 moves into Block k+2's slot, and the last subtask lands in the new slot!
    if earliest_start:
        es_str = str(earliest_start).strip()[:16]
        if len(es_str) == 10:
            es_str += "T09:00"
        search_from_dt = max(dt(es_str), datetime.now())
    else:
        search_from_dt = max(last_block_end, dt(target_block["end"]), datetime.now())

    # Build busy intervals from all calendar events + all other blocks (including the other blocks of this task!)
    busy = app.busy(exclude_task_id=int(task_id))
    load = app.day_load(exclude_task_id=int(task_id))
    for ob in all_blocks:
        if ob["id"] == target_block["id"]:
            continue
        s_ob, e_ob = dt(ob["start"]), dt(ob["end"])
        busy.append((s_ob, e_ob))
        day_k = s_ob.strftime("%Y-%m-%d")
        load[day_k] = load.get(day_k, 0) + int((e_ob - s_ob).total_seconds() // 60)

    # Also block target_block's old slot so we don't place right back into the overrun slot
    busy.append((dt(target_block["start"]), dt(target_block["end"])))

    # Block time before search_from_dt on its starting day
    day_floor = search_from_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if search_from_dt > day_floor:
        busy.append((day_floor, search_from_dt))

    prefs = json.loads(t.get("prefs_json") or "{}")
    sch = Scheduler(app.cfg, busy=busy, load=load)
    placement = sch.place(
        subtasks=[{"key": target_block.get("subtask_id"), "title": target_block["title"], "minutes": dur_mins}],
        deadline=t.get("deadline"),
        earliest=search_from_dt.strftime("%Y-%m-%d"),
        pref=prefs.get("preferred_time", "any"),
        max_block=max(dur_mins, int(prefs.get("max_block_minutes") or 120)),
        spread=False
    )

    if not placement["blocks"]:
        raise ValueError("No free slot found on the calendar to cascade into")

    new_slot = placement["blocks"][0]
    app.db.update("blocks", target_block["id"], {
        "start": new_slot["start"],
        "end": new_slot["end"],
        "pinned": 1
    })
    updated_target = app.db.one("SELECT * FROM blocks WHERE id=?", (target_block["id"],))
    _sync_block_event(app, updated_target, old_start=old_target_start)

    # Now realign all blocks of the task chronologically so Subtask k -> Slot k+1, Subtask k+1 -> Slot k+2, ..., Last Subtask -> New Slot!
    realigned = realign_task_subtask_order(app, int(task_id))

    summary = (
        f"Cascaded subtasks in sequential order · added slot {new_slot['start'].replace('T', ' ')}–{new_slot['end'][11:16]} "
        f"and shifted {len(realigned)} block(s)"
    )
    app.toast(summary, "success")
    app.notify("tasks_changed", {"action": "cascaded", "id": int(task_id)})
    app.notify("events_changed", {"action": "cascaded", "id": int(task_id)})
    return {
        "ok": True,
        "summary": summary,
        "realigned": realigned,
        "task": get_task_full(app, int(task_id))
    }


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
            "actual_minutes": None,
            "done_at": ""
        })
        # Remove the calibration sample for this subtask when unchecked/reopened
        memory.remove_sample_by_ref(app.db, "task", f"subtask:{subtask_id}")
        if task and task.get("status") == "done":
            has_sched = bool(app.db.q("SELECT id FROM blocks WHERE task_id=? AND status='scheduled'", (s["task_id"],)))
            app.db.update("tasks", s["task_id"], {"status": "scheduled" if has_sched else "draft", "updated_at": now})
        app.notify("tasks_changed", {"action": "subtask_reopened", "id": s["task_id"]})
        return get_task_full(app, s["task_id"])

    logged_mins = int(actual_minutes) if actual_minutes else int(s["minutes"] or 30)
    app.db.update("subtasks", subtask_id, {
        "status": "done",
        "actual_minutes": logged_mins,
        "done_at": now
    })

    # Check if all subtasks are done
    all_subs = app.db.q("SELECT status FROM subtasks WHERE task_id=?", (s["task_id"],))
    if all(r["status"] == "done" for r in all_subs):
        app.db.update("tasks", s["task_id"], {"status": "done", "updated_at": now})

    # Record or update single calibration sample for this subtask (upserts by ref)
    if task:
        memory.add_sample(app.db, "task", task["category"] or "thesis", s["minutes"], logged_mins,
                          label=s["title"], ref=f"subtask:{subtask_id}")
        app.toast(f"Subtask '{s['title']}' logged ({logged_mins}m) — calibration updated", "info")

    app.notify("tasks_changed", {"action": "subtask_done", "id": s["task_id"]})
    return get_task_full(app, s["task_id"])


def task_feedback(app, task_id: int, actual_minutes: int, note: str = "") -> dict:
    """User reports (or updates) how long the entire task took, recalibrating future estimates in place."""
    t = app.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not t:
        raise ValueError(f"Task #{task_id} not found")

    est = t["estimate_minutes"] or 1
    category = t["category"] or "thesis"
    memory.add_sample(app.db, "task", category, est, actual_minutes, label=t["title"], ref=f"task:{task_id}")

    # Replace any previous feedback belief for this task instead of duplicating
    prefix = f"Task '{t['title']}': took "
    existing_beliefs = app.db.q(
        "SELECT id, text FROM beliefs WHERE scope=? AND substr(text, 1, ?)=? ORDER BY id DESC",
        (category, len(prefix), prefix)
    )
    if note:
        new_text = f"Task '{t['title']}': took {actual_minutes}m (est {est}m). Note: {note}"
        if existing_beliefs:
            keep_bid = existing_beliefs[0]["id"]
            memory.remember(app.db, category, new_text, replace_id=keep_bid)
            for extra in existing_beliefs[1:]:
                memory.forget(app.db, extra["id"])
        else:
            memory.remember(app.db, category, new_text)
    elif existing_beliefs:
        # Preserve existing note text if the user only corrected the minutes
        old_text = existing_beliefs[0]["text"]
        old_note = old_text.split("Note: ", 1)[1] if "Note: " in old_text else ""
        if old_note:
            new_text = f"Task '{t['title']}': took {actual_minutes}m (est {est}m). Note: {old_note}"
            memory.remember(app.db, category, new_text, replace_id=existing_beliefs[0]["id"])
        for extra in existing_beliefs[1:]:
            memory.forget(app.db, extra["id"])

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

    _clean_task_calendar_events(app, task_id)

    app.db.x("DELETE FROM blocks WHERE task_id=?", (task_id,))
    app.db.x("DELETE FROM subtasks WHERE task_id=?", (task_id,))
    app.db.x("DELETE FROM tasks WHERE id=?", (task_id,))
    app.db.log("task_deleted", f"Deleted task #{task_id} '{t['title']}'")
    app.notify("tasks_changed", {"action": "deleted", "id": task_id})
    return True

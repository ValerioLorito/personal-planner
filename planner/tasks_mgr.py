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
                realign_task_subtask_order(app, tid)
                applied.append(f"Added block to task #{tid} ({ns}–{ne})")

        elif action == "swap_blocks" and bid and e.get("other_block_id"):
            other_bid = int(e["other_block_id"])
            swap_res = swap_blocks(app, int(bid), other_bid)
            applied.append(swap_res.get("summary") or f"Swapped block #{bid} with block #{other_bid}")

        elif action in ("cascade_subtasks", "cascade_from_block") and (tid or bid):
            cascade_res = cascade_task_from_block(
                app,
                task_id=tid,
                block_id=int(bid) if bid else None,
                earliest_start=e.get("new_start") or e.get("earliest")
            )
            applied.append(cascade_res.get("summary") or f"Cascaded subtasks in order for task #{tid}")

        elif action in ("realign_subtasks", "reorder_subtasks") and tid:
            changed = realign_task_subtask_order(app, tid)
            applied.append(f"Realigned {len(changed)} subtask block(s) in sequential order for task #{tid}")

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
    blocks = app.db.q("SELECT * FROM blocks WHERE task_id=? ORDER BY start, end, id", (int(task_id),))
    if len(blocks) <= 1:
        return []

    # Match each block to its subtask (via subtask_id or title fallback)
    matched_blocks = []
    for b in blocks:
        sid = b.get("subtask_id")
        sub = sub_by_id.get(sid) if sid else None
        if not sub:
            b_title = (b.get("title") or "").lower()
            for s in subs:
                if s["title"] and s["title"].lower() in b_title:
                    sub = s
                    break
        if sub and sub.get("status") != "done":
            matched_blocks.append((b, sub))

    if len(matched_blocks) <= 1:
        return []

    # The blocks are already in chronological order (by start, end, id).
    # Sort their assigned subtasks by (subtask.idx, subtask.id) so earlier slots always get earlier subtasks.
    ordered_subs = sorted([pair[1] for pair in matched_blocks], key=lambda s: (s.get("idx", 0), s["id"]))
    task_title = (t.get("title") or t.get("input_text") or "Task").strip()

    updated = []
    for (b, old_sub), new_sub in zip(matched_blocks, ordered_subs):
        new_title = f"{task_title} ({new_sub['title']})"
        if b.get("subtask_id") != new_sub["id"] or b.get("title") != new_title:
            app.db.update("blocks", b["id"], {
                "subtask_id": new_sub["id"],
                "title": new_title
            })
            if b.get("event_id"):
                try:
                    app.cal.update(b["event_id"], title=new_title)
                except Exception:
                    pass
            updated.append({
                "block_id": b["id"],
                "start": b["start"],
                "end": b["end"],
                "old_subtask": old_sub["title"],
                "new_subtask": new_sub["title"],
                "title": new_title
            })
    return updated


def move_block(app, block_id: int, new_start: str, new_end: str, preserve_subtask_order: bool = True) -> dict:
    b = app.db.one("SELECT * FROM blocks WHERE id=?", (block_id,))
    if not b:
        raise ValueError(f"Block #{block_id} not found")

    try:
        s_dt = datetime.fromisoformat(str(new_start)[:16])
        e_dt = datetime.fromisoformat(str(new_end)[:16])
    except ValueError:
        raise ValueError("Invalid start/end timestamp (expected YYYY-MM-DDTHH:MM)")
    if e_dt <= s_dt:
        raise ValueError("A block must end after it starts")

    ns_clean = s_dt.strftime("%Y-%m-%dT%H:%M")
    ne_clean = e_dt.strftime("%Y-%m-%dT%H:%M")

    app.db.update("blocks", block_id, {
        "start": ns_clean,
        "end": ne_clean,
        "pinned": 1
    })

    # Update calendar event if already scheduled
    if b.get("event_id"):
        try:
            app.cal.update(b["event_id"], start=ns_clean, end=ne_clean)
        except Exception as e:
            app.toast(f"Failed to move calendar event: {e}", "warning")

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


def swap_blocks(app, block_a_id: int, block_b_id: int) -> dict:
    """Swaps the scheduled time slots of two blocks (across different tasks or within the same task) and preserves sequential subtask order."""
    ba = app.db.one("SELECT * FROM blocks WHERE id=?", (int(block_a_id),))
    bb = app.db.one("SELECT * FROM blocks WHERE id=?", (int(block_b_id),))
    if not ba or not bb:
        raise ValueError("Both blocks must exist to swap their slots")
    if ba["id"] == bb["id"]:
        raise ValueError("Cannot swap a block with itself")

    sa_dt, ea_dt = dt(ba["start"]), dt(ba["end"])
    sb_dt, eb_dt = dt(bb["start"]), dt(bb["end"])
    dur_a = ea_dt - sa_dt
    dur_b = eb_dt - sb_dt

    # Place block A at B's start, and block B at A's start (preserving each block's duration)
    new_a_start = sb_dt.strftime("%Y-%m-%dT%H:%M")
    new_a_end = (sb_dt + dur_a).strftime("%Y-%m-%dT%H:%M")
    new_b_start = sa_dt.strftime("%Y-%m-%dT%H:%M")
    new_b_end = (sa_dt + dur_b).strftime("%Y-%m-%dT%H:%M")

    app.db.update("blocks", ba["id"], {"start": new_a_start, "end": new_a_end, "pinned": 1})
    app.db.update("blocks", bb["id"], {"start": new_b_start, "end": new_b_end, "pinned": 1})

    if ba.get("event_id"):
        try:
            app.cal.update(ba["event_id"], start=new_a_start, end=new_a_end)
        except Exception as e:
            app.toast(f"Failed to update event for block #{ba['id']}: {e}", "warning")
    if bb.get("event_id"):
        try:
            app.cal.update(bb["event_id"], start=new_b_start, end=new_b_end)
        except Exception as e:
            app.toast(f"Failed to update event for block #{bb['id']}: {e}", "warning")

    realigned_a = realign_task_subtask_order(app, ba["task_id"]) if ba.get("task_id") else []
    realigned_b = (
        realign_task_subtask_order(app, bb["task_id"])
        if bb.get("task_id") and bb.get("task_id") != ba.get("task_id")
        else []
    )

    summary = f"Swapped '{ba['title']}' ({new_a_start}) ⇄ '{bb['title']}' ({new_b_start})"
    app.toast(summary, "success")
    app.notify("tasks_changed", {"action": "blocks_swapped", "a": ba["id"], "b": bb["id"]})
    app.notify("events_changed", {"action": "blocks_swapped", "a": ba["id"], "b": bb["id"]})
    return {
        "ok": True,
        "summary": summary,
        "block_a": app.db.one("SELECT * FROM blocks WHERE id=?", (ba["id"],)),
        "block_b": app.db.one("SELECT * FROM blocks WHERE id=?", (bb["id"],)),
        "realigned": realigned_a + realigned_b
    }


def cascade_task_from_block(app, task_id: int = None, block_id: int = None, earliest_start: str = None) -> dict:
    """Pushes a subtask block (e.g. when the previous subtask overran its slot) and cascades all subsequent subtasks forward in strict sequential order."""
    target_block = None
    if block_id:
        target_block = app.db.one("SELECT * FROM blocks WHERE id=?", (int(block_id),))
        if target_block and not task_id:
            task_id = target_block["task_id"]
    if not task_id:
        raise ValueError("task_id or block_id is required")

    t = app.db.one("SELECT * FROM tasks WHERE id=?", (int(task_id),))
    if not t:
        raise ValueError(f"Task #{task_id} not found")

    all_blocks = app.db.q("SELECT * FROM blocks WHERE task_id=? ORDER BY start, end, id", (int(task_id),))
    if not all_blocks:
        raise ValueError(f"Task #{task_id} has no blocks to cascade")

    if not target_block:
        target_block = all_blocks[0]

    # Split into fixed blocks ( strictly before target_block ) and cascading blocks ( target_block and onwards )
    fixed_blocks = [b for b in all_blocks if b["start"] < target_block["start"] and b["id"] != target_block["id"]]
    cascading_blocks = [b for b in all_blocks if b["id"] == target_block["id"] or b["start"] >= target_block["start"]]

    # Determine the earliest start time for the cascaded chain:
    # By default, start right after target_block's current end (since the previous subtask occupied target_block's slot!)
    if earliest_start:
        es_str = str(earliest_start).strip()[:16]
        if len(es_str) == 10:
            es_str += "T09:00"
        cascade_after_dt = dt(es_str)
    else:
        cascade_after_dt = max(dt(target_block["end"]), datetime.now())

    # Build ordered subtask items to re-place for the cascading blocks
    subs = app.db.q("SELECT * FROM subtasks WHERE task_id=? ORDER BY idx, id", (int(task_id),))
    sub_by_id = {s["id"]: s for s in subs}

    sub_items = []
    for b in cascading_blocks:
        s_obj = sub_by_id.get(b.get("subtask_id"))
        if not s_obj:
            b_title = (b.get("title") or "").lower()
            for s in subs:
                if s["title"] and s["title"].lower() in b_title:
                    s_obj = s
                    break
        dur_mins = max(15, int((dt(b["end"]) - dt(b["start"])).total_seconds() // 60))
        if s_obj:
            sub_items.append({
                "key": s_obj["id"],
                "idx": s_obj.get("idx", 0),
                "title": s_obj["title"],
                "minutes": dur_mins
            })
        else:
            sub_items.append({
                "key": None,
                "idx": 999,
                "title": b.get("title") or t["title"],
                "minutes": dur_mins
            })

    # Ensure strict sequential subtask order (idx 0, 1, 2...)
    sub_items.sort(key=lambda x: x["idx"])

    # Remove old calendar events for the cascading blocks
    for cb in cascading_blocks:
        if cb.get("event_id"):
            try:
                app.cal.delete(cb["event_id"])
            except Exception:
                pass
        app.db.x("DELETE FROM blocks WHERE id=?", (cb["id"],))

    # Compute busy intervals (including fixed_blocks of this task + all other events/tasks)
    busy = app.busy(exclude_task_id=int(task_id))
    load = app.day_load(exclude_task_id=int(task_id))
    for fb in fixed_blocks:
        s_fb, e_fb = dt(fb["start"]), dt(fb["end"])
        busy.append((s_fb, e_fb))
        day_k = s_fb.strftime("%Y-%m-%d")
        load[day_k] = load.get(day_k, 0) + int((e_fb - s_fb).total_seconds() // 60)

    # Also block everything before cascade_after_dt on its starting day so placement begins strictly after the overrun slot
    day_floor = cascade_after_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if cascade_after_dt > day_floor:
        busy.append((day_floor, cascade_after_dt))

    prefs = json.loads(t.get("prefs_json") or "{}")
    sch = Scheduler(app.cfg, busy=busy, load=load)
    placement = sch.place(
        subtasks=sub_items,
        deadline=t.get("deadline"),
        earliest=cascade_after_dt.strftime("%Y-%m-%d"),
        pref=prefs.get("preferred_time", "any"),
        max_block=prefs.get("max_block_minutes"),
        spread=False
    )

    target_cal = app.calendar_for(t["category"])
    total_subs = max(len(subs), len(all_blocks))
    for b in placement["blocks"]:
        b_title = f"{t['title']} ({b['title']})" if total_subs > 1 and not str(b["title"]).startswith(t["title"]) else b["title"]
        app.db.insert("blocks", {
            "task_id": int(task_id),
            "subtask_id": b["key"],
            "start": b["start"],
            "end": b["end"],
            "status": "proposed",
            "calendar": target_cal,
            "event_id": "",
            "title": b_title,
            "pinned": 0
        })

    if t.get("status") == "scheduled":
        schedule_task(app, int(task_id))

    realign_task_subtask_order(app, int(task_id))
    summary = f"Cascaded {len(placement['blocks'])} subtask block(s) in sequential order starting after {cascade_after_dt.strftime('%a %H:%M')}"
    app.toast(summary, "success")
    app.notify("tasks_changed", {"action": "cascaded", "id": int(task_id)})
    app.notify("events_changed", {"action": "cascaded", "id": int(task_id)})
    return {
        "ok": True,
        "summary": summary,
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
